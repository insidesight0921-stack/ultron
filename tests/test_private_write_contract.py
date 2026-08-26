from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

import private_write_contract as pwc
from private_data_api import create_app


def _intent(**overrides):
    values = {
        "operation": "watchlist.add",
        "request_id": str(uuid4()),
        "idempotency_key": "watchlist-add-0001",
        "approval_id": str(uuid4()),
        "expected_version": 3,
        "payload": {"ticker": "005930", "name": "삼성전자"},
    }
    values.update(overrides)
    return pwc.validate_write_intent(**values)


def test_operation_allowlist_covers_only_planned_private_domains():
    assert set(pwc.OPERATION_SPECS) == {
        "watchlist.add",
        "watchlist.remove",
        "schedule.add",
        "schedule.delete",
        "schedule.complete",
        "paper.buy",
        "paper.sell",
        "paper_ipo.subscribe",
        "paper_ipo.close",
    }
    assert all(spec.approval_required for spec in pwc.OPERATION_SPECS.values())


def test_valid_intent_normalizes_ids_and_has_stable_fingerprint():
    request_id = str(uuid4()).upper()
    approval_id = str(uuid4()).upper()
    first = _intent(request_id=request_id, approval_id=approval_id)
    second = _intent(
        request_id=request_id,
        approval_id=approval_id,
        payload={"name": "삼성전자", "ticker": "005930"},
    )

    assert first.request_id == request_id.lower()
    assert first.approval_id == approval_id.lower()
    assert first.fingerprint == second.fingerprint


@pytest.mark.parametrize("operation", ["sqlite.execute", "paper.drop", "", None])
def test_unknown_operation_is_rejected(operation):
    with pytest.raises(pwc.WriteContractError, match="allowlisted") as exc:
        _intent(operation=operation)
    assert exc.value.code == "operation_not_allowed"


def test_missing_and_unknown_payload_fields_are_rejected():
    with pytest.raises(pwc.WriteContractError) as missing:
        _intent(payload={"ticker": "005930"})
    assert missing.value.code == "missing_field"

    with pytest.raises(pwc.WriteContractError) as unknown:
        _intent(payload={"ticker": "005930", "name": "삼성전자", "source": "private"})
    assert unknown.value.code == "unknown_field"


@pytest.mark.parametrize("field", ["token", "sql", "db_path", "table_name"])
def test_sensitive_nested_payload_fields_are_rejected(field):
    with pytest.raises(pwc.WriteContractError) as exc:
        pwc.validate_write_intent(
            operation="paper_ipo.subscribe",
            request_id=str(uuid4()),
            idempotency_key="paper-ipo-sub-0001",
            approval_id=str(uuid4()),
            expected_version=0,
            payload={"name": "테스트", "subscribed": True, "factors": {field: "blocked"}},
        )
    assert exc.value.code == "forbidden_field"


def test_non_string_payload_key_is_rejected():
    with pytest.raises(pwc.WriteContractError) as exc:
        _intent(payload={"ticker": "005930", "name": "삼성전자", 1: "blocked"})
    assert exc.value.code == "invalid_payload"


@pytest.mark.parametrize("key", ["short", "contains space 001", "slash/not/allowed/001"])
def test_invalid_idempotency_key_is_rejected(key):
    with pytest.raises(pwc.WriteContractError) as exc:
        _intent(idempotency_key=key)
    assert exc.value.code == "invalid_idempotency_key"


def test_idempotency_replay_requires_same_request_fingerprint():
    intent = _intent()
    assert pwc.validate_idempotency_replay(intent.fingerprint, intent) == "replayed"

    changed = pwc.WriteIntent(
        operation=intent.operation,
        request_id=intent.request_id,
        idempotency_key=intent.idempotency_key,
        approval_id=intent.approval_id,
        expected_version=intent.expected_version,
        payload={"ticker": "000660", "name": "SK하이닉스"},
    )
    with pytest.raises(pwc.WriteContractError) as exc:
        pwc.validate_idempotency_replay(intent.fingerprint, changed)
    assert exc.value.code == "idempotency_conflict"


def test_approval_state_machine_allows_one_apply_only():
    assert pwc.validate_approval_transition("pending", "approved") == ("pending", "approved")
    assert pwc.validate_approval_transition("approved", "applied") == ("approved", "applied")
    with pytest.raises(pwc.WriteContractError) as repeated:
        pwc.validate_approval_transition("applied", "applied")
    assert repeated.value.code == "invalid_approval_transition"
    with pytest.raises(pwc.WriteContractError):
        pwc.validate_approval_transition("rejected", "approved")


def test_audit_event_excludes_payload_token_and_raw_idempotency_key():
    intent = _intent()
    event = pwc.build_audit_event(
        intent,
        result="applied",
        occurred_at=datetime(2026, 8, 24, 1, 2, 3, tzinfo=timezone.utc),
        resource_version=4,
    )

    assert set(event) == {
        "request_id",
        "approval_id",
        "idempotency_key_hash",
        "domain",
        "action",
        "result",
        "occurred_at",
        "resource_version",
    }
    assert intent.idempotency_key not in str(event)
    assert "005930" not in str(event)
    assert len(event["idempotency_key_hash"]) == 64


def test_private_api_still_has_no_mutation_routes():
    app = create_app("x" * 64)
    mutation_methods = {"POST", "PUT", "PATCH", "DELETE"}
    exposed = {
        (route.path, method)
        for route in app.routes
        for method in (route.methods or set())
        if method in mutation_methods
    }
    assert exposed == set()
