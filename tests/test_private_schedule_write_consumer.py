from __future__ import annotations

import sqlite3
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from private_data_api import create_app
from private_data_api_client import PrivateAPIWriteConflict, PrivateDataClient
from private_schedule_write_consumer import (
    ScheduleApprovalRequired,
    ScheduleConsumerDisabled,
    ScheduleExecutionResult,
    ScheduleTerminalState,
    ScheduleWriteExecutor,
)
from private_schedule_write_store import ScheduleWriteStore
from telegram_write_identity import build_schedule_write_identity
import schedule_bot as sb


TOKEN = "private-schedule-consumer-token-32-characters-minimum"
CHAT_A = "111"
CHAT_B = "222"


class _BridgeResponse:
    def __init__(self, response):
        self.headers = response.headers
        self.content = response.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.content if limit < 0 else self.content[:limit]


class _Opener:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def __call__(self, request, timeout):
        parsed = urlsplit(request.full_url)
        self.calls.append((request.get_method(), parsed.path))
        response = self.client.request(
            request.get_method(),
            parsed.path,
            headers=dict(request.header_items()),
            content=request.data,
        )
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url,
                response.status_code,
                "Private API error",
                response.headers,
                BytesIO(response.content),
            )
        return _BridgeResponse(response)


@pytest.fixture
def consumer_stack(tmp_path, private_write_permit_factory):
    db = tmp_path / "schedule-consumer" / "assistant.db"
    writer = ScheduleWriteStore(db, writes_enabled=True)
    permit = private_write_permit_factory(db)
    app = create_app(
        TOKEN,
        schedule_writer=writer,
        enable_schedule_writes=True,
        write_activation_permit=permit,
    )
    opener = _Opener(TestClient(app))
    client = PrivateDataClient(
        token=TOKEN,
        opener=opener,
        writes_enabled=True,
        write_activation_permit=permit,
    )
    executor = ScheduleWriteExecutor(
        client,
        enabled=True,
        write_activation_permit=permit,
    )
    return executor, client, writer, opener


def _identity(*, update_id=8001, action="add", chat_id=CHAT_A):
    return build_schedule_write_identity(
        update_id=update_id,
        chat_id=chat_id,
        user_id=654321,
        message_id=4001 + update_id,
        action=action,
    )


def _execute_add(
    executor,
    identity=None,
    *,
    chat_id=CHAT_A,
    title="소비자 주간 일정",
    user_approved=True,
):
    return executor.execute(
        action="add",
        chat_id=chat_id,
        identity=identity or _identity(chat_id=chat_id),
        user_approved=user_approved,
        title=title,
        when_at="2026-09-01T10:00:00",
        rrule_freq="weekly",
        rrule_byday="MO,WE",
        pre_notify_minutes=[30, 5],
    )


def _event_count(db, chat_id=CHAT_A):
    with sqlite3.connect(db) as con:
        return con.execute(
            "SELECT COUNT(*) FROM events WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]


def _identity_args(identity, chat_id=CHAT_A):
    return {
        "operation": identity.operation,
        "chat_id": chat_id,
        "request_id": identity.request_id,
        "approval_id": identity.approval_id,
        "idempotency_key": identity.idempotency_key,
    }


def test_schedule_executor_is_disabled_by_default_before_client_call():
    class Client:
        writes_enabled = True

        def preflight_schedule_write(self, **kwargs):
            raise AssertionError("must not call client")

    executor = ScheduleWriteExecutor(Client())
    with pytest.raises(ScheduleConsumerDisabled):
        _execute_add(executor)


def test_schedule_executor_requires_enabled_client_and_matching_permit(
    private_write_permit_factory,
):
    disabled_client = PrivateDataClient(token=TOKEN)
    with pytest.raises(ValueError, match="activation permit"):
        ScheduleWriteExecutor(
            disabled_client,
            enabled=True,
            write_activation_permit=private_write_permit_factory(),
        )

    client_permit = private_write_permit_factory()
    enabled_client = PrivateDataClient(
        token=TOKEN,
        writes_enabled=True,
        write_activation_permit=client_permit,
    )
    with pytest.raises(ValueError, match="do not match"):
        ScheduleWriteExecutor(
            enabled_client,
            enabled=True,
            write_activation_permit=private_write_permit_factory(),
        )


def test_schedule_executor_requires_explicit_approval_without_http(consumer_stack):
    executor, _, writer, opener = consumer_stack

    with pytest.raises(ScheduleApprovalRequired):
        _execute_add(executor, user_approved=False)

    assert opener.calls == []
    with sqlite3.connect(f"file:{writer.db_path}?mode=ro", uri=True) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'private_scoped_%'"
        ).fetchone()[0] == 0


