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

import paper_db
from private_data_api import create_app
from private_data_api_client import (
    PrivateAPIAuthError,
    PrivateAPIError,
    PrivateAPIWriteConflict,
    PrivateAPIWriteDisabled,
    PrivateDataClient,
)
from private_paper_write_store import PaperTradeWriteStore


TOKEN = "private-paper-client-token-32-characters-minimum"


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
def paper_client(tmp_path, private_write_permit_factory):
    db = tmp_path / "paper-client" / "paper.db"
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
    opener = _TestAppOpener(TestClient(app))
    client = PrivateDataClient(
        token=TOKEN,
        opener=opener,
        paper_writes_enabled=True,
        paper_write_activation_permit=permit,
        paper_write_database_path=db,
    )
    return client, writer, opener, permit, slots


def _identity(prefix="paper-client"):
    return {
        "request_id": str(uuid4()),
        "approval_id": str(uuid4()),
        "idempotency_key": f"{prefix}-{uuid4().hex}",
    }


def _buy_payload(slot_id, *, quantity=10, price=80_000):
    return {
        "slot": slot_id,
        "ticker": "005930",
        "name": "삼성전자",
        "quantity": quantity,
        "price": price,
        "fees": 0,
    }


def _submit_buy(client, slot_id, identity, *, version=0, payload=None):
    return client.submit_paper_trade_write(
        operation="paper.buy",
        slot_id=slot_id,
        expected_version=version,
        payload=payload or _buy_payload(slot_id),
        **identity,
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


def test_paper_client_is_disabled_before_scope_validation_or_network():
    called = False

    def opener(request, timeout):
        nonlocal called
        called = True
        raise AssertionError("must not open")

    client = PrivateDataClient(token=TOKEN, opener=opener)
    identity = _identity()
    with pytest.raises(PrivateAPIWriteDisabled):
        _submit_buy(client, 0, identity)
    with pytest.raises(PrivateAPIWriteDisabled):
        client.preflight_paper_trade_write(
            operation="paper.buy", slot_id=0, **identity
        )
    with pytest.raises(PrivateAPIWriteDisabled):
        client.approve_paper_trade_write(
            slot_id=0,
            request_id=identity["request_id"],
            approval_id=identity["approval_id"],
        )
    assert called is False


def test_paper_client_requires_dedicated_database_bound_permit(
    tmp_path, private_write_permit_factory
):
    paper_db_path = tmp_path / "permit" / "paper.db"
    paper_permit = private_write_permit_factory(paper_db_path)
    assistant_permit = private_write_permit_factory(
        tmp_path / "permit-assistant" / "assistant.db"
    )
    with pytest.raises(ValueError, match="database path is required"):
        PrivateDataClient(token=TOKEN, paper_writes_enabled=True)
    with pytest.raises(ValueError, match="must be paper.db"):
        PrivateDataClient(
            token=TOKEN,
            paper_writes_enabled=True,
            paper_write_activation_permit=assistant_permit,
            paper_write_database_path=tmp_path / "assistant.db",
        )
    alias = tmp_path / "alias" / "paper.db"
    alias.parent.mkdir()
    alias.symlink_to(tmp_path / "permit-assistant" / "assistant.db")
    with pytest.raises(ValueError, match="must be paper.db"):
        PrivateDataClient(
            token=TOKEN,
            paper_writes_enabled=True,
            paper_write_activation_permit=assistant_permit,
            paper_write_database_path=alias,
        )
    with pytest.raises(ValueError, match="scope mismatch"):
        PrivateDataClient(
            token=TOKEN,
            paper_writes_enabled=True,
            paper_write_activation_permit=assistant_permit,
            paper_write_database_path=paper_db_path,
        )
    with pytest.raises(ValueError, match="require paper_writes_enabled"):
        PrivateDataClient(
            token=TOKEN,
            paper_write_activation_permit=paper_permit,
            paper_write_database_path=paper_db_path,
        )
    with pytest.raises(ValueError, match="must be separate"):
        PrivateDataClient(
            token=TOKEN,
            writes_enabled=True,
            write_activation_permit=paper_permit,
            paper_writes_enabled=True,
            paper_write_activation_permit=paper_permit,
            paper_write_database_path=paper_db_path,
        )


def test_paper_client_runs_buy_and_sell_approval_flows(paper_client):
    client, writer, opener, _, slots = paper_client
    slot_id = slots["콴텍"]
    buy_identity = _identity()
    before = _paper_state(writer.db_path, slot_id)

    preflight = client.preflight_paper_trade_write(
        operation="paper.buy", slot_id=slot_id, **buy_identity
    )
    pending = _submit_buy(
        client, slot_id, buy_identity, version=preflight["expected_version"]
    )
    assert pending["state"] == "pending"
    assert _paper_state(writer.db_path, slot_id) == before
    approved = client.approve_paper_trade_write(
        slot_id=slot_id,
        request_id=buy_identity["request_id"],
        approval_id=buy_identity["approval_id"],
    )
    assert approved["state"] == "approved"
    assert _paper_state(writer.db_path, slot_id) == before
    bought = client.apply_paper_trade_write(
        slot_id=slot_id,
        request_id=buy_identity["request_id"],
        approval_id=buy_identity["approval_id"],
    )
    assert bought["result"]["total_cost"] == 800_000.0

    sell_identity = _identity()
    client.submit_paper_trade_write(
        operation="paper.sell",
        slot_id=slot_id,
        expected_version=1,
        payload={
            "slot": slot_id,
            "ticker": "005930",
            "quantity": 10,
            "price": 90_000,
            "fees": 0,
        },
        **sell_identity,
    )
    client.approve_paper_trade_write(
        slot_id=slot_id,
        request_id=sell_identity["request_id"],
        approval_id=sell_identity["approval_id"],
    )
    sold = client.apply_paper_trade_write(
        slot_id=slot_id,
        request_id=sell_identity["request_id"],
        approval_id=sell_identity["approval_id"],
    )
    assert sold["result"]["proceeds"] == 900_000.0
    capital, positions, trades = _paper_state(writer.db_path, slot_id)
    assert capital == 40_100_000
    assert positions == []
    assert [row[0] for row in trades] == ["buy", "sell"]

    assert all("?" not in request["url"] for request in opener.seen)
    assert all(TOKEN not in request["url"] for request in opener.seen)
    assert all(
        request["headers"]["X-ai-agent-paper-slot-id"] == str(slot_id)
        for request in opener.seen
    )
    transitions = [request for request in opener.seen if "/approvals/" in request["url"]]
    assert all(request["body"] == b"" for request in transitions)


def test_paper_client_preflight_and_submit_replay_applied_result(paper_client):
    client, writer, _, _, slots = paper_client
    slot_id = slots["콴텍"]
    identity = _identity()
    pending = _submit_buy(client, slot_id, identity)
    client.approve_paper_trade_write(
        slot_id=slot_id,
        request_id=identity["request_id"],
        approval_id=identity["approval_id"],
    )
    client.apply_paper_trade_write(
        slot_id=slot_id,
        request_id=identity["request_id"],
        approval_id=identity["approval_id"],
    )
    existing = client.preflight_paper_trade_write(
        operation="paper.buy", slot_id=slot_id, **identity
    )
    replay = _submit_buy(
        client, slot_id, identity, version=pending["expected_version"]
    )
    assert existing["state"] == "applied"
    assert existing["result"]["slot_id"] == slot_id
    assert replay["replayed"] is True
    assert len(_paper_state(writer.db_path, slot_id)[2]) == 1


def test_paper_client_blocks_cross_slot_preflight_and_transition(paper_client):
    client, writer, _, _, slots = paper_client
    owner = slots["콴텍"]
    other = slots["키움"]
    identity = _identity()
    _submit_buy(client, owner, identity)

    with pytest.raises(PrivateAPIWriteConflict) as preflight_error:
        client.preflight_paper_trade_write(
            operation="paper.buy", slot_id=other, **identity
        )
    assert preflight_error.value.code == "scope_conflict"
    with pytest.raises(PrivateAPIWriteConflict) as transition_error:
        client.approve_paper_trade_write(
            slot_id=other,
            request_id=identity["request_id"],
            approval_id=identity["approval_id"],
        )
    assert transition_error.value.code == "scope_conflict"
    assert writer.get_record(identity["approval_id"], scope_value=owner).state == "pending"


def test_paper_client_rejects_invalid_scope_and_payload_before_network(paper_client):
    client, _, opener, _, slots = paper_client
    slot_id = slots["콴텍"]
    before = len(opener.seen)
    with pytest.raises(ValueError):
        _submit_buy(client, 0, _identity())
    with pytest.raises(ValueError, match="일치하지 않습니다"):
        _submit_buy(client, slot_id, _identity(), payload=_buy_payload(slots["키움"]))
    with pytest.raises(ValueError):
        client.submit_paper_trade_write(
            operation="paper.sell",
            slot_id=slot_id,
            expected_version=0,
            payload={"slot": slot_id, "ticker": "5930", "quantity": 1, "price": 1},
            **_identity(),
        )
    assert len(opener.seen) == before


def test_paper_client_reject_flow_never_mutates(paper_client):
    client, writer, _, _, slots = paper_client
    slot_id = slots["IPO"]
    identity = _identity()
    before = _paper_state(writer.db_path, slot_id)
    _submit_buy(client, slot_id, identity)
    rejected = client.reject_paper_trade_write(
        slot_id=slot_id,
        request_id=identity["request_id"],
        approval_id=identity["approval_id"],
    )
    assert rejected["state"] == "rejected"
    assert _paper_state(writer.db_path, slot_id) == before
    assert writer.current_version(slot_id) == 0


def test_paper_client_maps_auth_and_business_conflicts(paper_client):
    client, writer, opener, permit, slots = paper_client
    slot_id = slots["IPO"]
    unauthorized = PrivateDataClient(
        token="wrong-paper-client-token-32-characters-minimum",
        opener=opener,
        paper_writes_enabled=True,
        paper_write_activation_permit=permit,
        paper_write_database_path=writer.db_path,
    )
    with pytest.raises(PrivateAPIAuthError):
        _submit_buy(unauthorized, slot_id, _identity())

    identity = _identity()
    _submit_buy(
        client,
        slot_id,
        identity,
        payload=_buy_payload(slot_id, quantity=1000),
    )
    client.approve_paper_trade_write(
        slot_id=slot_id,
        request_id=identity["request_id"],
        approval_id=identity["approval_id"],
    )
    before = _paper_state(writer.db_path, slot_id)
    with pytest.raises(PrivateAPIWriteConflict) as conflict:
        client.apply_paper_trade_write(
            slot_id=slot_id,
            request_id=identity["request_id"],
            approval_id=identity["approval_id"],
        )
    assert conflict.value.code == "paper_insufficient_capital"
    assert _paper_state(writer.db_path, slot_id) == before


@pytest.mark.parametrize("mutation", ["extra_field", "wrong_total", "wrong_slot"])
def test_paper_client_rejects_non_allowlisted_or_inconsistent_results(
    mutation, tmp_path, private_write_permit_factory
):
    identity = _identity()
    result = {
        "side": "buy",
        "trade_id": 1,
        "slot_id": 1,
        "ticker": "005930",
        "quantity": 1,
        "price": 80_000.0,
        "fees": 0.0,
        "total_cost": 80_000.0,
    }
    response = {
        "request_id": identity["request_id"],
        "approval_id": identity["approval_id"],
        "operation": "paper.buy",
        "state": "applied",
        "expected_version": 0,
        "resource_version": 1,
        "result": result,
        "replayed": False,
    }
    if mutation == "extra_field":
        result["private_payload"] = "must be rejected"
    elif mutation == "wrong_total":
        result["total_cost"] = 1.0
    else:
        result["slot_id"] = 2
    db = tmp_path / mutation / "paper.db"
    permit = private_write_permit_factory(db)
    client = PrivateDataClient(
        token=TOKEN,
        opener=lambda request, timeout: _PayloadResponse(response),
        paper_writes_enabled=True,
        paper_write_activation_permit=permit,
        paper_write_database_path=db,
    )
    with pytest.raises(PrivateAPIError):
        _submit_buy(client, 1, identity, payload=_buy_payload(1, quantity=1))
