"""GoPlus token-security spike: can it gate the vote scout's exotic tokens?

Read-only probe — no writes, no state. The scout's majors-only gate is
off (the filter lab showed it costs ~32% with no stability gain), so
the exotic universe needs a MECHANICAL safety check instead of a
reputation one: honeypots, tax walls, pausable transfers, owners who
can rewrite balances. GoPlus (gopluslabs.io) is a free, keyless
security API that covers Base (chain id 8453) and is integrated by
Sushi for exactly this purpose.

This probe must show, for a mix of majors and live exotic tokens:
  1. coverage — does GoPlus know long-tail Base tokens at all
  2. which response fields are usable as hard gates without false
     positives on legit tokens (USDC is mintable and proxied — naive
     flags would nuke every major)
  3. batch behavior + latency (the scout would check ~60 tokens/scan)

Run:  .venv/bin/python docs/examples/goplus_probe.py <token> [<token>...]
      (token addresses; e.g. pair + reward tokens from the /votes page)

FINDINGS (live, 2026-07-07): the unauthenticated BATCH endpoint silently
returns partial results (asked 5 majors, got 1) — the same tokens answer
fine when queried one at a time (~0.3-0.5s each), so this probe retries
missing tokens individually and integrations must do the same. AERO
reads mintable=1 with top10=67% and USDC is a proxy — confirming only
the HARD_RISK_FIELDS below are safe to gate on.
"""

from __future__ import annotations

import sys
import time

import httpx

GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/8453"
BATCH = 30  # keep URLs short; GoPlus accepts comma-separated addresses

# fields that indicate a mechanically dangerous token (per
# docs.gopluslabs.io/reference/response-details); '1' means present.
# Deliberately NOT flagged: is_mintable / is_proxy / is_open_source —
# USDC and most governance tokens trip those, they are not scam markers
HARD_RISK_FIELDS = (
    "is_honeypot",
    "cannot_sell_all",
    "transfer_pausable",
    "is_blacklisted",
    "owner_change_balance",
    "selfdestruct",
    "trading_cooldown",
    "hidden_owner",
)
MAX_TAX = 0.05  # buy/sell tax above 5% is a toll booth


def main() -> None:
    tokens = [t.lower() for t in sys.argv[1:]]
    if not tokens:
        raise SystemExit("usage: goplus_probe.py <token-address> [<token-address>...]")
    http = httpx.Client(timeout=30)
    results: dict[str, dict] = {}
    for start in range(0, len(tokens), BATCH):
        chunk = tokens[start : start + BATCH]
        began = time.time()
        response = http.get(GOPLUS_URL, params={"contract_addresses": ",".join(chunk)})
        response.raise_for_status()
        body = response.json()
        print(
            f"batch of {len(chunk)}: code={body.get('code')} "
            f"({body.get('message')}), {time.time() - began:.1f}s"
        )
        results.update({k.lower(): v for k, v in (body.get("result") or {}).items()})

    # the batch endpoint drops tokens silently — retry the missing ones alone
    for token in [t for t in tokens if t not in results]:
        response = http.get(GOPLUS_URL, params={"contract_addresses": token})
        response.raise_for_status()
        found = response.json().get("result") or {}
        results.update({k.lower(): v for k, v in found.items()})
        if token in results:
            print(f"   (batch had dropped {token}; individual retry found it)")

    for token in tokens:
        data = results.get(token)
        print(f"\n== {token}")
        if data is None:
            print("   NOT COVERED by GoPlus — treat as unknown, not as safe")
            continue
        symbol = data.get("token_symbol", "?")
        holders = data.get("holder_count", "?")
        risks = [f for f in HARD_RISK_FIELDS if data.get(f) == "1"]
        for side in ("buy_tax", "sell_tax"):
            raw = data.get(side)
            if raw not in (None, "") and float(raw) > MAX_TAX:
                risks.append(f"{side}={float(raw):.0%}")
        top10 = sum(float(h.get("percent") or 0) for h in (data.get("holders") or [])[:10])
        print(
            f"   {symbol}: holders={holders}, top10={top10:.0%}, "
            f"open_source={data.get('is_open_source')}, proxy={data.get('is_proxy')}, "
            f"mintable={data.get('is_mintable')}"
        )
        print(f"   hard risks: {risks or 'none'}")
        # show the informational fields a stricter gate could use later
        print(
            f"   info: owner_percent={data.get('owner_percent')}, "
            f"creator_percent={data.get('creator_percent')}, "
            f"lp_holders={len(data.get('lp_holders') or [])} tracked"
        )


if __name__ == "__main__":
    main()
