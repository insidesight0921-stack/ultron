from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from private_schedule_write_contract import validate_schedule_write_intent
from private_schedule_write_store import ScheduleWriteStore
from private_write_contract import WriteContractError, validate_write_intent


CHAT_A = "111"
CHAT_B = "222"


def _intent(
    store: ScheduleWriteStore,
    *,
    chat_id: str = CHAT_A,
    operation: str = "schedule.add",
    payload: dict[str, object] | None = None,
    expected_version: int | None = None,
    request_id: str | None = None,
    approval_id: str | None = None,
    idempotency_key: str | None = None,
):
    if payload is None:
        payload = {"title": "격리 일정", "when_at": "2026-09-01T10:00:00"}
    return validate_schedule_write_intent(
        operation=operation,
        chat_id=chat_id,
        request_id=request_id or str(uuid4()),
        approval_id=approval_id or str(uuid4()),
        idempotency_key=idempotency_key or f"schedule-store-{uuid4().hex}",
        expected_version=(
            store.current_version(chat_id) if expected_version is None else expected_version
        ),
        payload=payload,
    )


def _events(db, chat_id):
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        return con.execute(
            "SELECT id,title,completed,chat_id FROM events WHERE chat_id=? ORDER BY id",
            (chat_id,),
        ).fetchall()


def test_disabled_store_creates_no_database(tmp_path):
    db = tmp_path / "disabled.db"
    store = ScheduleWriteStore(db)
    with pytest.raises(WriteContractError) as exc:
        store.current_version(CHAT_A)
    assert exc.value.code == "writes_disabled"
    assert not db.exists()


def test_add_mutates_only_after_approval_and_increments_chat_version(tmp_path):
    db = tmp_path / "schedule.db"
    store = ScheduleWriteStore(db, writes_enabled=True)
    intent = _intent(
        store,
        payload={
            "title": "주간 회의",
            "when_at": "2026-09-01T10:00:00",
            "rrule_freq": "weekly",
            "rrule_byday": "MO,WE",
            "pre_notify_minutes": [30, 5],
        },
    )

    pending = store.submit(intent)
    assert pending.state == "pending"
    assert _events(db, CHAT_A) == []
    approved = store.approve(
        intent.intent.approval_id,
        request_id=intent.intent.request_id,
        scope_value=CHAT_A,
    )
    assert approved.state == "approved"
    assert _events(db, CHAT_A) == []
    applied = store.apply(
        intent.intent.approval_id,
        request_id=intent.intent.request_id,
        scope_value=CHAT_A,
    )

    assert applied.state == "applied"
    assert applied.result["created"] is True
    assert applied.result["pre_notify_minutes_list"] == [30, 5]
    assert len(_events(db, CHAT_A)) == 1
    assert store.current_version(CHAT_A) == 1
    assert store.current_version(CHAT_B) == 0


def test_submit_and_apply_replay_do_not_duplicate_event_or_version(tmp_path):
    db = tmp_path / "schedule.db"
    store = ScheduleWriteStore(db, writes_enabled=True)
    intent = _intent(store)
    store.submit(intent)
    store.approve(intent.intent.approval_id, scope_value=CHAT_A)
    first = store.apply(intent.intent.approval_id, scope_value=CHAT_A)
    apply_replay = store.apply(intent.intent.approval_id, scope_value=CHAT_A)
    submit_replay = store.submit(intent)

    assert first.replayed is False
    assert apply_replay.replayed is True
    assert submit_replay.replayed is True
    assert len(_events(db, CHAT_A)) == 1
    assert store.current_version(CHAT_A) == 1


def test_cross_chat_cannot_preflight_approve_or_apply_intent(tmp_path):
    store = ScheduleWriteStore(tmp_path / "schedule.db", writes_enabled=True)
    intent = _intent(store, chat_id=CHAT_A)
    store.submit(intent)

    with pytest.raises(WriteContractError) as preflight:
        store.preflight(
            operation="schedule.add",
            scope_value=CHAT_B,
            request_id=intent.intent.request_id,
            approval_id=intent.intent.approval_id,
            idempotency_key=intent.intent.idempotency_key,
        )
    assert preflight.value.code == "scope_conflict"
    with pytest.raises(WriteContractError) as approve:
        store.approve(intent.intent.approval_id, scope_value=CHAT_B)
    assert approve.value.code == "scope_conflict"
    with pytest.raises(WriteContractError) as apply:
        store.apply(intent.intent.approval_id, scope_value=CHAT_B)
    assert apply.value.code == "scope_conflict"
    assert store.get_record(intent.intent.approval_id, scope_value=CHAT_A).state == "pending"


def test_same_idempotency_key_in_another_chat_is_conflict(tmp_path):
    store = ScheduleWriteStore(tmp_path / "schedule.db", writes_enabled=True)
    key = f"schedule-store-{uuid4().hex}"
    first = _intent(store, chat_id=CHAT_A, idempotency_key=key)
    second = _intent(store, chat_id=CHAT_B, idempotency_key=key)
    store.submit(first)

    with pytest.raises(WriteContractError) as exc:
        store.submit(second)
    assert exc.value.code == "idempotency_conflict"


