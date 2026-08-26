from __future__ import annotations

from uuid import uuid4

import pytest

from private_schedule_write_contract import (
    normalize_schedule_chat_id,
    schedule_audit_identity,
    validate_schedule_write_intent,
)
from private_write_contract import WriteContractError


def _intent(**overrides):
    values = {
        "operation": "schedule.add",
        "chat_id": "-1001234567890",
        "request_id": str(uuid4()),
        "idempotency_key": "schedule-write-contract-0001",
        "approval_id": str(uuid4()),
        "expected_version": 0,
        "payload": {
            "title": "주간 회의",
            "when_at": "2026-09-01T10:00:00",
            "notes": "의제 확인",
            "rrule_freq": "weekly",
            "rrule_byday": "MO,WE,MO",
            "rrule_until": "2026-12-01T10:00:00",
            "pre_notify_minutes": [5, 30, 5],
        },
    }
    values.update(overrides)
    return validate_schedule_write_intent(**values)


def test_schedule_add_matches_router_rrule_and_pre_notify_shape():
    result = _intent()

    assert result.intent.payload == {
        "title": "주간 회의",
        "when_at": "2026-09-01T10:00:00",
        "notes": "의제 확인",
        "rrule_freq": "weekly",
        "rrule_byday": "MO,WE",
        "rrule_until": "2026-12-01T10:00:00",
        "pre_notify_minutes": [30, 5],
    }


@pytest.mark.parametrize("operation", ["schedule.delete", "schedule.complete"])
def test_event_mutations_require_one_positive_event_id(operation):
    result = _intent(operation=operation, payload={"event_id": 7})
    assert result.intent.payload == {"event_id": 7}

    with pytest.raises(WriteContractError) as exc:
        _intent(operation=operation, payload={"event_id": 0})
    assert exc.value.code == "invalid_schedule_event"


@pytest.mark.parametrize("chat_id", ["123", "-1001234567890", 7004216259])
def test_chat_scope_accepts_only_nonzero_signed_integer_ids(chat_id):
    assert normalize_schedule_chat_id(chat_id) == str(chat_id)


@pytest.mark.parametrize("chat_id", [None, "", "abc", "1.5", 0, 2**63])
def test_invalid_chat_scope_is_rejected(chat_id):
    with pytest.raises(WriteContractError) as exc:
        normalize_schedule_chat_id(chat_id)
    assert exc.value.code == "invalid_schedule_scope"


def test_same_generic_intent_in_different_chat_has_different_fingerprint():
    request_id = str(uuid4())
    approval_id = str(uuid4())
    first = _intent(request_id=request_id, approval_id=approval_id, chat_id="111")
    second = _intent(request_id=request_id, approval_id=approval_id, chat_id="222")

    assert first.intent.fingerprint == second.intent.fingerprint
    assert first.scope_hash != second.scope_hash
    assert first.fingerprint != second.fingerprint


def test_repr_and_audit_identity_do_not_expose_chat_or_payload():
    result = _intent(chat_id="7004216259")
    audit = schedule_audit_identity(result)

    assert "7004216259" not in repr(result)
    assert "7004216259" not in str(audit)
    assert "주간 회의" not in str(audit)
    assert set(audit) == {"request_fingerprint", "scope_hash", "payload_hash"}


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"title": "", "when_at": "2026-09-01T10:00:00"}, "invalid_schedule_title"),
        ({"title": "회의", "when_at": "내일 오전"}, "invalid_schedule_time"),
        (
            {"title": "회의", "when_at": "2026-09-01T10:00:00+09:00"},
            "invalid_schedule_time",
        ),
        (
            {
                "title": "회의",
                "when_at": "2026-09-01T10:00:00",
                "rrule_byday": "MO",
            },
            "invalid_schedule_rrule",
        ),
        (
            {
                "title": "회의",
                "when_at": "2026-09-01T10:00:00",
                "pre_notify_minutes": [],
            },
            "invalid_schedule_pre_notify",
        ),
        (
            {
                "title": "회의",
                "when_at": "2026-09-01T10:00:00",
                "repeat_rule": "weekly",
            },
            "unknown_field",
        ),
    ],
)
def test_invalid_schedule_add_payload_is_rejected(payload, code):
    with pytest.raises(WriteContractError) as exc:
        _intent(payload=payload)
    assert exc.value.code == code


def test_schedule_contract_rejects_other_private_domains():
    with pytest.raises(WriteContractError) as exc:
        _intent(operation="watchlist.add", payload={"ticker": "005930", "name": "삼성전자"})
    assert exc.value.code == "operation_not_allowed"
