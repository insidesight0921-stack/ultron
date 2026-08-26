from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from private_data_api import create_app
from private_schedule_write_store import ScheduleWriteStore
from private_watchlist_write_store import WatchlistWriteStore


TOKEN = "private-schedule-http-token-32-characters-minimum"
CHAT_A = "111"
CHAT_B = "222"


@pytest.fixture
def schedule_write_api(tmp_path, private_write_permit_factory):
    db = tmp_path / "schedule-http" / "assistant.db"
    writer = ScheduleWriteStore(db, writes_enabled=True)
    permit = private_write_permit_factory(db)
    app = create_app(
        TOKEN,
        schedule_writer=writer,
        enable_schedule_writes=True,
        write_activation_permit=permit,
    )
    return TestClient(app), writer


def _headers(
    *,
    chat_id=CHAT_A,
    key=None,
    request_id=None,
    approval_id=None,
    authorized=True,
):
    headers = {
        "Content-Type": "application/json",
        "X-AI-Agent-Chat-ID": str(chat_id),
        "Idempotency-Key": key or f"schedule-http-{uuid4().hex}",
        "X-Request-ID": request_id or str(uuid4()),
        "X-Approval-ID": approval_id or str(uuid4()),
    }
    if authorized:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


def _body(*, operation="schedule.add", version=0, payload=None):
    if payload is None:
        payload = {"title": "API 일정", "when_at": "2026-09-01T10:00:00"}
    return {"operation": operation, "expected_version": version, "payload": payload}


def _submit(client, *, headers=None, body=None):
    return client.post(
        "/v1/private/schedule/write/intents",
        headers=headers or _headers(),
        json=body or _body(),
    )


def _preflight(client, headers, *, operation="schedule.add", chat_id=None):
    return client.post(
        "/v1/private/schedule/write/preflight",
        headers={
            "Authorization": headers.get("Authorization", ""),
            "X-AI-Agent-Chat-ID": str(chat_id or headers["X-AI-Agent-Chat-ID"]),
            "Idempotency-Key": headers["Idempotency-Key"],
            "X-Request-ID": headers["X-Request-ID"],
            "X-Approval-ID": headers["X-Approval-ID"],
            "X-Write-Operation": operation,
        },
    )


def _transition(client, action, headers, *, chat_id=None, authorized=True):
    request_headers = {
        "X-AI-Agent-Chat-ID": str(chat_id or headers["X-AI-Agent-Chat-ID"]),
        "X-Approval-ID": headers["X-Approval-ID"],
        "X-Request-ID": headers["X-Request-ID"],
    }
    if authorized:
        request_headers["Authorization"] = f"Bearer {TOKEN}"
    return client.post(
        f"/v1/private/schedule/write/approvals/{action}",
        headers=request_headers,
    )


