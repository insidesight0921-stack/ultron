from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

import paper_db
from private_paper_write_contract import validate_paper_trade_write_intent
from private_paper_write_store import PaperTradeWriteStore
from private_write_contract import WriteContractError, validate_write_intent


def _rows(db, query, params=()):
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(query, params)]


def _state(db, slot_id):
    with sqlite3.connect(db) as con:
        capital = con.execute(
            "SELECT current_capital FROM slots WHERE id=?", (slot_id,)
        ).fetchone()[0]
        positions = con.execute(
            "SELECT ticker,quantity,avg_price FROM positions WHERE slot_id=? ORDER BY ticker",
            (slot_id,),
        ).fetchall()
        trades = con.execute(
            "SELECT side,ticker,quantity,price,fees FROM trades WHERE slot_id=? ORDER BY id",
            (slot_id,),
        ).fetchall()
    return float(capital), [tuple(row) for row in positions], [tuple(row) for row in trades]


@pytest.fixture
def stack(tmp_path):
    db = tmp_path / "paper.db"
    seeded = paper_db.ensure_seed(db_path=db, seed_capital=100_000_000)
    slots = {item["name"]: int(item["id"]) for item in seeded["slots"]}
    return PaperTradeWriteStore(db, writes_enabled=True), db, slots


def _intent(
    store,
    *,
    slot_id,
    operation="paper.buy",
    payload=None,
    expected_version=None,
    request_id=None,
    approval_id=None,
    idempotency_key=None,
):
    if payload is None:
        payload = {
            "slot": slot_id,
            "ticker": "005930",
            "name": "삼성전자",
            "quantity": 10,
            "price": 80_000,
            "fees": 0,
        }
    return validate_paper_trade_write_intent(
        operation=operation,
        request_id=request_id or str(uuid4()),
        approval_id=approval_id or str(uuid4()),
        idempotency_key=idempotency_key or f"paper-store-{uuid4().hex}",
        expected_version=(
            store.current_version(slot_id)
            if expected_version is None
            else expected_version
        ),
        payload=payload,
    )


def _approve_apply(store, intent):
    slot_id = intent.slot_id
    store.submit(intent)
    store.approve(
        intent.intent.approval_id,
        request_id=intent.intent.request_id,
        scope_value=slot_id,
    )
    return store.apply(
        intent.intent.approval_id,
        request_id=intent.intent.request_id,
        scope_value=slot_id,
    )


def test_disabled_store_creates_no_database(tmp_path):
    db = tmp_path / "disabled.db"
    store = PaperTradeWriteStore(db)
    with pytest.raises(WriteContractError) as exc:
        store.current_version(1)
    assert exc.value.code == "writes_disabled"
    assert not db.exists()


def test_buy_mutates_only_after_approval_and_increments_slot_version(stack):
    store, db, slots = stack
    slot_id = slots["콴텍"]
    before = _state(db, slot_id)
    intent = _intent(store, slot_id=slot_id)

    assert store.submit(intent).state == "pending"
    assert _state(db, slot_id) == before
    assert store.approve(intent.intent.approval_id, scope_value=slot_id).state == "approved"
    assert _state(db, slot_id) == before
    applied = store.apply(intent.intent.approval_id, scope_value=slot_id)

    assert applied.state == "applied"
    assert applied.result == {
        "side": "buy",
        "trade_id": 1,
        "slot_id": slot_id,
        "ticker": "005930",
        "quantity": 10,
        "price": 80_000.0,
        "fees": 0.0,
        "total_cost": 800_000.0,
    }
    capital, positions, trades = _state(db, slot_id)
    assert capital == 39_200_000
    assert positions == [("005930", 10, 80_000.0)]
    assert trades == [("buy", "005930", 10, 80_000.0, 0.0)]
    assert store.current_version(slot_id) == 1
    assert store.current_version(slots["키움"]) == 0


def test_buy_replay_does_not_duplicate_trade_or_version(stack):
    store, db, slots = stack
    slot_id = slots["콴텍"]
    intent = _intent(store, slot_id=slot_id)
    first = _approve_apply(store, intent)
    apply_replay = store.apply(intent.intent.approval_id, scope_value=slot_id)
    submit_replay = store.submit(intent)

    assert first.replayed is False
    assert apply_replay.replayed is True
    assert submit_replay.replayed is True
    assert len(_rows(db, "SELECT id FROM trades")) == 1
    assert store.current_version(slot_id) == 1


