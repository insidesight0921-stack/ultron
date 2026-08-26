from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

import private_watchlist_write_store as pwws
import watchlist_store
from private_write_contract import WriteContractError, validate_write_intent


def _intent(*, operation="watchlist.add", version=0, key=None, **payload):
    default_payload = {"ticker": "005930", "name": "삼성전자"}
    default_payload.update(payload)
    if operation == "watchlist.remove":
        default_payload.pop("name", None)
    return validate_write_intent(
        operation=operation,
        request_id=str(uuid4()),
        idempotency_key=key or f"watchlist-write-{uuid4().hex}",
        approval_id=str(uuid4()),
        expected_version=version,
        payload=default_payload,
    )


def _store(tmp_path):
    return pwws.WatchlistWriteStore(
        tmp_path / "isolated" / "assistant.db",
        writes_enabled=True,
    )


def test_writes_are_disabled_by_default_without_creating_database(tmp_path):
    db = tmp_path / "assistant.db"
    store = pwws.WatchlistWriteStore(db)

    with pytest.raises(WriteContractError) as exc:
        store.submit(_intent())

    assert exc.value.code == "writes_disabled"
    assert not db.exists()


def test_submit_and_approve_do_not_mutate_watchlist(tmp_path):
    store = _store(tmp_path)
    intent = _intent()

    pending = store.submit(intent)
    approved = store.approve(intent.approval_id)

    assert pending.state == "pending"
    assert approved.state == "approved"
    assert watchlist_store.list_items(db_path=store.db_path) == []
    assert store.current_version() == 0


def test_preflight_new_request_returns_current_version_without_audit(tmp_path):
    store = _store(tmp_path)
    intent = _intent()

    preflight = store.preflight(
        operation=intent.operation,
        request_id=intent.request_id,
        approval_id=intent.approval_id,
        idempotency_key=intent.idempotency_key,
    )

    assert preflight.exists is False
    assert preflight.expected_version == 0
    assert preflight.current_version == 0
    assert preflight.state is None
    assert preflight.result is None
    assert store.list_audit_events() == []


def test_preflight_existing_intent_restores_original_version_after_other_write(tmp_path):
    store = _store(tmp_path)
    pending = _intent(version=0, ticker="000660", name="SK하이닉스")
    applied = _intent(version=0)
    store.submit(pending)
    store.submit(applied)
    store.approve(applied.approval_id)
    store.apply(applied.approval_id)

    preflight = store.preflight(
        operation=pending.operation,
        request_id=pending.request_id,
        approval_id=pending.approval_id,
        idempotency_key=pending.idempotency_key,
    )

    assert preflight.exists is True
    assert preflight.state == "pending"
    assert preflight.expected_version == 0
    assert preflight.current_version == 1
    assert preflight.result is None


def test_preflight_applied_intent_returns_saved_result(tmp_path):
    store = _store(tmp_path)
    intent = _intent()
    store.submit(intent)
    store.approve(intent.approval_id)
    applied = store.apply(intent.approval_id)

    preflight = store.preflight(
        operation=intent.operation,
        request_id=intent.request_id,
        approval_id=intent.approval_id,
        idempotency_key=intent.idempotency_key,
    )

    assert preflight.exists is True
    assert preflight.state == "applied"
    assert preflight.expected_version == 0
    assert preflight.current_version == 1
    assert preflight.result == applied.result


def test_preflight_rejects_identity_or_operation_mismatch(tmp_path):
    store = _store(tmp_path)
    intent = _intent()
    store.submit(intent)

    with pytest.raises(WriteContractError) as identity_conflict:
        store.preflight(
            operation=intent.operation,
            request_id=str(uuid4()),
            approval_id=intent.approval_id,
            idempotency_key=intent.idempotency_key,
        )
    assert identity_conflict.value.code == "identifier_conflict"

    with pytest.raises(WriteContractError) as operation_conflict:
        store.preflight(
            operation="watchlist.remove",
            request_id=intent.request_id,
            approval_id=intent.approval_id,
            idempotency_key=intent.idempotency_key,
        )
    assert operation_conflict.value.code == "idempotency_conflict"


def test_apply_adds_once_and_increments_version_once(tmp_path):
    store = _store(tmp_path)
    intent = _intent()
    store.submit(intent)
    store.approve(intent.approval_id)

    applied = store.apply(intent.approval_id)
    repeated = store.apply(intent.approval_id)

    assert applied.state == "applied"
    assert applied.result["ticker"] == "005930"
    assert applied.result["created"] is True
    assert applied.resource_version == 1
    assert repeated.replayed is True
    assert repeated.result == applied.result
    assert store.current_version() == 1
    assert [(item.ticker, item.name) for item in watchlist_store.list_items(db_path=store.db_path)] == [
        ("005930", "삼성전자")
    ]


