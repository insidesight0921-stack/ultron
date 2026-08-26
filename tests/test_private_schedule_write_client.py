from __future__ import annotations

import json
import sqlite3
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from private_data_api import create_app
from private_data_api_client import (
    PrivateAPIAuthError,
    PrivateAPIError,
    PrivateAPIWriteConflict,
    PrivateAPIWriteDisabled,
    PrivateDataClient,
)
from private_schedule_write_store import ScheduleWriteStore


TOKEN = "private-schedule-client-token-32-characters-minimum"
CHAT_A = "111"
CHAT_B = "222"


class _BridgeResponse:
    def __init__(self, response):
        self._content = response.content
        self.headers = response.headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self._content if limit < 0 else self._content[:limit]


class _TestAppOpener:
    def __init__(self, app_client):
        self.client = app_client
        self.seen = []

    def __call__(self, request, timeout):
        parsed = urlsplit(request.full_url)
        headers = dict(request.header_items())
        self.seen.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "headers": headers,
                "body": request.data,
                "timeout": timeout,
            }
        )
        response = self.client.request(
            request.get_method(),
            parsed.path,
            headers=headers,
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


class _PayloadResponse:
    def __init__(self, payload):
        self.headers = Message()
        self.headers["Cache-Control"] = "no-store"
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self._raw if limit < 0 else self._raw[:limit]


@pytest.fixture
def write_client(tmp_path, private_write_permit_factory):
    db = tmp_path / "schedule-client" / "assistant.db"
    writer = ScheduleWriteStore(db, writes_enabled=True)
    permit = private_write_permit_factory(db)
    app = create_app(
        TOKEN,
        schedule_writer=writer,
        enable_schedule_writes=True,
        write_activation_permit=permit,
    )
    opener = _TestAppOpener(TestClient(app))
    client = PrivateDataClient(
        token=TOKEN,
        opener=opener,
        writes_enabled=True,
        write_activation_permit=permit,
    )
    return client, writer, opener, permit


def _identity(prefix="schedule-client"):
    return {
        "request_id": str(uuid4()),
        "approval_id": str(uuid4()),
        "idempotency_key": f"{prefix}-{uuid4().hex}",
    }


def _submit_add(client, identity, *, chat_id=CHAT_A, version=0):
    return client.submit_schedule_write(
        operation="schedule.add",
        chat_id=chat_id,
        expected_version=version,
        payload={
            "title": "클라이언트 주간 일정",
            "when_at": "2026-09-01T10:00:00",
            "rrule_freq": "weekly",
            "rrule_byday": "MO,WE",
            "pre_notify_minutes": [30, 5],
        },
        **identity,
    )


def _event_count(db, chat_id=CHAT_A):
    with sqlite3.connect(db) as con:
        return con.execute(
            "SELECT COUNT(*) FROM events WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]


def test_schedule_client_is_disabled_before_scope_validation_or_network():
    called = False

    def opener(request, timeout):
        nonlocal called
        called = True
        raise AssertionError("must not open")

    client = PrivateDataClient(token=TOKEN, opener=opener)
    identity = _identity()
    with pytest.raises(PrivateAPIWriteDisabled):
        _submit_add(client, identity, chat_id="not-a-chat")
    with pytest.raises(PrivateAPIWriteDisabled):
        client.preflight_schedule_write(
            operation="schedule.add", chat_id="not-a-chat", **identity
        )
    with pytest.raises(PrivateAPIWriteDisabled):
        client.approve_schedule_write(
            chat_id="not-a-chat",
            request_id=identity["request_id"],
            approval_id=identity["approval_id"],
        )
    assert called is False


def test_schedule_client_runs_full_scoped_approval_flow(write_client):
    client, writer, opener, _ = write_client
    identity = _identity()

    preflight = client.preflight_schedule_write(
        operation="schedule.add", chat_id=CHAT_A, **identity
    )
    pending = _submit_add(client, identity, version=preflight["expected_version"])
    assert pending["state"] == "pending"
    assert _event_count(writer.db_path) == 0

    approved = client.approve_schedule_write(
        chat_id=CHAT_A,
        request_id=identity["request_id"],
        approval_id=identity["approval_id"],
    )
    assert approved["state"] == "approved"
    assert _event_count(writer.db_path) == 0

    applied = client.apply_schedule_write(
        chat_id=CHAT_A,
        request_id=identity["request_id"],
        approval_id=identity["approval_id"],
    )
    assert applied["state"] == "applied"
    assert applied["resource_version"] == 1
    assert applied["result"]["pre_notify_minutes_list"] == [30, 5]
    assert _event_count(writer.db_path) == 1
    assert writer.current_version(CHAT_A) == 1

    assert all("?" not in request["url"] for request in opener.seen)
    assert all(TOKEN not in request["url"] for request in opener.seen)
    assert all(
        request["headers"]["X-ai-agent-chat-id"] == CHAT_A
        for request in opener.seen
    )
    transitions = [request for request in opener.seen if "/approvals/" in request["url"]]
    assert all(request["body"] == b"" for request in transitions)


def test_schedule_client_replay_does_not_duplicate_event(write_client):
    client, writer, _, _ = write_client
    identity = _identity()
    pending = _submit_add(client, identity)
    client.approve_schedule_write(
        chat_id=CHAT_A,
        request_id=identity["request_id"],
        approval_id=identity["approval_id"],
    )
    client.apply_schedule_write(
        chat_id=CHAT_A,
        request_id=identity["request_id"],
        approval_id=identity["approval_id"],
    )
    replay = _submit_add(client, identity, version=pending["expected_version"])

    assert replay["state"] == "applied"
    assert replay["replayed"] is True
    assert _event_count(writer.db_path) == 1
    assert writer.current_version(CHAT_A) == 1


def test_schedule_client_blocks_cross_chat_preflight_and_transition(write_client):
    client, writer, _, _ = write_client
    identity = _identity()
    _submit_add(client, identity)

    with pytest.raises(PrivateAPIWriteConflict) as preflight_error:
        client.preflight_schedule_write(
            operation="schedule.add", chat_id=CHAT_B, **identity
        )
    assert preflight_error.value.code == "scope_conflict"
    with pytest.raises(PrivateAPIWriteConflict) as transition_error:
        client.approve_schedule_write(
            chat_id=CHAT_B,
            request_id=identity["request_id"],
            approval_id=identity["approval_id"],
        )
    assert transition_error.value.code == "scope_conflict"
    assert writer.get_record(identity["approval_id"], scope_value=CHAT_A).state == "pending"


def test_schedule_client_rejects_invalid_scope_and_payload_before_network(write_client):
    client, _, opener, _ = write_client
    identity = _identity()
    before = len(opener.seen)
    with pytest.raises(ValueError):
        _submit_add(client, identity, chat_id="0")
    with pytest.raises(ValueError):
        client.submit_schedule_write(
            operation="schedule.delete",
            chat_id=CHAT_A,
            expected_version=0,
            payload={"event_id": 0},
            **_identity(),
        )
    assert len(opener.seen) == before


def test_schedule_client_reject_flow_never_mutates(write_client):
    client, writer, _, _ = write_client
    identity = _identity()
    _submit_add(client, identity)
    rejected = client.reject_schedule_write(
        chat_id=CHAT_A,
        request_id=identity["request_id"],
        approval_id=identity["approval_id"],
    )
    assert rejected["state"] == "rejected"
    assert _event_count(writer.db_path) == 0
    assert writer.current_version(CHAT_A) == 0


def test_schedule_client_validates_complete_and_delete_results(write_client):
    client, writer, _, _ = write_client
    add_identity = _identity()
    _submit_add(client, add_identity)
    client.approve_schedule_write(
        chat_id=CHAT_A,
        request_id=add_identity["request_id"],
        approval_id=add_identity["approval_id"],
    )
    added = client.apply_schedule_write(
        chat_id=CHAT_A,
        request_id=add_identity["request_id"],
        approval_id=add_identity["approval_id"],
    )
    event_id = added["result"]["id"]

    complete_identity = _identity()
    client.submit_schedule_write(
        operation="schedule.complete",
        chat_id=CHAT_A,
        expected_version=1,
        payload={"event_id": event_id},
        **complete_identity,
    )
    client.approve_schedule_write(
        chat_id=CHAT_A,
        request_id=complete_identity["request_id"],
        approval_id=complete_identity["approval_id"],
    )
    completed = client.apply_schedule_write(
        chat_id=CHAT_A,
        request_id=complete_identity["request_id"],
        approval_id=complete_identity["approval_id"],
    )
    assert completed["result"] == {"event_id": event_id, "completed": True}

    delete_identity = _identity()
    client.submit_schedule_write(
        operation="schedule.delete",
        chat_id=CHAT_A,
        expected_version=2,
        payload={"event_id": event_id},
        **delete_identity,
    )
    client.approve_schedule_write(
        chat_id=CHAT_A,
        request_id=delete_identity["request_id"],
        approval_id=delete_identity["approval_id"],
    )
    deleted = client.apply_schedule_write(
        chat_id=CHAT_A,
        request_id=delete_identity["request_id"],
        approval_id=delete_identity["approval_id"],
    )
    assert deleted["result"] == {"event_id": event_id, "deleted": True}
    assert _event_count(writer.db_path) == 0
    assert writer.current_version(CHAT_A) == 3


def test_schedule_client_maps_auth_failure(write_client):
    _, _, opener, permit = write_client
    client = PrivateDataClient(
        token="wrong-schedule-client-token-32-characters-minimum",
        opener=opener,
        writes_enabled=True,
        write_activation_permit=permit,
    )
    with pytest.raises(PrivateAPIAuthError):
        _submit_add(client, _identity())


@pytest.mark.parametrize("mutation", ["extra_field", "bad_result"])
def test_schedule_client_rejects_non_allowlisted_responses(
    mutation, private_write_permit_factory
):
    identity = _identity()
    response = {
        "request_id": identity["request_id"],
        "approval_id": identity["approval_id"],
        "operation": "schedule.add",
        "state": "applied",
        "expected_version": 0,
        "resource_version": 1,
        "result": {
            "id": 1,
            "title": "응답 일정",
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
        "replayed": False,
    }
    if mutation == "extra_field":
        response["private_payload"] = "must be rejected"
    else:
        response["result"]["chat_id"] = CHAT_A

    def opener(request, timeout):
        return _PayloadResponse(response)

    client = PrivateDataClient(
        token=TOKEN,
        opener=opener,
        writes_enabled=True,
        write_activation_permit=private_write_permit_factory(),
    )
    with pytest.raises(PrivateAPIError):
        _submit_add(client, identity)
