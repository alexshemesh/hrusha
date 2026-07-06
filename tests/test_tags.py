"""Tagging engine, epoch calendar, and the neto report."""

from decimal import Decimal

import pytest

from hrusha.ledger import reports
from hrusha.ledger.tags import (
    REINVEST_WINDOW_SECONDS,
    SECONDS_PER_WEEK,
    add_rule,
    assign_epochs,
    epoch_id_for,
    retag_all,
    set_manual_tag,
)
from tests.conftest import COLD, MAIN, OUTSIDER, TOKEN_CONTRACT, TS_1, TX_1, TX_2

# TS_1 = 1_750_000_000 falls in the epoch that flipped Thu 2025-06-12 00:00 UTC
EPOCH_OF_TS_1 = "2025-06-12"
EPOCH_START = TS_1 - TS_1 % SECONDS_PER_WEEK


def insert_event(conn, **overrides) -> int:
    values = dict(
        ts=TS_1,
        chain="base",
        tx_hash=TX_1,
        log_index=0,
        block=31_000_000,
        kind="transfer_in",
        token="USDC",
        amount_native="100",
        usd_at_time=100.0,
        gas_usd=None,
        address=MAIN,
        counterparty=OUTSIDER,
        contract=TOKEN_CONTRACT,
    )
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" * len(values))
    with conn:
        cursor = conn.execute(
            f"INSERT INTO events ({columns}) VALUES ({placeholders})", tuple(values.values())
        )
    return cursor.lastrowid


def event_tags(conn, event_id) -> set[str]:
    return {t for (t,) in conn.execute("SELECT tag FROM tags WHERE event_id = ?", (event_id,))}


def event_source(conn, event_id):
    return conn.execute("SELECT source FROM events WHERE id = ?", (event_id,)).fetchone()[0]


# -- epochs -------------------------------------------------------------------


def test_epoch_id_is_the_thursday_flip_date():
    assert epoch_id_for(TS_1) == EPOCH_OF_TS_1
    assert epoch_id_for(EPOCH_START) == EPOCH_OF_TS_1  # first second of the epoch
    assert epoch_id_for(EPOCH_START - 1) == "2025-06-05"  # last second of the previous


def test_assign_epochs_matches_python_and_registers_epochs(ledger):
    event_id = insert_event(ledger)
    assert assign_epochs(ledger) == 1
    epoch_id = ledger.execute("SELECT epoch_id FROM events WHERE id = ?", (event_id,)).fetchone()
    assert epoch_id == (EPOCH_OF_TS_1,)  # SQL strftime agrees with epoch_id_for()
    starts, ends = ledger.execute(
        "SELECT starts_ts, ends_ts FROM epochs WHERE epoch_id = ?", (EPOCH_OF_TS_1,)
    ).fetchone()
    assert (starts, ends) == (EPOCH_START, EPOCH_START + SECONDS_PER_WEEK)


# -- rules --------------------------------------------------------------------


def test_rule_matches_contract_and_direction(ledger):
    hit = insert_event(ledger, kind="transfer_in")
    wrong_direction = insert_event(ledger, kind="transfer_out", tx_hash=TX_2)
    wrong_contract = insert_event(ledger, contract="0x" + "b" * 40, log_index=1)
    add_rule(ledger, 100, {"contract": TOKEN_CONTRACT, "direction": "in"}, ["claim"], "aerodrome")

    retag_all(ledger, tracked_addresses={MAIN})

    assert event_tags(ledger, hit) == {"claim"}
    assert event_source(ledger, hit) == "aerodrome"
    # the out-transfer right after the claim earns 'reinvest', but never 'claim'
    assert "claim" not in event_tags(ledger, wrong_direction)
    assert event_source(ledger, wrong_contract) is None


