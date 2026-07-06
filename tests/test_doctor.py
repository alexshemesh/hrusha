"""Ledger-vs-chain reconciliation with stubbed balance readers."""

from decimal import Decimal

from hrusha.ledger.ingest import ingest_fees, ingest_transfers
from hrusha.service.doctor import reconcile
from tests.conftest import MAIN, OUTSIDER, TOKEN_CONTRACT, TX_1, TX_2, make_fee, make_transfer

ADDRESSES = {"main": MAIN}
NFT_CONTRACT = "0x" + "b" * 40


def no_price(token_key, ts):
    return None


def seed_ledger(ledger):
    """100.5 USDC in, 30 out; 0.5 ETH in; one NFT in; 0.0002 ETH gas."""
    ingest_transfers(
        ledger,
        [
            make_transfer(amount=Decimal("100.5")),
            make_transfer(log_index=8, direction="out", amount=Decimal("30")),
            make_transfer(
                tx_hash=TX_2, log_index=-1, token="ETH", contract=None, amount=Decimal("0.5")
            ),
            make_transfer(
                tx_hash=TX_2,
                log_index=100_000,
                token="veNFT",
                contract=NFT_CONTRACT,
                token_id="7",
                amount=Decimal(1),
            ),
        ],
        tracked_addresses=set(),
        price_fn=no_price,
    )
    ingest_fees(ledger, [make_fee(amount_eth=Decimal("0.0002"))], {TX_1: 1}, no_price)


def run(ledger, erc20=Decimal("70.5"), nft=1, native=Decimal("0.4998")):
    return reconcile(
        ledger,
        ADDRESSES,
        erc20_balance=lambda contract, address: erc20,
        nft_balance=lambda contract, address: nft,
        native_balance=lambda address: native,
    )


def test_matching_chain_state_reconciles_clean(ledger):
    seed_ledger(ledger)
    rows = run(ledger)
    assert [(r.token, r.ok) for r in rows] == [("ETH", True), ("USDC", True), ("veNFT", True)]
    assert all(r.diff == 0 for r in rows)


def test_missing_outflow_shows_negative_diff(ledger):
    seed_ledger(ledger)
    # chain says 50.5 but ledger nets 70.5: 20 of outflows never got ingested
    (usdc,) = [r for r in run(ledger, erc20=Decimal("50.5")) if r.token == "USDC"]
    assert not usdc.ok
    assert usdc.diff == Decimal("-20")


def test_nft_counts_are_reconciled(ledger):
    seed_ledger(ledger)
    (row,) = [r for r in run(ledger, nft=0) if r.token == "veNFT"]
    assert not row.ok
    assert row.diff == Decimal(-1)
    assert row.ledger == Decimal(1)


def test_native_tolerance_absorbs_reverted_tx_gas(ledger):
    seed_ledger(ledger)
    (eth,) = [r for r in run(ledger, native=Decimal("0.4993")) if r.token == "ETH"]
    assert eth.ok  # 0.0005 short: reverted-tx gas the ledger cannot see
    assert eth.note != ""


def test_failing_balance_call_is_reported_not_raised(ledger):
    seed_ledger(ledger)

    def exploding(contract, address):
        raise ValueError("spam token reverted")

    rows = reconcile(
        ledger,
        ADDRESSES,
        erc20_balance=exploding,
        nft_balance=lambda contract, address: 1,
        native_balance=lambda address: Decimal("0.4998"),
    )
    (usdc,) = [r for r in rows if r.token == "USDC"]
    assert not usdc.ok
    assert usdc.onchain is None
    assert "failed" in usdc.note


def test_tokens_of_other_addresses_are_not_mixed_in(ledger):
    ingest_transfers(
        ledger,
        [make_transfer(address=OUTSIDER, counterparty=MAIN, amount=Decimal("5"))],
        tracked_addresses=set(),
        price_fn=no_price,
    )
    rows = reconcile(
        ledger,
        ADDRESSES,
        erc20_balance=lambda contract, address: Decimal(0),
        nft_balance=lambda contract, address: 0,
        native_balance=lambda address: Decimal(0),
    )
    assert [r.token for r in rows] == ["ETH"]  # OUTSIDER's USDC is not MAIN's problem
    assert TOKEN_CONTRACT not in [r.contract for r in rows]
