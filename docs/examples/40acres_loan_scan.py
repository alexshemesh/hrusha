"""40acres loan scan: enumerate every loan and measure locked-vs-idle capital.

Answers the question: "my 40acres deposit is stuck -- how much is locked in loans,
how much is idle, and what's the loan history?"

Scans FundsBorrowed and LoanPaid events over the full contract history (concurrent
10k-block chunks via the mainnet.base.org public RPC, which allows that chunk size
whereas Alchemy's free tier caps eth_getLogs at 10 blocks), then reports:
  * total borrowed / paid / active loan counts and id ranges
  * _outstandingCapital() vs vault idle USDC (the stuck-deposit gap)
  * sample _loanDetails() decodes for active loans

Run:  python docs/examples/40acres_loan_scan.py
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
LOAN = Web3.to_checksum_address("0x87f18b377e625b62c708d5f6ea96ec193558efd0")
VAULT = Web3.to_checksum_address("0xb99b6df96d4d5448cc0a5b3e0ef7896df9507cf5")
RPC = "https://mainnet.base.org"
T0_BORROW = "0x" + Web3.keccak(text="FundsBorrowed(uint256,address,uint256)").hex()
T0_PAID = "0x" + Web3.keccak(text="LoanPaid(uint256,address,uint256,uint256,bool)").hex()
DEPLOY = 25712608

# Pre-compute the selectors that ARE in the loan bytecode (from prior matching)
LOAN_DETAIL_SEL = Web3.keccak(text="_loanDetails(uint256)")[:4].hex()
OUTSTANDING_SEL = Web3.keccak(text="_outstandingCapital()")[:4].hex()


def rpc_call(method, params):
    r = requests.post(
        RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=30,
    )
    d = r.json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d["result"]


def fetch_chunk(from_block, to_block):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getLogs",
        "params": [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": LOAN,
                "topics": [[T0_BORROW, T0_PAID]],
            }
        ],
    }
    for attempt in range(3):
        try:
            r = requests.post(RPC, json=payload, timeout=30)
            d = r.json()
            if "error" in d:
                if attempt == 2:
                    return []
                time.sleep(1.5)
                continue
            return d.get("result", [])
        except Exception:
            time.sleep(1.5)
    return []


def eth_call(to, data):
    r = requests.post(
        RPC,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        },
        timeout=20,
    )
    d = r.json()
    return d.get("result", "0x")


def u256(n):
    return int(n).to_bytes(32, "big")


def main():
    cur = int(rpc_call("eth_blockNumber", []), 16)
    print(f"current block {cur}, scanning {cur - DEPLOY} blocks in 10k chunks")

    chunks = []
    b = DEPLOY
    while b <= cur:
        e = min(b + 9999, cur)
        chunks.append((b, e))
        b = e + 1
    print(f"{len(chunks)} chunks")

    all_logs = []
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(fetch_chunk, fb, tb): (fb, tb) for fb, tb in chunks}
        done = 0
        for f in concurrent.futures.as_completed(futs):
            all_logs.extend(f.result())
            done += 1
            if done % 200 == 0:
                print(
                    f"  {done}/{len(chunks)} chunks, {len(all_logs)} logs, {time.time() - t0:.0f}s"
                )
    print(f"scanned {len(all_logs)} logs in {time.time() - t0:.0f}s")

    borrowed = {}  # id -> (block, amount, ts)
    paid = {}  # id -> (block, ts)
    for lg in all_logs:
        t = lg["topics"][0].lower()
        d = lg["data"]
        if d.startswith("0x"):
            d = d[2:]
        lid = int(d[0:64], 16)
        if t == T0_BORROW.lower():
            amt = int(d[128:192], 16)
            borrowed[lid] = (int(lg["blockNumber"], 16), amt)
        elif t == T0_PAID.lower():
            paid[lid] = int(lg["blockNumber"], 16)

    all_ids = sorted(set(borrowed) | set(paid))
    active_ids = sorted(set(borrowed) - set(paid))
    print(f"\nloans borrowed: {len(borrowed)}, paid: {len(paid)}, active: {len(active_ids)}")
    print(f"id range: {all_ids[0]} .. {all_ids[-1]}")

    # Get timestamps for active loan borrow blocks in batch
    def ts_for_block(blk):
        return int(rpc_call("eth_getBlockByNumber", [hex(blk), False])["timestamp"], 16)

    # Read _loanDetails for each active loan
    # Returns 14 words: decode schedule
    active_details = {}
    for lid in active_ids:
        data = "0x" + LOAN_DETAIL_SEL + u256(lid).hex()
        res = eth_call(LOAN, data)
        if res == "0x" or len(res) < 2 + 32 * 8:
            continue
        rb = bytes.fromhex(res[2:])
        words = [int.from_bytes(rb[k : k + 32], "big") for k in range(0, len(rb), 32)]
        active_details[lid] = words

    print(f"\ngot details for {len(active_details)} active loans")
    # Decode: figure out which word is start time, duration, principal, interest
    # Show a sample to identify fields
    if active_details:
        sample_id = next(iter(active_details))
        w = active_details[sample_id]
        print(f"\nsample loan id={sample_id} words ({len(w)}):")
        for i, v in enumerate(w):
            tag = ""
            if 1_000_000_000 < v < 2_000_000_000:
                tag = f"  <-- unix ts? {datetime.datetime.utcfromtimestamp(v)}"
            elif v != 0 and v < 2**160 and len(hex(v)) == 42:
                tag = f"  <-- addr? {Web3.to_checksum_address('0x' + hex(v)[2:].rjust(40, '0'))}"
            elif v < 10**18 and v > 10**12:
                tag = f"  <-- usdc amt? ${v / 1e6:,.2f}"
            print(f"  [{i:2d}] {v}{tag}")

    # Save raw data
    out = {
        "borrowed": {str(k): v for k, v in borrowed.items()},
        "paid": {str(k): v for k, v in paid.items()},
        "active_ids": active_ids,
        "active_details": {str(k): v for k, v in active_details.items()},
    }
    out_path = f"{tempfile.gettempdir()}/hrusha_loans.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"\nsaved {out_path}")

    # Totals
    outstanding = int(eth_call(LOAN, "0x" + OUTSTANDING_SEL), 16)
    print(f"\n_outstandingCapital: ${outstanding / 1e6:,.2f}")
    # vault idle USDC
    usdc = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    bal_sel = Web3.keccak(text="balanceOf(address)")[:4].hex() + u256(int(VAULT, 16)).hex()
    idle = int(eth_call(usdc, "0x" + bal_sel), 16)
    print(f"vault idle USDC: ${idle / 1e6:,.2f}")


if __name__ == "__main__":
    main()