def test_lower_priority_rule_wins_the_source(ledger):
    event_id = insert_event(ledger)
    add_rule(ledger, 200, {"token": "USDC"}, ["b"], "second")
    add_rule(ledger, 100, {"contract": TOKEN_CONTRACT}, ["a"], "first")

    retag_all(ledger, tracked_addresses=set())

    assert event_source(ledger, event_id) == "first"
    assert event_tags(ledger, event_id) == {"a", "b"}  # tags accumulate; source doesn't


def test_manual_tag_survives_retag_and_beats_rules(ledger):
    event_id = insert_event(ledger)
    assert set_manual_tag(ledger, event_id, "income")
    retag_all(ledger, tracked_addresses=set())
    retag_all(ledger, tracked_addresses=set())
    assert event_tags(ledger, event_id) == {"income"}
    origin = ledger.execute(
        "SELECT origin FROM tags WHERE event_id = ? AND tag = 'income'", (event_id,)
    ).fetchone()
    assert origin == ("manual",)


def test_manual_tag_on_missing_event_is_reported(ledger):
    assert set_manual_tag(ledger, 999, "income") is False


def test_own_transfer_is_rederived_by_retag(ledger):
    own = insert_event(ledger, counterparty=COLD)
    stranger = insert_event(ledger, counterparty=OUTSIDER, tx_hash=TX_2)

    retag_all(ledger, tracked_addresses={MAIN, COLD})

    assert event_tags(ledger, own) == {"own-transfer"}
    assert event_tags(ledger, stranger) == set()


def test_gas_fee_inherits_source_from_its_transaction(ledger):
    transfer = insert_event(ledger, kind="transfer_in")
    gas = insert_event(
        ledger, kind="gas_fee", log_index=-1, token="ETH", gas_usd=0.05, contract=None
    )
    unrelated_gas = insert_event(ledger, kind="gas_fee", tx_hash=TX_2, log_index=-1, contract=None)
    add_rule(ledger, 100, {"contract": TOKEN_CONTRACT}, ["claim"], "aerodrome")

    retag_all(ledger, tracked_addresses=set())

    assert event_source(ledger, transfer) == "aerodrome"
    assert event_source(ledger, gas) == "aerodrome"  # same tx_hash
    assert event_source(ledger, unrelated_gas) is None


def test_outgoing_transfer_soon_after_claim_is_a_reinvest(ledger):
    insert_event(ledger, kind="transfer_in")  # will be tagged 'claim' by the rule
    swap_out = insert_event(ledger, kind="transfer_out", tx_hash=TX_2, ts=TS_1 + 3600)
    too_late = insert_event(
        ledger, kind="transfer_out", log_index=2, ts=TS_1 + REINVEST_WINDOW_SECONDS + 1
    )
    add_rule(ledger, 100, {"direction": "in", "contract": TOKEN_CONTRACT}, ["claim"], None)

    retag_all(ledger, tracked_addresses=set())

    assert "reinvest" in event_tags(ledger, swap_out)
    assert "reinvest" not in event_tags(ledger, too_late)


def test_add_rule_rejects_unknown_or_empty_match(ledger):
    with pytest.raises(ValueError, match="unknown match keys"):
        add_rule(ledger, 1, {"colour": "red"}, ["x"])
    with pytest.raises(ValueError, match="at least one field"):
        add_rule(ledger, 1, {}, ["x"])


def test_in_and_out_in_one_tx_is_a_swap(ledger):
    swap_out = insert_event(ledger, kind="transfer_out", token="USDC")
    swap_in = insert_event(ledger, kind="transfer_in", token="CBBTC", log_index=1)
    plain_in = insert_event(ledger, kind="transfer_in", tx_hash=TX_2)
    gas = insert_event(ledger, kind="gas_fee", log_index=-1, token="ETH", contract=None)

    retag_all(ledger, tracked_addresses=set())

    assert "swap" in event_tags(ledger, swap_out)
    assert "swap" in event_tags(ledger, swap_in)
    assert "swap" not in event_tags(ledger, plain_in)
    assert "swap" not in event_tags(ledger, gas)  # gas of a swap tx still counts


# -- neto report --------------------------------------------------------------


