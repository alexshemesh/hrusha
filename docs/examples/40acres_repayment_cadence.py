"""40acres repayment cadence: when does money flow back to the vault?

Answers the question: "my deposit is locked at 100% utilization -- is any principal
being repaid, or only interest?" Builds the weekly/daily repayment schedule from
LoanPaid events (USDC flowing back), calibrated to calendar time via piecewise-linear
block->timestamp interpolation (avoids per-block eth_getBlockByNumber calls), and
reports new-borrowing cadence over the recent window so you can tell whether
repayments are being re-lent straight back out.

Run:  python docs/examples/40acres_repayment_cadence.py
"""

import concurrent.futures
import datetime
import json
import tempfile
import time

import requests
from web3 import Web3

from hrusha.config import load_config

cfg = load_config()
ALCHEMY = f"https://base-mainnet.g.alchemy.com/v2/{cfg.alchemy_api_key}"
PUBLIC = "https://mainnet.base.org"
LOAN = Web3.to_checksum_address("0x87f18b377e625b62c708d5f6ea96ec193558efd0")
VAULT = Web3.to_checksum_address("0xb99b6df96d4d5448cc0a5b3e0ef7896df9507cf5")
USDC = Web3.to_checksum_address("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")
T0_PAID = "0x" + Web3.keccak(text="LoanPaid(uint256,address,uint256,uint256,uint256,bool)").hex()
# correct sig from ABI: LoanPaid(uint256,address,uint256,uint256,bool)
T0_PAID = "0x" + Web3.keccak(text="LoanPaid(uint256,address,uint256,uint256,bool)").hex()
T0_BORROW = "0x" + Web3.keccak(text="FundsBorrowed(uint256,address,uint256)").hex()
DEPLOY = 25712608


def rpc(url, method, params):
    for _ in range(3):
        try:
            r = requests.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=30,
            )
            if r.status_code == 200 and r.text.strip().startswith("{"):
                d = r.json()
                if "error" in d:
                    time.sleep(0.5)
                    continue
                return d.get("result")
        except Exception:
            time.sleep(0.5)
    return None


def fetch_chunk(fb, tb):
    payload = {
        "fromBlock": hex(fb),
        "toBlock": hex(tb),
        "address": LOAN,
        "topics": [[T0_BORROW, T0_PAID]],
    }
    res = rpc(PUBLIC, "eth_getLogs", [payload])
    return res or []


def eth_call(to, data, url=ALCHEMY):
    return rpc(url, "eth_call", [{"to": to, "data": data}, "latest"])