def test_same_idempotency_key_replays_saved_result_without_second_write(tmp_path):
    store = _store(tmp_path)
    intent = _intent(key="watchlist-same-key-0001")
    store.submit(intent)
    store.approve(intent.approval_id)
    applied = store.apply(intent.approval_id)

    replayed = store.submit(intent)

    assert replayed.replayed is True
    assert replayed.state == "applied"
    assert replayed.result == applied.result
    assert store.current_version() == 1
    assert len(watchlist_store.list_items(db_path=store.db_path)) == 1
    assert [event["result"] for event in store.list_audit_events()] == [
        "pending",
        "approved",
        "applied",
        "replayed",
    ]


def test_same_idempotency_key_with_changed_payload_is_conflict(tmp_path):
    store = _store(tmp_path)
    key = "watchlist-conflict-0001"
    first = _intent(key=key)
    changed = validate_write_intent(
        operation="watchlist.add",
        request_id=first.request_id,
        idempotency_key=key,
        approval_id=first.approval_id,
        expected_version=0,
        payload={"ticker": "000660", "name": "SK하이닉스"},
    )
    store.submit(first)

    with pytest.raises(WriteContractError) as exc:
        store.submit(changed)

    assert exc.value.code == "idempotency_conflict"
    assert watchlist_store.list_items(db_path=store.db_path) == []
    assert store.list_audit_events()[-1]["error_code"] == "idempotency_conflict"


def test_stale_expected_version_expires_without_mutation(tmp_path):
    store = _store(tmp_path)
    first = _intent(version=0)
    stale = _intent(version=0, ticker="000660", name="SK하이닉스")
    store.submit(first)
    store.submit(stale)
    store.approve(first.approval_id)
    store.approve(stale.approval_id)
    store.apply(first.approval_id)

    with pytest.raises(WriteContractError) as exc:
        store.apply(stale.approval_id)

    assert exc.value.code == "version_conflict"
    assert store.get_record(stale.approval_id).state == "expired"
    assert store.current_version() == 1
    assert [item.ticker for item in watchlist_store.list_items(db_path=store.db_path)] == ["005930"]
    assert store.list_audit_events()[-1]["error_code"] == "version_conflict"


def test_remove_uses_same_approval_and_version_contract(tmp_path):
    store = _store(tmp_path)
    add = _intent(version=0)
    store.submit(add)
    store.approve(add.approval_id)
    store.apply(add.approval_id)
    remove = _intent(operation="watchlist.remove", version=1)
    store.submit(remove)
    store.approve(remove.approval_id)

    result = store.apply(remove.approval_id)

    assert result.result == {"ticker": "005930", "removed": True}
    assert result.resource_version == 2
    assert watchlist_store.list_items(db_path=store.db_path) == []


def test_domain_noop_does_not_advance_resource_version(tmp_path):
    store = _store(tmp_path)
    first = _intent(version=0)
    store.submit(first)
    store.approve(first.approval_id)
    store.apply(first.approval_id)
    duplicate = _intent(version=1)
    store.submit(duplicate)
    store.approve(duplicate.approval_id)

    result = store.apply(duplicate.approval_id)

    assert result.result["created"] is False
    assert result.resource_version == 1
    assert store.current_version() == 1
    assert len(watchlist_store.list_items(db_path=store.db_path)) == 1


def test_rejected_intent_cannot_be_applied(tmp_path):
    store = _store(tmp_path)
    intent = _intent()
    store.submit(intent)
    rejected = store.reject(intent.approval_id)

    with pytest.raises(WriteContractError) as exc:
        store.apply(intent.approval_id)

    assert rejected.state == "rejected"
    assert exc.value.code == "approval_required"
    assert watchlist_store.list_items(db_path=store.db_path) == []
    assert store.list_audit_events()[-1]["error_code"] == "approval_required"


def test_audit_table_has_no_payload_or_raw_idempotency_key(tmp_path):
    store = _store(tmp_path)
    raw_key = "watchlist-audit-key-0001"
    intent = _intent(key=raw_key)
    store.submit(intent)
    store.approve(intent.approval_id)
    store.apply(intent.approval_id)

    events = store.list_audit_events()
    hashes = {event["idempotency_key_hash"] for event in events}
    assert all(set(event) <= {
        "request_id",
        "approval_id",
        "idempotency_key_hash",
        "domain",
        "action",
        "result",
        "occurred_at",
        "resource_version",
        "error_code",
    } for event in events)
    assert raw_key not in str(events)
    assert len(hashes) == 1
    assert "005930" not in str(events)
    assert "삼성전자" not in str(events)

    with sqlite3.connect(store.db_path) as con:
        columns = [row[1] for row in con.execute("PRAGMA table_info(private_write_audit)")]
    assert "payload_json" not in columns
    assert "idempotency_key" not in columns


def test_non_watchlist_operation_is_rejected(tmp_path):
    store = _store(tmp_path)
    intent = validate_write_intent(
        operation="schedule.delete",
        request_id=str(uuid4()),
        idempotency_key="schedule-delete-0001",
        approval_id=str(uuid4()),
        expected_version=0,
        payload={"event_id": 1},
    )

    with pytest.raises(WriteContractError) as exc:
        store.submit(intent)

    assert exc.value.code == "operation_not_allowed"
    assert not store.db_path.exists()
