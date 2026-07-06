"""Ledger-vs-chain reconciliation: does our history explain today's balances?

For every (address, token) the ledger has seen, net ledger flows must
equal the current on-chain balance:

- ERC-20:  sum(in) - sum(out)                 == balanceOf(address)
- ERC-721: count(in) - count(out)             == balanceOf(address)
- native:  sum(in) - sum(out) - sum(gas fees) == eth_getBalance(address)

A mismatch means missing or duplicated ledger legs (a provider gap, a
cursor bug) — or a token that moves balances without Transfer events
(rebasing/fee-on-transfer, reverted-tx gas). The report states the
discrepancy; deciding what it means stays with the operator.

Chain reads are injected as plain callables so the reconciliation logic
is testable without web3 or a network.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

log = logging.getLogger("hrusha.doctor")

# (contract, address) -> current balance, scaled to token units
Erc20BalanceFn = Callable[[str, str], Decimal]
# (contract, address) -> number of NFTs held
NftBalanceFn = Callable[[str, str], int]
# address -> ETH balance
NativeBalanceFn = Callable[[str], Decimal]

# reverted txs still burn gas but Blockscout txlist rows with isError=1 are
# skipped and carry no receipts here; tiny native drift is expected
NATIVE_TOLERANCE_ETH = Decimal("0.001")


@dataclass(frozen=True)
class ReconcileRow:
    address: str
    token: str  # symbol (or contract fallback)
    contract: str | None  # None for native ETH
    ledger: Decimal  # net amount per the ledger
    onchain: Decimal | None  # None when the balance call failed
    diff: Decimal | None  # onchain - ledger; positive = ledger is missing inflows
    ok: bool
    note: str = ""


def reconcile(
    conn: sqlite3.Connection,
    addresses: dict[str, str],
    erc20_balance: Erc20BalanceFn,
    nft_balance: NftBalanceFn,
    native_balance: NativeBalanceFn,
) -> list[ReconcileRow]:
    rows: list[ReconcileRow] = []
    for address in addresses.values():
        rows.append(_reconcile_native(conn, address, native_balance))
        rows.extend(_reconcile_contracts(conn, address, erc20_balance, nft_balance))
    return rows


def _reconcile_native(
    conn: sqlite3.Connection, address: str, native_balance: NativeBalanceFn
) -> ReconcileRow:
    net = _net_amount(conn, address, contract=None, nft=False) - _gas_spent(conn, address)
    return _compare(
        address,
        "ETH",  # positional: S106 mistakes `token="ETH"` for a hardcoded secret
        contract=None,
        ledger=net,
        balance_fn=lambda: native_balance(address),
        tolerance=NATIVE_TOLERANCE_ETH,
        tolerance_note="within gas-of-reverted-txs tolerance",
    )


def _reconcile_contracts(
    conn: sqlite3.Connection,
    address: str,
    erc20_balance: Erc20BalanceFn,
    nft_balance: NftBalanceFn,
) -> list[ReconcileRow]:
    rows = []
    for contract, token, is_nft in conn.execute(
        """
        SELECT contract, MAX(token), MAX(token_id IS NOT NULL) FROM events
        WHERE address = ? AND contract IS NOT NULL
          AND kind IN ('transfer_in', 'transfer_out')
        GROUP BY contract ORDER BY contract
        """,
        (address,),
    ).fetchall():
        net = _net_amount(conn, address, contract, nft=bool(is_nft))
        rows.append(
            _compare(
                address,
                token=token,
                contract=contract,
                ledger=net,
                balance_fn=lambda c=contract, nft=is_nft: (
                    Decimal(nft_balance(c, address)) if nft else erc20_balance(c, address)
                ),
            )
        )
    return rows


def _compare(
    address: str,
    token: str,
    contract: str | None,
    ledger: Decimal,
    balance_fn: Callable[[], Decimal],
    tolerance: Decimal = Decimal(0),
    tolerance_note: str = "",
) -> ReconcileRow:
    try:
        onchain = balance_fn()
    except Exception as exc:  # spam token contracts can revert or misbehave
        log.warning(
            "balance call failed",
            extra={"contract": contract or "native", "error": exc.__class__.__name__},
        )
        return ReconcileRow(
            address=address,
            token=token,
            contract=contract,
            ledger=ledger,
            onchain=None,
            diff=None,
            ok=False,
            note=f"balance call failed ({exc.__class__.__name__})",
        )
    diff = onchain - ledger
    ok = abs(diff) <= tolerance
    note = tolerance_note if (ok and diff != 0) else ""
    return ReconcileRow(
        address=address,
        token=token,
        contract=contract,
        ledger=ledger,
        onchain=onchain,
        diff=diff,
        ok=ok,
        note=note,
    )


def _net_amount(conn: sqlite3.Connection, address: str, contract: str | None, nft: bool) -> Decimal:
    where = "contract = ?" if contract is not None else "contract IS NULL AND token = 'ETH'"
    params: tuple = (address, contract) if contract is not None else (address,)
    rows = conn.execute(
        f"""
        SELECT kind, amount_native FROM events
        WHERE address = ? AND {where} AND kind IN ('transfer_in', 'transfer_out')
          AND (token_id IS NOT NULL) = ?
        """,  # noqa: S608 — `where` is one of two literals above
        (*params, int(nft)),
    ).fetchall()
    net = Decimal(0)
    for kind, amount in rows:
        net += Decimal(amount) if kind == "transfer_in" else -Decimal(amount)
    return net


def _gas_spent(conn: sqlite3.Connection, address: str) -> Decimal:
    rows = conn.execute(
        "SELECT amount_native FROM events WHERE address = ? AND kind = 'gas_fee'",
        (address,),
    ).fetchall()
    return sum((Decimal(a) for (a,) in rows), Decimal(0))


# -- web3 wiring (kept here so the CLI stays thin) -----------------------------

_BALANCE_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]

WEI_PER_ETH = Decimal(10) ** 18


def chain_readers(w3) -> tuple[Erc20BalanceFn, NftBalanceFn, NativeBalanceFn]:
    """Balance callables over a live web3 connection."""
    from web3 import Web3  # deferred: web3 import is slow, only doctor/sync need it

    def contract(address: str):
        return w3.eth.contract(address=Web3.to_checksum_address(address), abi=_BALANCE_ABI)

    def erc20_balance(token_contract: str, holder: str) -> Decimal:
        c = contract(token_contract)
        raw = c.functions.balanceOf(Web3.to_checksum_address(holder)).call()
        return Decimal(raw) / Decimal(10) ** c.functions.decimals().call()

    def nft_balance(token_contract: str, holder: str) -> int:
        raw = contract(token_contract).functions.balanceOf(Web3.to_checksum_address(holder))
        return int(raw.call())

    def native_balance(holder: str) -> Decimal:
        return Decimal(w3.eth.get_balance(Web3.to_checksum_address(holder))) / WEI_PER_ETH

    return erc20_balance, nft_balance, native_balance
