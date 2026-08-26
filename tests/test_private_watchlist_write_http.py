from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from private_data_api import create_app
from private_watchlist_write_store import WatchlistWriteStore
import watchlist_store


TOKEN = "private-write-http-token-32-characters-minimum"


@pytest.fixture
def write_api(tmp_path, private_write_permit_factory):
    db = tmp_path / "http-isolated" / "assistant.db"
    writer = WatchlistWriteStore(db, writes_enabled=True)
    permit = private_write_permit_factory(db)

    def read_items():
        return [
            {
                "ticker": item.ticker,
                "name": item.name,
                "created_at": item.created_at,
            }
            for item in watchlist_store.list_items(db_path=db)
        ]

    app = create_app(
        TOKEN,
        watchlist_reader=read_items,
        watchlist_writer=writer,
        enable_watchlist_writes=True,
        write_activation_permit=permit,
    )
    return TestClient(app), writer


def _headers(*, key=None, request_id=None, approval_id=None, authorized=True):
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": key or f"watchlist-http-{uuid4().hex}",
        "X-Request-ID": request_id or str(uuid4()),
        "X-Approval-ID": approval_id or str(uuid4()),
    }
    if authorized:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


def _body(*, operation="watchlist.add", version=0, ticker="005930", name="삼성전자"):
    payload = {"ticker": ticker}
    if operation == "watchlist.add":
        payload["name"] = name
    return {"operation": operation, "expected_version": version, "payload": payload}


def _submit(client, *, headers=None, body=None):
    return client.post(
        "/v1/private/watchlist/write/intents",
        headers=headers or _headers(),
        json=body or _body(),
    )


def _transition(client, action, approval_id, *, request_id=None, authorized=True):
    headers = {
        "X-Approval-ID": approval_id,
        "X-Request-ID": request_id or "00000000-0000-0000-0000-000000000000",
    }
    if authorized:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return client.post(
        f"/v1/private/watchlist/write/approvals/{action}",
        headers=headers,
    )


def _preflight(client, headers, *, operation="watchlist.add"):
    return client.post(
        "/v1/private/watchlist/write/preflight",
        headers={
            "Authorization": headers.get("Authorization", ""),
            "Idempotency-Key": headers["Idempotency-Key"],
            "X-Request-ID": headers["X-Request-ID"],
            "X-Approval-ID": headers["X-Approval-ID"],
            "X-Write-Operation": operation,
        },
    )


def _assert_write_schema_absent(writer):
    with sqlite3.connect(f"file:{writer.db_path}?mode=ro", uri=True) as con:
        assert con.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'private_write_%'"
        ).fetchone()[0] == 0


def test_default_app_has_no_mutation_routes_and_cannot_be_enabled_without_writer():
    app = create_app(TOKEN)
    mutation_methods = {"POST", "PUT", "PATCH", "DELETE"}
    assert {
        (route.path, method)
        for route in app.routes
        for method in (route.methods or set())
        if method in mutation_methods
    } == set()
    with pytest.raises(ValueError, match="writer is required"):
        create_app(TOKEN, enable_watchlist_writes=True)


def test_injected_writer_requires_matching_activation_permit(
    tmp_path, private_write_permit_factory
):
    db = tmp_path / "permit-boundary" / "assistant.db"
    writer = WatchlistWriteStore(db, writes_enabled=True)
    with pytest.raises(ValueError, match="activation permit"):
        create_app(
            TOKEN,
            watchlist_writer=writer,
            enable_watchlist_writes=True,
        )

    other_permit = private_write_permit_factory()
    with pytest.raises(ValueError, match="database scope mismatch"):
        create_app(
            TOKEN,
            watchlist_writer=writer,
            enable_watchlist_writes=True,
            write_activation_permit=other_permit,
        )


def test_operational_cli_has_no_write_activation_option():
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "private_data_api.py"
    ).read_text(encoding="utf-8")
    assert 'parser.add_argument("--enable' not in source
    assert "load_private_write_runtime_bundle()" in source
    assert "uvicorn.run(app," in source