def test_second_buy_uses_weighted_average_and_default_fee(stack):
    store, db, slots = stack
    slot_id = slots["콴텍"]
    _approve_apply(store, _intent(store, slot_id=slot_id))
    second = _intent(
        store,
        slot_id=slot_id,
        payload={
            "slot": slot_id,
            "ticker": "005930",
            "name": "삼성전자",
            "quantity": 10,
            "price": 100_000,
        },
    )
    applied = _approve_apply(store, second)

    expected_fee = 1_500.0
    assert applied.result["fees"] == expected_fee
    position = _rows(
        db,
        "SELECT quantity,avg_price FROM positions WHERE slot_id=? AND ticker='005930'",
        (slot_id,),
    )[0]
    assert position["quantity"] == 20
    assert position["avg_price"] == (800_000 + 1_000_000 + expected_fee) / 20
    assert store.current_version(slot_id) == 2


def test_partial_then_full_sell_updates_position_capital_and_version(stack):
    store, db, slots = stack
    slot_id = slots["콴텍"]
    _approve_apply(store, _intent(store, slot_id=slot_id))
    partial = _intent(
        store,
        slot_id=slot_id,
        operation="paper.sell",
        payload={
            "slot": slot_id,
            "ticker": "005930",
            "quantity": 4,
            "price": 90_000,
            "fees": 0,
        },
    )
    first = _approve_apply(store, partial)
    assert first.result["proceeds"] == 360_000.0
    assert _state(db, slot_id)[1] == [("005930", 6, 80_000.0)]

    full = _intent(
        store,
        slot_id=slot_id,
        operation="paper.sell",
        payload={
            "slot": slot_id,
            "ticker": "005930",
            "quantity": 6,
            "price": 90_000,
            "fees": 0,
        },
    )
    second = _approve_apply(store, full)
    capital, positions, trades = _state(db, slot_id)

    assert second.result["proceeds"] == 540_000.0
    assert positions == []
    assert capital == 40_100_000
    assert [row[0] for row in trades] == ["buy", "sell", "sell"]
    assert store.current_version(slot_id) == 3


def test_versions_and_mutations_are_isolated_by_slot(stack):
    store, db, slots = stack
    quantec = slots["콴텍"]
    kium = slots["키움"]
    _approve_apply(store, _intent(store, slot_id=quantec))
    _approve_apply(
        store,
        _intent(
            store,
            slot_id=kium,
            payload={
                "slot": kium,
                "ticker": "000660",
                "name": "SK하이닉스",
                "quantity": 2,
                "price": 200_000,
                "fees": 0,
            },
        ),
    )

    assert store.current_version(quantec) == 1
    assert store.current_version(kium) == 1
    assert _state(db, quantec)[1] == [("005930", 10, 80_000.0)]
    assert _state(db, kium)[1] == [("000660", 2, 200_000.0)]


def test_other_slot_cannot_preflight_approve_or_apply_intent(stack):
    store, _, slots = stack
    owner = slots["콴텍"]
    other = slots["키움"]
    intent = _intent(store, slot_id=owner)
    store.submit(intent)

    with pytest.raises(WriteContractError) as preflight:
        store.preflight(
            operation=intent.intent.operation,
            scope_value=other,
            request_id=intent.intent.request_id,
            approval_id=intent.intent.approval_id,
            idempotency_key=intent.intent.idempotency_key,
        )
    assert preflight.value.code == "scope_conflict"
    with pytest.raises(WriteContractError) as approve:
        store.approve(intent.intent.approval_id, scope_value=other)
    assert approve.value.code == "scope_conflict"
    with pytest.raises(WriteContractError) as apply:
        store.apply(intent.intent.approval_id, scope_value=other)
    assert apply.value.code == "scope_conflict"
    assert store.get_record(intent.intent.approval_id, scope_value=owner).state == "pending"


def test_insufficient_capital_expires_without_trade_or_version_change(stack):
    store, db, slots = stack
    slot_id = slots["IPO"]
    before = _state(db, slot_id)
    intent = _intent(
        store,
        slot_id=slot_id,
        payload={
            "slot": slot_id,
            "ticker": "005930",
            "name": "삼성전자",
            "quantity": 1000,
            "price": 80_000,
            "fees": 0,
        },
    )
    store.submit(intent)
    store.approve(intent.intent.approval_id, scope_value=slot_id)

    with pytest.raises(WriteContractError) as exc:
        store.apply(intent.intent.approval_id, scope_value=slot_id)
    assert exc.value.code == "paper_insufficient_capital"
    assert store.get_record(intent.intent.approval_id, scope_value=slot_id).state == "expired"
    assert _state(db, slot_id) == before
    assert store.current_version(slot_id) == 0


