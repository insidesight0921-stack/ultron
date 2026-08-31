from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import paper_db
from private_data_api import create_app
from private_paper_write_store import PaperTradeWriteStore
from private_schedule_write_store import ScheduleWriteStore
from private_watchlist_write_store import WatchlistWriteStore


TOKEN = "private-paper-http-token-32-characters-minimum"


@pytest.fixture
def paper_write_api(tmp_path, private_write_permit_factory):
    db = tmp_path / "paper-http" / "paper.db"
    seeded = paper_db.ensure_seed(db_path=db, seed_capital=100_000_000)
    slots = {item["name"]: int(item["id"]) for item in seeded["slots"]}
    writer = PaperTradeWriteStore(db, writes_enabled=True)
    permit = private_write_permit_factory(db)
    app = create_app(
        TOKEN,
        paper_trade_writer=writer,
        enable_paper_trade_writes=True,
        paper_write_activation_permit=permit,
    )
    return TestClient(app), writer, slots


def _headers(
    slot_id,
    *,
    key=None,
    request_id=None,
    approval_id=None,
    authorized=True,
):
    headers = {
        "Content-Type": "application/json",
        "X-AI-Agent-Paper-Slot-ID": str(slot_id),
        "Idempotency-Key": key or f"paper-http-{uuid4().hex}",
        "X-Request-ID": request_id or str(uuid4()),
        "X-Approval-ID": approval_id or str(uuid4()),
    }
    if authorized:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


def _body(slot_id, *, operation="paper.buy", version=0, payload=None):
    if payload is None:
        payload = {
            "slot": slot_id,
            "ticker": "005930",
            "name": "삼성전자",
            "quantity": 10,
            "price": 80_000,
            "fees": 0,
        }
    return {"operation": operation, "expected_version": version, "payload": payload}


def _submit(client, slot_id, *, headers=None, body=None):
    return client.post(
        "/v1/private/paper/write/intents",
        headers=headers or _headers(slot_id),
        json=body or _body(slot_id),
    )


def _preflight(client, headers, *, operation="paper.buy", slot_id=None):
    return client.post(
        "/v1/private/paper/write/preflight",
        headers={
            "Authorization": headers.get("Authorization", ""),
            "X-AI-Agent-Paper-Slot-ID": str(
                slot_id if slot_id is not None else headers["X-AI-Agent-Paper-Slot-ID"]
            ),
            "Idempotency-Key": headers["Idempotency-Key"],
            "X-Request-ID": headers["X-Request-ID"],
            "X-Approval-ID": headers["X-Approval-ID"],
            "X-Write-Operation": operation,
        },
    )


def _transition(client, action, headers, *, slot_id=None, authorized=True):
    request_headers = {
        "X-AI-Agent-Paper-Slot-ID": str(
            slot_id if slot_id is not None else headers["X-AI-Agent-Paper-Slot-ID"]
        ),
        "X-Approval-ID": headers["X-Approval-ID"],
        "X-Request-ID": headers["X-Request-ID"],
    }
    if authorized:
        request_headers["Authorization"] = f"Bearer {TOKEN}"
    return client.post(
        f"/v1/private/paper/write/approvals/{action}",
        headers=request_headers,
    )


def _paper_state(db, slot_id):
    with sqlite3.connect(db) as con:
        capital = con.execute(
            "SELECT current_capital FROM slots WHERE id=?", (slot_id,)
        ).fetchone()[0]
        positions = con.execute(
            "SELECT ticker,quantity,avg_price FROM positions WHERE slot_id=? ORDER BY ticker",
            (slot_id,),
        ).fetchall()
        trades = con.execute(
            "SELECT side,ticker,quantity,price,fees FROM trades WHERE slot_id=? ORDER BY id",
            (slot_id,),
        ).fetchall()
    return float(capital), [tuple(row) for row in positions], [tuple(row) for row in trades]


def _scoped_table_count(db):
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        return con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'private_scoped_%'"
        ).fetchone()[0]