def test_schedule_executor_rejects_bad_payload_before_http(consumer_stack):
    executor, _, writer, opener = consumer_stack

    with pytest.raises(ValueError):
        _execute_add(executor, title="")

    assert opener.calls == []
    with sqlite3.connect(f"file:{writer.db_path}?mode=ro", uri=True) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'private_scoped_%'"
        ).fetchone()[0] == 0


def test_schedule_executor_full_flow_and_applied_replay(consumer_stack):
    executor, _, writer, opener = consumer_stack
    identity = _identity()

    first = _execute_add(executor, identity)
    replay = _execute_add(executor, identity)

    assert first.state == "applied"
    assert first.replayed is False
    assert replay.state == "applied"
    assert replay.replayed is True
    assert replay.result == first.result
    assert writer.current_version(CHAT_A) == 1
    assert _event_count(writer.db_path) == 1
    assert [path for _, path in opener.calls].count(
        "/v1/private/schedule/write/approvals/apply"
    ) == 1


@pytest.mark.parametrize("partial_state", ["pending", "approved"])
def test_schedule_executor_resumes_partial_request(consumer_stack, partial_state):
    executor, client, writer, _ = consumer_stack
    identity = _identity()
    args = _identity_args(identity)
    preflight = client.preflight_schedule_write(**args)
    client.submit_schedule_write(
        **args,
        expected_version=preflight["expected_version"],
        payload={
            "title": "소비자 주간 일정",
            "when_at": "2026-09-01T10:00:00",
            "rrule_freq": "weekly",
            "rrule_byday": "MO,WE",
            "pre_notify_minutes": [30, 5],
        },
    )
    if partial_state == "approved":
        client.approve_schedule_write(
            chat_id=CHAT_A,
            request_id=identity.request_id,
            approval_id=identity.approval_id,
        )

    result = _execute_add(executor, identity)

    assert result.state == "applied"
    assert result.replayed is True
    assert writer.current_version(CHAT_A) == 1


def test_schedule_executor_refuses_rejected_intent(consumer_stack):
    executor, client, writer, _ = consumer_stack
    identity = _identity()
    args = _identity_args(identity)
    preflight = client.preflight_schedule_write(**args)
    client.submit_schedule_write(
        **args,
        expected_version=preflight["expected_version"],
        payload={"title": "거부 일정", "when_at": "2026-09-01T10:00:00"},
    )
    client.reject_schedule_write(
        chat_id=CHAT_A,
        request_id=identity.request_id,
        approval_id=identity.approval_id,
    )

    with pytest.raises(ScheduleTerminalState) as error:
        executor.execute(
            action="add",
            chat_id=CHAT_A,
            identity=identity,
            user_approved=True,
            title="거부 일정",
            when_at="2026-09-01T10:00:00",
        )

    assert error.value.state == "rejected"
    assert writer.current_version(CHAT_A) == 0
    assert _event_count(writer.db_path) == 0


def test_schedule_executor_detects_payload_drift(consumer_stack):
    executor, client, writer, _ = consumer_stack
    identity = _identity()
    args = _identity_args(identity)
    preflight = client.preflight_schedule_write(**args)
    client.submit_schedule_write(
        **args,
        expected_version=preflight["expected_version"],
        payload={"title": "원래 일정", "when_at": "2026-09-01T10:00:00"},
    )

    with pytest.raises(PrivateAPIWriteConflict) as error:
        executor.execute(
            action="add",
            chat_id=CHAT_A,
            identity=identity,
            user_approved=True,
            title="변경된 일정",
            when_at="2026-09-01T10:00:00",
        )

    assert error.value.code == "idempotency_conflict"
    assert writer.current_version(CHAT_A) == 0
    assert _event_count(writer.db_path) == 0