def main():
    cur = int(rpc(PUBLIC, "eth_blockNumber", []), 16)
    print(f"current block {cur}")

    # ---- scan borrow + paid events ----
    chunks = []
    b = DEPLOY
    while b <= cur:
        e = min(b + 9999, cur)
        chunks.append((b, e))
        b = e + 1
    print(f"scanning {len(chunks)} chunks for borrow+paid events...")
    all_logs = []
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(fetch_chunk, fb, tb) for fb, tb in chunks]
        for f in concurrent.futures.as_completed(futs):
            all_logs.extend(f.result())
    print(f"  {len(all_logs)} logs in {time.time() - t0:.0f}s")

    borrows = []  # (block, loanId, amount)
    pays = []  # (block, loanId, amount, bool_flag)
    for lg in all_logs:
        t = lg["topics"][0].lower()
        d = lg["data"][2:] if lg["data"].startswith("0x") else lg["data"]
        lid = int(d[0:64], 16)
        blk = int(lg["blockNumber"], 16)
        if t == T0_BORROW.lower():
            amt = int(d[128:192], 16)
            borrows.append((blk, lid, amt))
        elif t == T0_PAID.lower():
            # (uint256 loanId, address borrower, uint256 amount, uint256 ?, bool ?)
            amt = int(d[128:192], 16)
            flag = int(d[256:320], 16) if len(d) >= 320 else 0
            pays.append((blk, lid, amt, flag))

    print(f"\nborrows: {len(borrows)}, pays: {len(pays)}")

    # ---- calibrate block->timestamp via linear interpolation (Base ~2s blocks) ----
    # Use 4 calibration points across the range for a piecewise-linear fit.
    calib_blocks = [DEPLOY, (DEPLOY + cur) // 4, (DEPLOY + cur) // 2, cur]
    calib = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            ex.submit(
                lambda b: (
                    b,
                    int(rpc(PUBLIC, "eth_getBlockByNumber", [hex(b), False])["timestamp"], 16),
                ),
                b,
            )
            for b in calib_blocks
        }
        for f in concurrent.futures.as_completed(futs):
            b, ts = f.result()
            calib[b] = ts
    calib_pts = sorted(calib.items())
    print(
        "calibration points:",
        [(b, datetime.datetime.utcfromtimestamp(ts)) for b, ts in calib_pts],
    )

    def approx_ts(blk):
        # piecewise linear interpolation
        for i in range(len(calib_pts) - 1):
            b0, t0 = calib_pts[i]
            b1, t1 = calib_pts[i + 1]
            if b0 <= blk <= b1:
                if b1 == b0:
                    return t0
                return int(t0 + (blk - b0) * (t1 - t0) / (b1 - b0))
        b0, t0 = calib_pts[0]
        if blk < b0:
            return t0 - (b0 - blk) * 2
        b1, t1 = calib_pts[-1]
        return int(t1 + (blk - b1) * 2)

    # ---- weekly repayment schedule ----
    # bucket pays by ISO week
    from collections import defaultdict

    weekly = defaultdict(lambda: [0, 0.0])  # week -> [count, usd]
    daily = defaultdict(lambda: [0, 0.0])
    for blk, _lid, amt, _flag in pays:
        ts = approx_ts(blk)
        if not ts:
            continue
        dt = datetime.datetime.utcfromtimestamp(ts)
        wk = dt.strftime("%G-W%V")
        d = dt.strftime("%Y-%m-%d")
        weekly[wk][0] += 1
        weekly[wk][1] += amt / 1e6
        daily[d][0] += 1
        daily[d][1] += amt / 1e6

    print("\n=== weekly repayment cadence (USDC flowing back to vault) ===")
    for wk in sorted(weekly):
        c, u = weekly[wk]
        print(f"  {wk}  {c:4d} payments  ${u:>12,.2f}")

    # last 30 days daily
    print("\n=== last 30 days daily repayments ===")
    recent = sorted(daily)[-30:]
    for d in recent:
        c, u = daily[d]
        print(f"  {d}  {c:3d}  ${u:>10,.2f}")

    # ---- vault state ----
    print("\n=== vault / loan state ===")
    totalAssets = int(eth_call(VAULT, "0x" + Web3.keccak(text="totalAssets()")[:4].hex()), 16)
    idle = int(
        eth_call(
            USDC,
            "0x"
            + Web3.keccak(text="balanceOf(address)")[:4].hex()
            + int(VAULT, 16).to_bytes(32, "big").hex(),
        ),
        16,
    )
    outstanding = int(
        eth_call(LOAN, "0x" + Web3.keccak(text="_outstandingCapital()")[:4].hex()), 16
    )
    print(f"  totalAssets       ${totalAssets / 1e6:,.2f}")
    print(f"  vault idle USDC   ${idle / 1e6:,.2f}")
    print(f"  outstanding loans ${outstanding / 1e6:,.2f}")
    print(f"  utilization       {(outstanding / (totalAssets or 1)) * 100:.2f}%")

    # ---- borrow cadence last ~9 days (is re-borrowing immediate?) ----
    rb_daily = defaultdict(float)
    for blk, _lid, amt in borrows:
        if blk > cur - 400000:
            ts = approx_ts(blk)
            rb_daily[datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")] += amt / 1e6
    print("\n=== last ~9 days: new borrowing per day ===")
    for d in sorted(rb_daily):
        print(f"  {d}  ${rb_daily[d]:>10,.2f}")

    # save
    json.dump(
        {
            "weekly": {k: v for k, v in weekly.items()},
            "daily_recent": {k: v for k, v in daily.items()},
            "totalAssets": totalAssets,
            "idle": idle,
            "outstanding": outstanding,
            "n_borrows": len(borrows),
            "n_pays": len(pays),
        },
        open(f"{tempfile.gettempdir()}/hrusha_cadence.json", "w"),
        indent=1,
    )


if __name__ == "__main__":
    main()
