from __future__ import annotations

import json
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
    PrivateAPIWriteRejected,
    PrivateDataClient,
)
from private_watchlist_write_store import WatchlistWriteStore
import watchlist_store


TOKEN = "private-write-client-token-32-characters-minimum"


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
    db = tmp_path / "client-isolated" / "assistant.db"
    writer = WatchlistWriteStore(db, writes_enabled=True)
    permit = private_write_permit_factory(db)

    def read_items():
        return [
            {"ticker": item.ticker, "name": item.name, "created_at": item.created_at}
            for item in watchlist_store.list_items(db_path=db)
        ]

    app = create_app(
        TOKEN,
        watchlist_reader=read_items,
        watchlist_writer=writer,
        enable_watchlist_writes=True,
        write_activation_permit=permit,
    )
    opener = _TestAppOpener(TestClient(app))
    client = PrivateDataClient(
        token=TOKEN,
        opener=opener,
        writes_enabled=True,
        write_activation_permit=permit,
    )
    return client, writer, opener


def _identity():
    return {
        "request_id": str(uuid4()),
        "approval_id": str(uuid4()),
        "idempotency_key": f"watchlist-client-{uuid4().hex}",
    }


def _submit_add(client, identity, *, version=0, ticker="005930", name="삼성전자"):
    return client.submit_watchlist_write(
        operation="watchlist.add",
        expected_version=version,
        payload={"ticker": ticker, "name": name},
        **identity,
    )


def test_write_client_is_disabled_by_default_without_opening_network(
    private_write_permit_factory,
):
    called = False

    def opener(request, timeout):
        nonlocal called
        called = True
        raise AssertionError("must not open")

    client = PrivateDataClient(token=TOKEN, opener=opener)
    with pytest.raises(PrivateAPIWriteDisabled):
        _submit_add(client, _identity())
    identity = _identity()
    with pytest.raises(PrivateAPIWriteDisabled):
        client.preflight_watchlist_write(operation="watchlist.add", **identity)
    assert called is False

    with pytest.raises(ValueError, match="activation permit"):
        PrivateDataClient(token=TOKEN, opener=opener, writes_enabled=True)
    with pytest.raises(ValueError, match="requires writes_enabled"):
        PrivateDataClient(
            token=TOKEN,
            opener=opener,
            write_activation_permit=private_write_permit_factory(),
        )


def test_client_runs_full_temporary_http_approval_flow(write_client):
    client, writer, opener = write_client
    identity = _identity()

    preflight = client.preflight_watchlist_write(
        operation="watchlist.add",
        **identity,
    )
    pending = _submit_add(client, identity, version=preflight["expected_version"])
    approved = client.approve_watchlist_write(
        request_id=identity["request_id"],
        approval_id=identity["approval_id"],
    )
    applied = client.apply_watchlist_write(
        request_id=identity["request_id"],
        approval_id=identity["approval_id"],
    )

    assert preflight["exists"] is False
    assert preflight["expected_version"] == 0
    assert pending["state"] == "pending"
    assert approved["state"] == "approved"
    assert applied["state"] == "applied"
    assert applied["result"]["ticker"] == "005930"
    assert writer.current_version() == 1
    assert client.list_watchlist()[0]["name"] == "삼성전자"
    assert all("?" not in request["url"] for request in opener.seen)
    assert all(TOKEN not in request["url"] for request in opener.seen)
    transitions = [request for request in opener.seen if "/approvals/" in request["url"]]
    assert all(request["body"] == b"" for request in transitions)