def test_paper_writer_is_default_absent_and_requires_dedicated_matching_permit(
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
    with pytest.raises(ValueError, match="Paper trade writer is required"):
        create_app(TOKEN, enable_paper_trade_writes=True)

    db = tmp_path / "paper.db"
    with pytest.raises(ValueError, match="explicitly enable mutations"):
        create_app(
            TOKEN,
            paper_trade_writer=PaperTradeWriteStore(db),
            enable_paper_trade_writes=True,
        )
    writer = PaperTradeWriteStore(db, writes_enabled=True)
    with pytest.raises(ValueError, match="activation permit"):
        create_app(
            TOKEN,
            paper_trade_writer=writer,
            enable_paper_trade_writes=True,
        )
    wrong = private_write_permit_factory()
    with pytest.raises(ValueError, match="requires enabled Paper writes"):
        create_app(TOKEN, paper_write_activation_permit=wrong)
    with pytest.raises(ValueError, match="database scope mismatch"):
        create_app(
            TOKEN,
            paper_trade_writer=writer,
            enable_paper_trade_writes=True,
            paper_write_activation_permit=wrong,
        )


def test_paper_only_app_exposes_exactly_five_paper_mutations(paper_write_api):
    client, _, _ = paper_write_api
    routes = {
        (route.path, method)
        for route in client.app.routes
        for method in (route.methods or set())
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert len(routes) == 5
    assert all(path.startswith("/v1/private/paper/write/") for path, _ in routes)
    status = client.get(
        "/v1/private/status", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert status.status_code == 200
    assert status.json()["writes_enabled"] is True
    assert status.json()["capabilities"][-1] == "paper:trades:write-contract"


def test_combined_app_uses_separate_assistant_and_paper_permits(
    tmp_path, private_write_permit_factory
):
    assistant_db = tmp_path / "combined" / "assistant.db"
    paper_db_path = tmp_path / "combined" / "paper.db"
    assistant_permit = private_write_permit_factory(assistant_db)
    paper_permit = private_write_permit_factory(paper_db_path)
    app = create_app(
        TOKEN,
        watchlist_writer=WatchlistWriteStore(assistant_db, writes_enabled=True),
        enable_watchlist_writes=True,
        schedule_writer=ScheduleWriteStore(assistant_db, writes_enabled=True),
        enable_schedule_writes=True,
        write_activation_permit=assistant_permit,
        paper_trade_writer=PaperTradeWriteStore(paper_db_path, writes_enabled=True),
        enable_paper_trade_writes=True,
        paper_write_activation_permit=paper_permit,
    )
    routes = {
        (route.path, method)
        for route in app.routes
        for method in (route.methods or set())
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert len(routes) == 15


def test_http_buy_then_sell_mutates_only_after_apply_and_replays(paper_write_api):
    client, writer, slots = paper_write_api
    slot_id = slots["콴텍"]
    buy_headers = _headers(slot_id)
    before = _paper_state(writer.db_path, slot_id)

    preflight = _preflight(client, buy_headers)
    assert preflight.status_code == 200
    assert preflight.json()["exists"] is False
    assert preflight.json()["current_version"] == 0
    pending = _submit(client, slot_id, headers=buy_headers)
    assert pending.status_code == 202
    assert pending.json()["state"] == "pending"
    assert _paper_state(writer.db_path, slot_id) == before
    approved = _transition(client, "approve", buy_headers)
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert _paper_state(writer.db_path, slot_id) == before
    applied = _transition(client, "apply", buy_headers)
    assert applied.status_code == 200
    assert applied.json()["state"] == "applied"
    assert applied.json()["resource_version"] == 1
    assert applied.json()["result"]["total_cost"] == 800_000.0

    replay = _submit(client, slot_id, headers=buy_headers)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert len(_paper_state(writer.db_path, slot_id)[2]) == 1

    sell_headers = _headers(slot_id)
    sell_body = _body(
        slot_id,
        operation="paper.sell",
        version=1,
        payload={
            "slot": slot_id,
            "ticker": "005930",
            "quantity": 10,
            "price": 90_000,
            "fees": 0,
        },
    )
    assert _submit(client, slot_id, headers=sell_headers, body=sell_body).status_code == 202
    assert _transition(client, "approve", sell_headers).status_code == 200
    sold = _transition(client, "apply", sell_headers)
    assert sold.status_code == 200
    assert sold.json()["resource_version"] == 2
    assert sold.json()["result"]["proceeds"] == 900_000.0
    capital, positions, trades = _paper_state(writer.db_path, slot_id)
    assert capital == 35_100_000     # 콴텍 시드 4,000만 → 3,500만(2026-08-29)
    assert positions == []
    assert [item[0] for item in trades] == ["buy", "sell"]


def test_preflight_recovers_existing_applied_state(paper_write_api):
    client, writer, slots = paper_write_api
    slot_id = slots["콴텍"]
    headers = _headers(slot_id)
    assert _submit(client, slot_id, headers=headers).status_code == 202
    assert _transition(client, "approve", headers).status_code == 200
    assert _transition(client, "apply", headers).status_code == 200

    existing = _preflight(client, headers)
    assert existing.status_code == 200
    assert existing.json()["exists"] is True
    assert existing.json()["state"] == "applied"
    assert existing.json()["expected_version"] == 0
    assert existing.json()["current_version"] == 1
    assert existing.json()["result"]["ticker"] == "005930"
    assert writer.current_version(slot_id) == 1


def test_auth_and_scope_are_required_before_schema_creation(paper_write_api):
    client, writer, slots = paper_write_api
    slot_id = slots["콴텍"]
    unauthorized = _submit(client, slot_id, headers=_headers(slot_id, authorized=False))
    assert unauthorized.status_code == 401
    assert _scoped_table_count(writer.db_path) == 0

    missing = _headers(slot_id)
    missing["X-AI-Agent-Paper-Slot-ID"] = ""
    response = _submit(client, slot_id, headers=missing)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_paper_slot"
    assert _scoped_table_count(writer.db_path) == 0

    noncanonical = _headers(slot_id)
    noncanonical["X-AI-Agent-Paper-Slot-ID"] = f"0{slot_id}"
    assert _submit(client, slot_id, headers=noncanonical).status_code == 400
    assert _scoped_table_count(writer.db_path) == 0


def test_cross_slot_scope_and_payload_scope_mismatch_are_blocked(paper_write_api):
    client, writer, slots = paper_write_api
    owner = slots["콴텍"]
    other = slots["키움"]
    headers = _headers(owner)
    assert _submit(client, owner, headers=headers).status_code == 202

    preflight = _preflight(client, headers, slot_id=other)
    assert preflight.status_code == 409
    assert preflight.json()["detail"]["code"] == "scope_conflict"
    approve = _transition(client, "approve", headers, slot_id=other)
    assert approve.status_code == 409
    assert approve.json()["detail"]["code"] == "scope_conflict"
    assert writer.get_record(headers["X-Approval-ID"], scope_value=owner).state == "pending"

    mismatch_headers = _headers(owner)
    mismatch = _submit(
        client,
        owner,
        headers=mismatch_headers,
        body=_body(
            other,
            payload={
                "slot": other,
                "ticker": "000660",
                "name": "SK하이닉스",
                "quantity": 1,
                "price": 200_000,
            },
        ),
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "scope_conflict"


@pytest.mark.parametrize(
    "body,status,code",
    [
        (
            {"operation": "paper.buy", "expected_version": 0, "payload": {"slot": 1}},
            400,
            "missing_field",
        ),
        (
            _body(
                1,
                payload={
                    "slot": 1,
                    "ticker": "5930",
                    "name": "삼성전자",
                    "quantity": 1,
                    "price": 80_000,
                },
            ),
            422,
            "invalid_paper_ticker",
        ),
        (
            _body(
                1,
                operation="schedule.delete",
                payload={"event_id": 1},
            ),
            400,
            "operation_not_allowed",
        ),
    ],
)
def test_paper_http_domain_validation(paper_write_api, body, status, code):
    client, _, slots = paper_write_api
    slot_id = slots["콴텍"]
    if isinstance(body.get("payload"), dict) and "slot" in body["payload"]:
        body = {**body, "payload": {**body["payload"], "slot": slot_id}}
    response = _submit(client, slot_id, body=body)
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


def test_business_state_conflicts_are_nonmutating_409(paper_write_api):
    client, writer, slots = paper_write_api
    slot_id = slots["IPO"]
    headers = _headers(slot_id)
    body = _body(
        slot_id,
        payload={
            "slot": slot_id,
            "ticker": "005930",
            "name": "삼성전자",
            "quantity": 1000,
            "price": 80_000,
            "fees": 0,
        },
    )
    before = _paper_state(writer.db_path, slot_id)
    assert _submit(client, slot_id, headers=headers, body=body).status_code == 202
    assert _transition(client, "approve", headers).status_code == 200
    applied = _transition(client, "apply", headers)
    assert applied.status_code == 409
    assert applied.json()["detail"]["code"] == "paper_insufficient_capital"
    assert _paper_state(writer.db_path, slot_id) == before
    assert writer.current_version(slot_id) == 0


def test_query_duplicate_json_oversize_and_transition_body_are_rejected(
    paper_write_api,
):
    client, writer, slots = paper_write_api
    slot_id = slots["콴텍"]
    headers = _headers(slot_id)
    assert client.post(
        "/v1/private/paper/write/intents?slot=1",
        headers=headers,
        json=_body(slot_id),
    ).status_code == 400
    duplicate = client.post(
        "/v1/private/paper/write/intents",
        headers=headers,
        content=(
            '{"operation":"paper.buy","operation":"paper.sell",'
            f'"expected_version":0,"payload":{{"slot":{slot_id},'
            '"ticker":"005930","quantity":1,"price":1}}}'
        ),
    )
    assert duplicate.status_code == 400
    assert client.post(
        "/v1/private/paper/write/intents",
        headers=headers,
        content=b"x" * 40_000,
    ).status_code == 413
    assert client.post(
        "/v1/private/paper/write/approvals/approve",
        headers=headers,
        json={"approved": True},
    ).status_code == 400
    assert _scoped_table_count(writer.db_path) == 0


def test_operational_cli_has_no_independent_paper_write_activation_option():
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "private_data_api.py"
    ).read_text(encoding="utf-8")
    assert 'parser.add_argument("--enable-paper' not in source
    assert "AI_AGENT_PRIVATE_PAPER_WRITE" not in source
    assert "runtime_bundle.paper_writes_enabled" in source
    assert "runtime_bundle.build_paper_api_writer()" in source