def test_schedule_executor_blocks_cross_chat_retry(consumer_stack):
    executor, client, writer, _ = consumer_stack
    identity = _identity(chat_id=CHAT_A)
    args = _identity_args(identity)
    preflight = client.preflight_schedule_write(**args)
    client.submit_schedule_write(
        **args,
        expected_version=preflight["expected_version"],
        payload={"title": "범위 일정", "when_at": "2026-09-01T10:00:00"},
    )

    with pytest.raises(PrivateAPIWriteConflict) as error:
        executor.execute(
            action="add",
            chat_id=CHAT_B,
            identity=identity,
            user_approved=True,
            title="범위 일정",
            when_at="2026-09-01T10:00:00",
        )

    assert error.value.code == "scope_conflict"
    assert writer.current_version(CHAT_A) == 0


def test_schedule_executor_complete_and_delete(consumer_stack):
    executor, _, writer, _ = consumer_stack
    added = _execute_add(executor)
    event_id = int(added.result["id"])

    completed = executor.execute(
        action="complete",
        chat_id=CHAT_A,
        identity=_identity(update_id=8002, action="complete"),
        user_approved=True,
        event_id=event_id,
    )
    deleted = executor.execute(
        action="delete",
        chat_id=CHAT_A,
        identity=_identity(update_id=8003, action="delete"),
        user_approved=True,
        event_id=event_id,
    )

    assert completed.result == {"event_id": event_id, "completed": True}
    assert deleted.result == {"event_id": event_id, "deleted": True}
    assert writer.current_version(CHAT_A) == 3
    assert _event_count(writer.db_path) == 0


def test_schedule_bot_injected_executor_bypasses_direct_store(monkeypatch):
    identity = _identity()

    class Executor:
        def require_ready(self, *, user_approved):
            assert user_approved is True

        def execute(self, **kwargs):
            assert kwargs["user_approved"] is True
            return ScheduleExecutionResult(
                operation="schedule.add",
                state="applied",
                result={
                    "id": 7,
                    "title": "API 일정",
                    "when_at": "2026-09-01T10:00:00",
                    "notes": None,
                    "completed": False,
                    "rrule_freq": None,
                    "rrule_byday": None,
                    "rrule_until": None,
                    "pre_notify_minutes": 0,
                    "pre_notify_minutes_list": [],
                    "created": True,
                },
                replayed=False,
                request_id=identity.request_id,
                approval_id=identity.approval_id,
            )

    monkeypatch.setattr(
        sb,
        "add_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("direct add")),
    )
    monkeypatch.setattr(
        sb,
        "find_conflicts",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("direct read")),
    )
    answer, _ = sb.run(
        "add",
        title="API 일정",
        when_at="2026-09-01T10:00:00",
        chat_id=CHAT_A,
        write_identity=identity,
        write_executor=Executor(),
        write_user_approved=True,
    )

    assert "일정 등록 #7" in answer


def test_schedule_bot_executor_requires_approval_and_forbids_db_path(
    consumer_stack,
    tmp_path,
):
    executor, _, _, _ = consumer_stack
    identity = _identity()
    with pytest.raises(ScheduleApprovalRequired):
        sb.run(
            "add",
            title="승인 전",
            when_at="2026-09-01T10:00:00",
            chat_id=CHAT_A,
            write_identity=identity,
            write_executor=executor,
        )
    with pytest.raises(ValueError, match="cannot be combined"):
        sb.run(
            "add",
            title="경계 충돌",
            when_at="2026-09-01T10:00:00",
            chat_id=CHAT_A,
            db_path=tmp_path / "assistant.db",
            write_identity=identity,
            write_executor=executor,
            write_user_approved=True,
        )


def test_schedule_bot_executor_error_has_no_direct_fallback(monkeypatch):
    identity = _identity()

    class Executor:
        def require_ready(self, *, user_approved):
            pass

        def execute(self, **kwargs):
            raise PrivateAPIWriteConflict("stale_version")

    monkeypatch.setattr(
        sb,
        "add_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("direct fallback")),
    )
    with pytest.raises(PrivateAPIWriteConflict):
        sb.run(
            "add",
            title="폴백 금지",
            when_at="2026-09-01T10:00:00",
            chat_id=CHAT_A,
            write_identity=identity,
            write_executor=Executor(),
            write_user_approved=True,
        )