def test_client_preflight_restores_original_version_for_partial_retry(write_client):
    client, writer, _ = write_client
    pending_identity = _identity()
    first = client.preflight_watchlist_write(
        operation="watchlist.add",
        **pending_identity,
    )
    _submit_add(client, pending_identity, version=first["expected_version"], ticker="000660", name="SK하이닉스")

    other_identity = _identity()
    other = client.preflight_watchlist_write(operation="watchlist.add", **other_identity)
    _submit_add(client, other_identity, version=other["expected_version"])
    client.approve_watchlist_write(
        request_id=other_identity["request_id"],
        approval_id=other_identity["approval_id"],
    )
    client.apply_watchlist_write(
        request_id=other_identity["request_id"],
        approval_id=other_identity["approval_id"],
    )

    retry = client.preflight_watchlist_write(
        operation="watchlist.add",
        **pending_identity,
    )
    assert retry["exists"] is True
    assert retry["state"] == "pending"
    assert retry["expected_version"] == 0
    assert retry["current_version"] == 1
    assert writer.current_version() == 1


def test_client_preflight_returns_saved_applied_result(write_client):
    client, _, _ = write_client
    identity = _identity()
    initial = client.preflight_watchlist_write(operation="watchlist.add", **identity)
    _submit_add(client, identity, version=initial["expected_version"])
    client.approve_watchlist_write(
        request_id=identity["request_id"], approval_id=identity["approval_id"]
    )
    applied = client.apply_watchlist_write(
        request_id=identity["request_id"], approval_id=identity["approval_id"]
    )

    retry = client.preflight_watchlist_write(operation="watchlist.add", **identity)

    assert retry["exists"] is True
    assert retry["state"] == "applied"
    assert retry["result"] == applied["result"]
    assert retry["expected_version"] == 0
    assert retry["current_version"] == 1


def test_client_preflight_maps_identity_conflict(write_client):
    client, _, _ = write_client
    identity = _identity()
    _submit_add(client, identity)
    changed = {**identity, "request_id": str(uuid4())}

    with pytest.raises(PrivateAPIWriteConflict) as exc:
        client.preflight_watchlist_write(operation="watchlist.add", **changed)

    assert exc.value.code == "identifier_conflict"


def test_client_replays_same_intent_and_maps_payload_conflict(write_client):
    client, writer, _ = write_client
    identity = _identity()
    _submit_add(client, identity)
    client.approve_watchlist_write(
        request_id=identity["request_id"], approval_id=identity["approval_id"]
    )
    client.apply_watchlist_write(
        request_id=identity["request_id"], approval_id=identity["approval_id"]
    )

    replay = _submit_add(client, identity)
    assert replay["replayed"] is True
    assert replay["state"] == "applied"

    with pytest.raises(PrivateAPIWriteConflict) as exc:
        _submit_add(client, identity, ticker="000660", name="SK하이닉스")
    assert exc.value.code == "idempotency_conflict"
    assert writer.current_version() == 1


def test_client_maps_version_conflict_without_second_mutation(write_client):
    client, writer, _ = write_client
    first = _identity()
    stale = _identity()
    _submit_add(client, first, version=0)
    _submit_add(client, stale, version=0, ticker="000660", name="SK하이닉스")
    for identity in (first, stale):
        client.approve_watchlist_write(
            request_id=identity["request_id"], approval_id=identity["approval_id"]
        )
    client.apply_watchlist_write(
        request_id=first["request_id"], approval_id=first["approval_id"]
    )

    with pytest.raises(PrivateAPIWriteConflict) as exc:
        client.apply_watchlist_write(
            request_id=stale["request_id"], approval_id=stale["approval_id"]
        )
    assert exc.value.code == "version_conflict"
    assert writer.current_version() == 1
    assert [item.ticker for item in watchlist_store.list_items(db_path=writer.db_path)] == ["005930"]