@pytest.mark.parametrize(
    "payload,code",
    [
        (
            {"slot": 1, "ticker": "005930", "quantity": 1, "price": 90_000, "fees": 0},
            "paper_position_not_found",
        ),
    ],
)
def test_invalid_sell_state_expires_without_mutation(stack, payload, code):
    store, db, slots = stack
    slot_id = slots["콴텍"]
    payload = {**payload, "slot": slot_id}
    intent = _intent(store, slot_id=slot_id, operation="paper.sell", payload=payload)
    before = _state(db, slot_id)
    store.submit(intent)
    store.approve(intent.intent.approval_id, scope_value=slot_id)

    with pytest.raises(WriteContractError) as exc:
        store.apply(intent.intent.approval_id, scope_value=slot_id)
    assert exc.value.code == code
    assert _state(db, slot_id) == before
    assert store.current_version(slot_id) == 0


def test_oversell_expires_without_changing_existing_position(stack):
    store, db, slots = stack
    slot_id = slots["콴텍"]
    _approve_apply(store, _intent(store, slot_id=slot_id))
    before = _state(db, slot_id)
    sell = _intent(
        store,
        slot_id=slot_id,
        operation="paper.sell",
        payload={
            "slot": slot_id,
            "ticker": "005930",
            "quantity": 11,
            "price": 90_000,
            "fees": 0,
        },
    )
    store.submit(sell)
    store.approve(sell.intent.approval_id, scope_value=slot_id)

    with pytest.raises(WriteContractError) as exc:
        store.apply(sell.intent.approval_id, scope_value=slot_id)
    assert exc.value.code == "paper_quantity_exceeds_position"
    assert _state(db, slot_id) == before
    assert store.current_version(slot_id) == 1


def test_stale_intent_expires_after_another_trade_advances_slot_version(stack):
    store, db, slots = stack
    slot_id = slots["콴텍"]
    first = _intent(store, slot_id=slot_id)
    stale = _intent(
        store,
        slot_id=slot_id,
        payload={
            "slot": slot_id,
            "ticker": "000660",
            "name": "SK하이닉스",
            "quantity": 1,
            "price": 200_000,
            "fees": 0,
        },
    )
    store.submit(first)
    store.submit(stale)
    store.approve(first.intent.approval_id, scope_value=slot_id)
    store.approve(stale.intent.approval_id, scope_value=slot_id)
    store.apply(first.intent.approval_id, scope_value=slot_id)

    with pytest.raises(WriteContractError) as exc:
        store.apply(stale.intent.approval_id, scope_value=slot_id)
    assert exc.value.code == "version_conflict"
    assert store.get_record(stale.intent.approval_id, scope_value=slot_id).state == "expired"
    assert len(_rows(db, "SELECT id FROM trades")) == 1


def test_rejected_intent_never_mutates(stack):
    store, db, slots = stack
    slot_id = slots["콴텍"]
    before = _state(db, slot_id)
    intent = _intent(store, slot_id=slot_id)
    store.submit(intent)
    assert store.reject(intent.intent.approval_id, scope_value=slot_id).state == "rejected"

    with pytest.raises(WriteContractError) as exc:
        store.apply(intent.intent.approval_id, scope_value=slot_id)
    assert exc.value.code == "approval_required"
    assert _state(db, slot_id) == before


def test_audit_excludes_paper_payload_and_raw_scope(stack):
    store, _, slots = stack
    slot_id = slots["콴텍"]
    intent = _intent(
        store,
        slot_id=slot_id,
        payload={
            "slot": slot_id,
            "ticker": "005930",
            "name": "비공개 종목명",
            "quantity": 1,
            "price": 80_000,
            "notes": "비공개 매매 메모",
        },
    )
    store.submit(intent)
    rendered = str(store.list_audit_events())

    assert "005930" not in rendered
    assert "비공개 종목명" not in rendered
    assert "비공개 매매 메모" not in rendered
    assert intent.intent.idempotency_key not in rendered
    assert len(store.list_audit_events()[0]["scope_hash"]) == 64


def test_store_rejects_generic_unscoped_paper_intent(stack):
    store, _, slots = stack
    slot_id = slots["콴텍"]
    generic = validate_write_intent(
        operation="paper.buy",
        request_id=str(uuid4()),
        approval_id=str(uuid4()),
        idempotency_key=f"paper-store-{uuid4().hex}",
        expected_version=0,
        payload={
            "slot": slot_id,
            "ticker": "005930",
            "name": "삼성전자",
            "quantity": 1,
            "price": 80_000,
        },
    )
    with pytest.raises(WriteContractError) as exc:
        store.submit(generic)
    assert exc.value.code == "invalid_paper_intent"