def test_neto_report_groups_by_epoch_and_source(ledger):
    insert_event(ledger, kind="transfer_in", usd_at_time=100.0)
    insert_event(
        ledger,
        kind="gas_fee",
        log_index=-1,
        token="ETH",
        gas_usd=2.5,
        usd_at_time=2.5,
        contract=None,
    )
    insert_event(  # next epoch, different token, no rule matches it
        ledger,
        kind="transfer_in",
        tx_hash=TX_2,
        ts=TS_1 + SECONDS_PER_WEEK,
        usd_at_time=7.0,
        token="OTHER",
        contract="0x" + "b" * 40,
    )
    add_rule(ledger, 100, {"contract": TOKEN_CONTRACT, "direction": "in"}, ["claim"], "aerodrome")
    retag_all(ledger, tracked_addresses=set())

    rows = reports.neto_by_epoch_source(ledger)

    by_key = {(r.epoch_id, r.source): r for r in rows}
    aero = by_key[(EPOCH_OF_TS_1, "aerodrome")]
    assert aero.income_usd == 100.0
    assert aero.gas_usd == 2.5
    assert aero.neto_usd == 97.5
    assert (epoch_id_for(TS_1 + SECONDS_PER_WEEK), "untagged") in by_key


def test_neto_report_excludes_own_transfers_and_counts_unpriced(ledger):
    insert_event(ledger, counterparty=COLD, usd_at_time=500.0)  # own-transfer
    unpriced = insert_event(ledger, tx_hash=TX_2, usd_at_time=None)
    del unpriced
    retag_all(ledger, tracked_addresses={MAIN, COLD})

    rows = reports.neto_by_epoch_source(ledger)

    assert len(rows) == 1  # the own-transfer row is gone entirely
    assert rows[0].income_usd == 0.0
    assert rows[0].unpriced_count == 1


def test_neto_report_excludes_swap_legs_and_honors_date_range(ledger):
    insert_event(ledger, kind="transfer_out", token="USDC", usd_at_time=50.0)  # swap leg
    insert_event(ledger, kind="transfer_in", token="CBBTC", log_index=1, usd_at_time=49.9)
    income = insert_event(ledger, kind="transfer_in", tx_hash=TX_2, usd_at_time=10.0)
    del income
    retag_all(ledger, tracked_addresses=set())

    rows = reports.neto_by_epoch_source(ledger)
    assert len(rows) == 1
    assert rows[0].income_usd == 10.0  # both swap legs invisible
    assert rows[0].spend_usd == 0.0

    # until_ts is exclusive: a window ending on the event's ts excludes it
    assert reports.neto_by_epoch_source(ledger, until_ts=TS_1) == []
    assert len(reports.neto_by_epoch_source(ledger, since_ts=TS_1, until_ts=TS_1 + 1)) == 1


def test_locking_into_escrow_is_not_spend(ledger):
    from hrusha.adapters.known_contracts import VOTING_ESCROW, seed_default_rules

    lock = insert_event(
        ledger,
        kind="transfer_out",
        token="AERO",
        counterparty=VOTING_ESCROW,
        usd_at_time=45.77,
    )
    seeded_first = seed_default_rules(ledger)
    seeded_again = seed_default_rules(ledger)
    retag_all(ledger, tracked_addresses=set())

    assert seeded_first >= 3 and seeded_again == 0  # additive once, then idempotent
    assert "lock" in event_tags(ledger, lock)
    rows = reports.neto_by_epoch_source(ledger)
    assert rows == [] or all(r.spend_usd == 0.0 for r in rows)  # the lock is not spend


def test_coins_report_sums_native_amounts_exactly(ledger):
    insert_event(ledger, amount_native="0.1")
    insert_event(ledger, tx_hash=TX_2, amount_native="0.2")
    retag_all(ledger, tracked_addresses=set())

    rows = reports.coins_by_epoch_source(ledger)

    assert rows == [(EPOCH_OF_TS_1, "untagged", "USDC", "in", str(Decimal("0.3")))]