def test_client_reject_flow_and_identity_binding(write_client):
    client, writer, _ = write_client
    identity = _identity()
    _submit_add(client, identity)

    with pytest.raises(PrivateAPIWriteConflict) as mismatch:
        client.approve_watchlist_write(
            request_id=str(uuid4()),
            approval_id=identity["approval_id"],
        )
    assert mismatch.value.code == "identifier_conflict"
    assert writer.get_record(identity["approval_id"]).state == "pending"

    rejected = client.reject_watchlist_write(
        request_id=identity["request_id"], approval_id=identity["approval_id"]
    )
    assert rejected["state"] == "rejected"
    with pytest.raises(PrivateAPIWriteConflict) as apply_rejected:
        client.apply_watchlist_write(
            request_id=identity["request_id"], approval_id=identity["approval_id"]
        )
    assert apply_rejected.value.code == "approval_required"


def test_client_maps_auth_and_domain_validation_without_leaking_token(write_client):
    _, writer, opener = write_client
    permit = write_client[0].write_activation_permit
    wrong = PrivateDataClient(
        token="wrong-private-client-token-32-characters",
        opener=opener,
        writes_enabled=True,
        write_activation_permit=permit,
    )
    with pytest.raises(PrivateAPIAuthError) as auth:
        _submit_add(wrong, _identity())
    assert wrong.token not in str(auth.value)

    client = PrivateDataClient(
        token=TOKEN,
        opener=opener,
        writes_enabled=True,
        write_activation_permit=permit,
    )
    identity = _identity()
    _submit_add(client, identity, ticker="BAD")
    client.approve_watchlist_write(
        request_id=identity["request_id"], approval_id=identity["approval_id"]
    )
    with pytest.raises(PrivateAPIWriteRejected) as invalid:
        client.apply_watchlist_write(
            request_id=identity["request_id"], approval_id=identity["approval_id"]
        )
    assert invalid.value.code == "invalid_ticker"
    assert invalid.value.status_code == 422
    assert writer.get_record(identity["approval_id"]).state == "expired"
    assert writer.list_audit_events()[-1]["result"] == "failed"
    assert watchlist_store.list_items(db_path=writer.db_path) == []


def test_client_rejects_unexpected_write_response_fields(private_write_permit_factory):
    identity = _identity()
    payload = {
        "request_id": identity["request_id"],
        "approval_id": identity["approval_id"],
        "operation": "watchlist.add",
        "state": "pending",
        "expected_version": 0,
        "resource_version": 0,
        "result": None,
        "replayed": False,
        "private_payload": {"ticker": "005930"},
    }
    client = PrivateDataClient(
        token=TOKEN,
        opener=lambda request, timeout: _PayloadResponse(payload),
        writes_enabled=True,
        write_activation_permit=private_write_permit_factory(),
    )
    with pytest.raises(PrivateAPIError):
        _submit_add(client, identity)


def test_client_rejects_inconsistent_preflight_response(private_write_permit_factory):
    identity = _identity()
    payload = {
        "exists": False,
        "request_id": identity["request_id"],
        "approval_id": identity["approval_id"],
        "operation": "watchlist.add",
        "state": "pending",
        "expected_version": 0,
        "current_version": 0,
        "result": None,
    }
    client = PrivateDataClient(
        token=TOKEN,
        opener=lambda request, timeout: _PayloadResponse(payload),
        writes_enabled=True,
        write_activation_permit=private_write_permit_factory(),
    )
    with pytest.raises(PrivateAPIError):
        client.preflight_watchlist_write(operation="watchlist.add", **identity)


def test_client_maps_413_error_code_without_echoing_body(private_write_permit_factory):
    def too_large(request, timeout):
        headers = Message()
        headers["Cache-Control"] = "no-store"
        body = BytesIO(b'{"detail":{"code":"payload_too_large"}}')
        raise HTTPError(request.full_url, 413, "too large", headers, body)

    client = PrivateDataClient(
        token=TOKEN,
        opener=too_large,
        writes_enabled=True,
        write_activation_permit=private_write_permit_factory(),
    )
    with pytest.raises(PrivateAPIWriteRejected) as exc:
        _submit_add(client, _identity())
    assert exc.value.code == "payload_too_large"
    assert exc.value.status_code == 413