def test_delete_is_scoped_and_noop_does_not_increment_other_chat(tmp_path):
    db = tmp_path / "schedule.db"
    store = ScheduleWriteStore(db, writes_enabled=True)
    add = _intent(store, chat_id=CHAT_A)
    store.submit(add)
    store.approve(add.intent.approval_id, scope_value=CHAT_A)
    event_id = store.apply(add.intent.approval_id, scope_value=CHAT_A).result["id"]

    wrong = _intent(
        store,
        chat_id=CHAT_B,
        operation="schedule.delete",
        payload={"event_id": event_id},
    )
    store.submit(wrong)
    store.approve(wrong.intent.approval_id, scope_value=CHAT_B)
    wrong_result = store.apply(wrong.intent.approval_id, scope_value=CHAT_B)
    assert wrong_result.result == {"event_id": event_id, "deleted": False}
    assert store.current_version(CHAT_B) == 0
    assert len(_events(db, CHAT_A)) == 1

    delete = _intent(
        store,
        chat_id=CHAT_A,
        operation="schedule.delete",
        payload={"event_id": event_id},
    )
    store.submit(delete)
    store.approve(delete.intent.approval_id, scope_value=CHAT_A)
    deleted = store.apply(delete.intent.approval_id, scope_value=CHAT_A)
    assert deleted.result == {"event_id": event_id, "deleted": True}
    assert store.current_version(CHAT_A) == 2
    assert _events(db, CHAT_A) == []


def test_complete_is_scoped_and_second_completion_is_noop(tmp_path):
    db = tmp_path / "schedule.db"
    store = ScheduleWriteStore(db, writes_enabled=True)
    add = _intent(store)
    store.submit(add)
    store.approve(add.intent.approval_id, scope_value=CHAT_A)
    event_id = store.apply(add.intent.approval_id, scope_value=CHAT_A).result["id"]

    complete = _intent(
        store,
        operation="schedule.complete",
        payload={"event_id": event_id},
    )
    store.submit(complete)
    store.approve(complete.intent.approval_id, scope_value=CHAT_A)
    result = store.apply(complete.intent.approval_id, scope_value=CHAT_A)
    assert result.result == {"event_id": event_id, "completed": True}
    assert store.current_version(CHAT_A) == 2

    again = _intent(
        store,
        operation="schedule.complete",
        payload={"event_id": event_id},
    )
    store.submit(again)
    store.approve(again.intent.approval_id, scope_value=CHAT_A)
    noop = store.apply(again.intent.approval_id, scope_value=CHAT_A)
    assert noop.result == {"event_id": event_id, "completed": False}
    assert store.current_version(CHAT_A) == 2


def test_stale_intent_expires_without_mutating(tmp_path):
    db = tmp_path / "schedule.db"
    store = ScheduleWriteStore(db, writes_enabled=True)
    first = _intent(store)
    stale = _intent(store)
    store.submit(first)
    store.submit(stale)
    store.approve(first.intent.approval_id, scope_value=CHAT_A)
    store.approve(stale.intent.approval_id, scope_value=CHAT_A)
    store.apply(first.intent.approval_id, scope_value=CHAT_A)

    with pytest.raises(WriteContractError) as exc:
        store.apply(stale.intent.approval_id, scope_value=CHAT_A)
    assert exc.value.code == "version_conflict"
    assert store.get_record(stale.intent.approval_id, scope_value=CHAT_A).state == "expired"
    assert len(_events(db, CHAT_A)) == 1


def test_rejected_intent_never_mutates(tmp_path):
    db = tmp_path / "schedule.db"
    store = ScheduleWriteStore(db, writes_enabled=True)
    intent = _intent(store)
    store.submit(intent)
    rejected = store.reject(intent.intent.approval_id, scope_value=CHAT_A)
    assert rejected.state == "rejected"
    with pytest.raises(WriteContractError) as exc:
        store.apply(intent.intent.approval_id, scope_value=CHAT_A)
    assert exc.value.code == "approval_required"
    assert _events(db, CHAT_A) == []
    assert store.current_version(CHAT_A) == 0


def test_audit_exposes_hashes_but_not_chat_or_schedule_payload(tmp_path):
    store = ScheduleWriteStore(tmp_path / "schedule.db", writes_enabled=True)
    intent = _intent(
        store,
        chat_id="7004216259",
        payload={"title": "비공개 회의", "when_at": "2026-09-01T10:00:00"},
    )
    store.submit(intent)
    events = store.list_audit_events()
    rendered = str(events)

    assert "7004216259" not in rendered
    assert "비공개 회의" not in rendered
    assert intent.intent.idempotency_key not in rendered
    assert events[0]["domain"] == "schedule"
    assert len(events[0]["scope_hash"]) == 64


def test_store_rejects_unscoped_generic_intent(tmp_path):
    store = ScheduleWriteStore(tmp_path / "schedule.db", writes_enabled=True)
    generic = validate_write_intent(
        operation="schedule.delete",
        request_id=str(uuid4()),
        approval_id=str(uuid4()),
        idempotency_key=f"schedule-store-{uuid4().hex}",
        expected_version=0,
        payload={"event_id": 1},
    )
    with pytest.raises(WriteContractError) as exc:
        store.submit(generic)
    assert exc.value.code == "invalid_schedule_intent"