def _event_count(db, chat_id=CHAT_A):
    with sqlite3.connect(db) as con:
        return con.execute(
            "SELECT COUNT(*) FROM events WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]


def test_schedule_writer_is_default_absent_and_requires_writer_and_permit(
    tmp_path, private_write_permit_factory
):
    app = create_app(TOKEN)
    mutation = {
        (route.path, method)
        for route in app.routes
        for method in (route.methods or set())
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert mutation == set()
    with pytest.raises(ValueError, match="schedule writer is required"):
        create_app(TOKEN, enable_schedule_writes=True)

    db = tmp_path / "schedule.db"
    writer = ScheduleWriteStore(db, writes_enabled=True)
    with pytest.raises(ValueError, match="activation permit"):
        create_app(TOKEN, schedule_writer=writer, enable_schedule_writes=True)
    wrong = private_write_permit_factory()
    with pytest.raises(ValueError, match="database scope mismatch"):
        create_app(
            TOKEN,
            schedule_writer=writer,
            enable_schedule_writes=True,
            write_activation_permit=wrong,
        )


def test_schedule_only_app_exposes_exactly_five_schedule_mutations(schedule_write_api):
    client, _ = schedule_write_api
    routes = {
        (route.path, method)
        for route in client.app.routes
        for method in (route.methods or set())
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert len(routes) == 5
    assert all(path.startswith("/v1/private/schedule/write/") for path, _ in routes)
    status = client.get(
        "/v1/private/status", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert status.status_code == 200
    assert status.json()["writes_enabled"] is True
    assert status.json()["capabilities"][-1] == "schedule:write-contract"


def test_combined_injected_app_has_ten_routes_with_one_matching_permit(
    tmp_path, private_write_permit_factory
):
    db = tmp_path / "combined" / "assistant.db"
    permit = private_write_permit_factory(db)
    app = create_app(
        TOKEN,
        watchlist_writer=WatchlistWriteStore(db, writes_enabled=True),
        enable_watchlist_writes=True,
        schedule_writer=ScheduleWriteStore(db, writes_enabled=True),
        enable_schedule_writes=True,
        write_activation_permit=permit,
    )
    routes = {
        (route.path, method)
        for route in app.routes
        for method in (route.methods or set())
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert len(routes) == 10


def test_schedule_http_flow_mutates_only_after_apply_and_replays(schedule_write_api):
    client, writer = schedule_write_api
    headers = _headers()
    body = _body(
        payload={
            "title": "API 주간 일정",
            "when_at": "2026-09-01T10:00:00",
            "rrule_freq": "weekly",
            "rrule_byday": "MO,WE",
            "pre_notify_minutes": [30, 5],
        }
    )
    preflight = _preflight(client, headers)
    assert preflight.status_code == 200
    assert preflight.json()["exists"] is False
    assert preflight.json()["current_version"] == 0

    pending = _submit(
        client,
        headers=headers,
        body=body,
    )
    assert pending.status_code == 202
    assert pending.json()["state"] == "pending"
    assert _event_count(writer.db_path) == 0

    approved = _transition(client, "approve", headers)
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert _event_count(writer.db_path) == 0

    applied = _transition(client, "apply", headers)
    assert applied.status_code == 200
    assert applied.json()["state"] == "applied"
    assert applied.json()["resource_version"] == 1
    assert applied.json()["result"]["pre_notify_minutes_list"] == [30, 5]
    assert _event_count(writer.db_path) == 1

    replay = _submit(client, headers=headers, body=body)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert writer.current_version(CHAT_A) == 1
    assert _event_count(writer.db_path) == 1


def test_schedule_http_requires_auth_and_chat_scope_without_creating_schema(
    schedule_write_api,
):
    client, writer = schedule_write_api
    unauthorized = _submit(client, headers=_headers(authorized=False))
    assert unauthorized.status_code == 401
    missing_scope = _submit(client, headers={**_headers(), "X-AI-Agent-Chat-ID": ""})
    assert missing_scope.status_code == 400
    assert missing_scope.json()["detail"]["code"] == "invalid_schedule_scope"
    with sqlite3.connect(f"file:{writer.db_path}?mode=ro", uri=True) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'private_scoped_%'"
        ).fetchone()[0] == 0


def test_schedule_http_cross_chat_transition_and_preflight_are_blocked(
    schedule_write_api,
):
    client, writer = schedule_write_api
    headers = _headers(chat_id=CHAT_A)
    assert _submit(client, headers=headers).status_code == 202

    preflight = _preflight(client, headers, chat_id=CHAT_B)
    assert preflight.status_code == 409
    assert preflight.json()["detail"]["code"] == "scope_conflict"
    approve = _transition(client, "approve", headers, chat_id=CHAT_B)
    assert approve.status_code == 409
    assert approve.json()["detail"]["code"] == "scope_conflict"
    assert writer.get_record(
        headers["X-Approval-ID"], scope_value=CHAT_A
    ).state == "pending"


@pytest.mark.parametrize(
    "body,status,code",
    [
        (
            _body(payload={"title": "", "when_at": "2026-09-01T10:00:00"}),
            422,
            "invalid_schedule_title",
        ),
        (
            _body(operation="schedule.delete", payload={"event_id": 0}),
            422,
            "invalid_schedule_event",
        ),
        (
            _body(operation="watchlist.add", payload={"ticker": "005930", "name": "삼성전자"}),
            400,
            "operation_not_allowed",
        ),
    ],
)
def test_schedule_http_payload_and_operation_validation(
    schedule_write_api, body, status, code
):
    client, _ = schedule_write_api
    response = _submit(client, body=body)
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


def test_schedule_http_rejects_query_duplicate_json_oversize_and_transition_body(
    schedule_write_api,
):
    client, writer = schedule_write_api
    headers = _headers()
    assert client.post(
        "/v1/private/schedule/write/intents?chat_id=111",
        headers=headers,
        json=_body(),
    ).status_code == 400
    duplicate = client.post(
        "/v1/private/schedule/write/intents",
        headers=headers,
        content=(
            '{"operation":"schedule.add","operation":"schedule.delete",'
            '"expected_version":0,"payload":{"event_id":1}}'
        ),
    )
    assert duplicate.status_code == 400
    assert client.post(
        "/v1/private/schedule/write/intents",
        headers=headers,
        content=b"x" * 40_000,
    ).status_code == 413
    assert client.post(
        "/v1/private/schedule/write/approvals/approve",
        headers=headers,
        json={"approved": True},
    ).status_code == 400
    with sqlite3.connect(f"file:{writer.db_path}?mode=ro", uri=True) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'private_scoped_%'"
        ).fetchone()[0] == 0
