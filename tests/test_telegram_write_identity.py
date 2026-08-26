from __future__ import annotations

from dataclasses import replace

import pytest

import telegram_write_identity as twi
import watchlist_bot as wb


def _identity(**overrides):
    values = {
        "update_id": 7001,
        "chat_id": -100123456789,
        "user_id": 123456,
        "message_id": 3001,
        "action": "add",
    }
    values.update(overrides)
    return twi.build_watchlist_write_identity(**values)


def _schedule_identity(**overrides):
    values = {
        "update_id": 8001,
        "chat_id": -100987654321,
        "user_id": 654321,
        "message_id": 4001,
        "action": "add",
    }
    values.update(overrides)
    return twi.build_schedule_write_identity(**values)


def test_same_telegram_update_produces_same_write_identity():
    first = _identity()
    second = _identity()

    assert first == second
    assert first.operation == "watchlist.add"
    assert first.idempotency_key.startswith("tg-watchlist-")
    assert len(first.idempotency_key) == 61


@pytest.mark.parametrize(
    "field,value",
    [
        ("update_id", 7002),
        ("chat_id", -100123456788),
        ("user_id", 123457),
        ("message_id", 3002),
        ("action", "remove"),
    ],
)
def test_any_source_coordinate_change_produces_new_identity(field, value):
    original = _identity()
    changed = _identity(**{field: value})

    assert changed.request_id != original.request_id
    assert changed.approval_id != original.approval_id
    assert changed.idempotency_key != original.idempotency_key


def test_identity_contains_no_plaintext_telegram_coordinates():
    identity = _identity()
    rendered = str(identity)

    for private_value in ("7001", "-100123456789", "123456", "3001"):
        assert private_value not in rendered


@pytest.mark.parametrize(
    "overrides",
    [
        {"update_id": -1},
        {"chat_id": 0},
        {"user_id": 0},
        {"message_id": 0},
        {"action": "list"},
        {"update_id": "7.1"},
        {"message_id": 2**63},
    ],
)
def test_invalid_telegram_source_is_rejected(overrides):
    with pytest.raises(ValueError):
        _identity(**overrides)


def test_watchlist_run_accepts_matching_identity_without_changing_direct_behavior(
    monkeypatch,
    tmp_path,
):
    identity = _identity()
    monkeypatch.setattr(wb, "resolve_ticker", lambda query: ("005930", "삼성전자"))

    answer, _ = wb.run(
        "add",
        "삼성전자",
        db_path=tmp_path / "assistant.db",
        write_identity=identity,
    )

    assert "추가" in answer
    assert "005930" in answer


def test_watchlist_run_rejects_identity_for_different_operation(tmp_path):
    identity = _identity(action="remove")

    with pytest.raises(ValueError, match="does not match"):
        wb.run(
            "add",
            "삼성전자",
            db_path=tmp_path / "assistant.db",
            write_identity=identity,
        )


def test_tampered_identity_is_rejected():
    identity = replace(_identity(), request_id="not-a-uuid")
    with pytest.raises(ValueError, match="UUID"):
        twi.validate_watchlist_write_identity(identity, action="add")


def test_same_schedule_update_produces_same_non_plaintext_identity():
    first = _schedule_identity()
    second = _schedule_identity()

    assert first == second
    assert first.operation == "schedule.add"
    assert first.idempotency_key.startswith("tg-schedule-")
    rendered = repr(first)
    for private_value in ("8001", "-100987654321", "654321", "4001"):
        assert private_value not in rendered


@pytest.mark.parametrize(
    "field,value",
    [
        ("update_id", 8002),
        ("chat_id", -100987654322),
        ("user_id", 654322),
        ("message_id", 4002),
        ("action", "delete"),
        ("action", "complete"),
    ],
)
def test_schedule_source_or_action_change_produces_new_identity(field, value):
    original = _schedule_identity()
    changed = _schedule_identity(**{field: value})

    assert changed.request_id != original.request_id
    assert changed.approval_id != original.approval_id
    assert changed.idempotency_key != original.idempotency_key


def test_schedule_and_watchlist_domains_do_not_share_identity():
    coordinates = {
        "update_id": 7001,
        "chat_id": -100123456789,
        "user_id": 123456,
        "message_id": 3001,
        "action": "add",
    }
    watchlist = twi.build_watchlist_write_identity(**coordinates)
    schedule = twi.build_schedule_write_identity(**coordinates)

    assert schedule.request_id != watchlist.request_id
    assert schedule.approval_id != watchlist.approval_id
    assert schedule.idempotency_key != watchlist.idempotency_key


@pytest.mark.parametrize(
    "overrides",
    [
        {"update_id": -1},
        {"chat_id": 0},
        {"user_id": 0},
        {"message_id": 0},
        {"action": "list"},
        {"action": "remove"},
    ],
)
def test_invalid_schedule_source_is_rejected(overrides):
    with pytest.raises(ValueError):
        _schedule_identity(**overrides)


def test_schedule_identity_validation_rejects_mismatch_and_tampering():
    identity = _schedule_identity(action="delete")
    with pytest.raises(ValueError, match="does not match"):
        twi.validate_schedule_write_identity(identity, action="complete")
    tampered = replace(identity, idempotency_key="bad key")
    with pytest.raises(ValueError):
        twi.validate_schedule_write_identity(tampered, action="delete")