def test_injected_http_contract_is_authenticated_and_reports_test_capability(write_api):
    client, writer = write_api
    response = _submit(client, headers=_headers(authorized=False))
    assert response.status_code == 401
    _assert_write_schema_absent(writer)

    status = client.get(
        "/v1/private/status",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status.status_code == 200
    assert status.json()["writes_enabled"] is True
    assert status.json()["capabilities"][-1] == "watchlist:write-contract"


def test_submit_approve_apply_http_flow_mutates_only_after_apply(write_api):
    client, writer = write_api
    headers = _headers()
    pending = _submit(client, headers=headers)

    assert pending.status_code == 202
    assert pending.json()["state"] == "pending"
    assert watchlist_store.list_items(db_path=writer.db_path) == []

    approved = _transition(
        client, "approve", headers["X-Approval-ID"], request_id=headers["X-Request-ID"]
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert watchlist_store.list_items(db_path=writer.db_path) == []

    applied = _transition(
        client, "apply", headers["X-Approval-ID"], request_id=headers["X-Request-ID"]
    )
    assert applied.status_code == 200
    assert applied.json()["state"] == "applied"
    assert applied.json()["resource_version"] == 1
    assert [(item.ticker, item.name) for item in watchlist_store.list_items(db_path=writer.db_path)] == [
        ("005930", "삼성전자")
    ]


def test_http_preflight_returns_new_version_then_existing_applied_state(write_api):
    client, writer = write_api
    headers = _headers()

    new = _preflight(client, headers)
    assert new.status_code == 200
    assert new.json() == {
        "exists": False,
        "request_id": headers["X-Request-ID"],
        "approval_id": headers["X-Approval-ID"],
        "operation": "watchlist.add",
        "state": None,
        "expected_version": 0,
        "current_version": 0,
        "result": None,
    }
    assert writer.list_audit_events() == []

    assert _submit(client, headers=headers).status_code == 202
    assert _transition(client, "approve", headers["X-Approval-ID"], request_id=headers["X-Request-ID"]).status_code == 200
    assert _transition(client, "apply", headers["X-Approval-ID"], request_id=headers["X-Request-ID"]).status_code == 200

    existing = _preflight(client, headers)
    assert existing.status_code == 200
    assert existing.json()["exists"] is True
    assert existing.json()["state"] == "applied"
    assert existing.json()["expected_version"] == 0
    assert existing.json()["current_version"] == 1
    assert existing.json()["result"]["ticker"] == "005930"


def test_http_preflight_rejects_identity_and_operation_mismatch(write_api):
    client, _ = write_api
    headers = _headers()
    assert _submit(client, headers=headers).status_code == 202

    wrong_identity = dict(headers)
    wrong_identity["X-Request-ID"] = str(uuid4())
    identity_conflict = _preflight(client, wrong_identity)
    assert identity_conflict.status_code == 409
    assert identity_conflict.json()["detail"]["code"] == "identifier_conflict"

    operation_conflict = _preflight(client, headers, operation="watchlist.remove")
    assert operation_conflict.status_code == 409
    assert operation_conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_http_preflight_requires_auth_headers_and_empty_body(write_api):
    client, writer = write_api
    headers = _headers(authorized=False)
    assert _preflight(client, headers).status_code == 401
    _assert_write_schema_absent(writer)

    authorized = _headers()
    response = client.post(
        "/v1/private/watchlist/write/preflight",
        headers={**authorized, "X-Write-Operation": "watchlist.add"},
        json={"payload": "not-allowed"},
    )
    assert response.status_code == 400


def test_http_idempotency_replay_and_payload_conflict(write_api):
    client, writer = write_api
    headers = _headers(key="watchlist-http-same-0001")
    assert _submit(client, headers=headers).status_code == 202
    assert _transition(client, "approve", headers["X-Approval-ID"], request_id=headers["X-Request-ID"]).status_code == 200
    assert _transition(client, "apply", headers["X-Approval-ID"], request_id=headers["X-Request-ID"]).status_code == 200

    replay = _submit(client, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert writer.current_version() == 1

    conflict = _submit(
        client,
        headers=headers,
        body=_body(ticker="000660", name="SK하이닉스"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    assert writer.current_version() == 1


def test_http_version_conflict_does_not_apply_stale_intent(write_api):
    client, writer = write_api
    first_headers = _headers()
    stale_headers = _headers()
    assert _submit(client, headers=first_headers).status_code == 202
    assert _submit(
        client,
        headers=stale_headers,
        body=_body(ticker="000660", name="SK하이닉스"),
    ).status_code == 202
    for headers in (first_headers, stale_headers):
        assert _transition(client, "approve", headers["X-Approval-ID"], request_id=headers["X-Request-ID"]).status_code == 200
    assert _transition(client, "apply", first_headers["X-Approval-ID"], request_id=first_headers["X-Request-ID"]).status_code == 200

    conflict = _transition(client, "apply", stale_headers["X-Approval-ID"], request_id=stale_headers["X-Request-ID"])
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "version_conflict"
    assert writer.current_version() == 1
    assert [item.ticker for item in watchlist_store.list_items(db_path=writer.db_path)] == ["005930"]


def test_http_reject_blocks_apply(write_api):
    client, writer = write_api
    headers = _headers()
    assert _submit(client, headers=headers).status_code == 202
    rejected = _transition(client, "reject", headers["X-Approval-ID"], request_id=headers["X-Request-ID"])
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"

    apply_response = _transition(client, "apply", headers["X-Approval-ID"], request_id=headers["X-Request-ID"])
    assert apply_response.status_code == 409
    assert apply_response.json()["detail"]["code"] == "approval_required"
    assert watchlist_store.list_items(db_path=writer.db_path) == []


@pytest.mark.parametrize(
    "headers,body,status",
    [
        ({"Authorization": f"Bearer {TOKEN}"}, _body(), 400),
        (_headers(), {"operation": "schedule.delete", "expected_version": 0, "payload": {"event_id": 1}}, 400),
        (_headers(), {**_body(), "db_path": "/tmp/private.db"}, 400),
    ],
)
def test_http_envelope_and_operation_allowlist(headers, body, status, write_api):
    client, _ = write_api
    assert _submit(client, headers=headers, body=body).status_code == status


def test_http_rejects_query_duplicate_json_and_oversized_body(write_api):
    client, writer = write_api
    headers = _headers()
    query = client.post(
        "/v1/private/watchlist/write/intents?db_path=/tmp/x",
        headers=headers,
        json=_body(),
    )
    assert query.status_code == 400

    duplicate = client.post(
        "/v1/private/watchlist/write/intents",
        headers=headers,
        content=(
            '{"operation":"watchlist.add","operation":"watchlist.remove",'
            '"expected_version":0,"payload":{"ticker":"005930"}}'
        ),
    )
    assert duplicate.status_code == 400

    oversized = client.post(
        "/v1/private/watchlist/write/intents",
        headers=headers,
        content=b"x" * 40_000,
    )
    assert oversized.status_code == 413
    _assert_write_schema_absent(writer)


def test_http_transition_rejects_invalid_approval_id_and_body(write_api):
    client, _ = write_api
    invalid_id = client.post(
        "/v1/private/watchlist/write/approvals/approve",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Approval-ID": "not-a-uuid",
            "X-Request-ID": str(uuid4()),
        },
    )
    assert invalid_id.status_code == 400

    unexpected_body = client.post(
        "/v1/private/watchlist/write/approvals/approve",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Approval-ID": str(uuid4()),
            "X-Request-ID": str(uuid4()),
            "Content-Type": "application/json",
        },
        json={"approved": True},
    )
    assert unexpected_body.status_code == 400


def test_http_transition_rejects_mismatched_request_id_before_state_change(write_api):
    client, writer = write_api
    headers = _headers()
    assert _submit(client, headers=headers).status_code == 202

    conflict = _transition(
        client,
        "approve",
        headers["X-Approval-ID"],
        request_id=str(uuid4()),
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "identifier_conflict"
    assert writer.get_record(headers["X-Approval-ID"]).state == "pending"
