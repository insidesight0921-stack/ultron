from __future__ import annotations

import sqlite3
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import paper_db
from paper_trade_identity import build_paper_trade_write_identity
from private_data_api import create_app
from private_data_api_client import (
    PrivateAPIUnavailable,
    PrivateAPIWriteConflict,
    PrivateDataClient,
)
from private_paper_write_consumer import (
    PaperTradeApprovalRequired,
    PaperTradeConsumerDisabled,
    PaperTradeTerminalState,
    PaperTradeWriteExecutor,
)
from private_paper_write_store import PaperTradeWriteStore


TOKEN = "private-paper-consumer-token-32-characters-minimum"


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
    db = tmp_path / "paper-consumer" / "paper.db"
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
    opener = _Opener(TestClient(app))
    client = PrivateDataClient(
        token=TOKEN,
        opener=opener,
        paper_writes_enabled=True,
        paper_write_activation_permit=permit,
        paper_write_database_path=db,
    )
    executor = PaperTradeWriteExecutor(
        client,
        enabled=True,
        paper_write_activation_permit=permit,
    )
    return executor, client, writer, opener, permit, slots


def _identity(*, caller="paper-ui", action="buy", slot_id=1, event_id="event-1"):
    return build_paper_trade_write_identity(
        caller=caller,
        operation=f"paper.{action}",
        slot_id=slot_id,
        actor_id="actor-7",
        source_event_id=event_id,
        item_key="item-1",
    )


def _execute(
    executor,
    *,
    identity,
    slot_id,
    caller="paper-ui",
    action="buy",
    user_approved=True,
    policy_approved=False,
    quantity=10,
    price=80_000,
    name="삼성전자",
):
    return executor.execute(
        caller=caller,
        action=action,
        slot_id=slot_id,
        identity=identity,
        user_approved=user_approved,
        policy_approved=policy_approved,
        ticker="005930",
        name=name,
        quantity=quantity,
        price=price,
        fees=0,
    )


def _identity_args(identity):
    return {
        "operation": identity.operation,
        "slot_id": identity.slot_id,
        "request_id": identity.request_id,
        "approval_id": identity.approval_id,
        "idempotency_key": identity.idempotency_key,
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


def test_executor_is_disabled_by_default_before_client_call():
    class Client:
        paper_writes_enabled = True

        def preflight_paper_trade_write(self, **kwargs):
            raise AssertionError("must not call client")

    executor = PaperTradeWriteExecutor(Client())
    with pytest.raises(PaperTradeConsumerDisabled):
        _execute(executor, identity=_identity(), slot_id=1)


def test_executor_requires_enabled_client_and_matching_paper_permit(
    tmp_path, private_write_permit_factory
):
    disabled_client = PrivateDataClient(token=TOKEN)
    with pytest.raises(ValueError, match="activation permit"):
        PaperTradeWriteExecutor(
            disabled_client,
            enabled=True,
            paper_write_activation_permit=private_write_permit_factory(
                tmp_path / "disabled" / "paper.db"
            ),
        )

    db = tmp_path / "enabled" / "paper.db"
    permit = private_write_permit_factory(db)
    client = PrivateDataClient(
        token=TOKEN,
        paper_writes_enabled=True,
        paper_write_activation_permit=permit,
        paper_write_database_path=db,
    )
    with pytest.raises(ValueError, match="do not match"):
        PaperTradeWriteExecutor(
            client,
            enabled=True,
            paper_write_activation_permit=private_write_permit_factory(
                tmp_path / "other" / "paper.db"
            ),
        )


def test_explicit_and_policy_approval_modes_block_before_http(consumer_stack):
    executor, _, writer, opener, _, slots = consumer_stack
    slot_id = slots["콴텍"]
    before = _paper_state(writer.db_path, slot_id)

    with pytest.raises(PaperTradeApprovalRequired):
        _execute(
            executor,
            identity=_identity(slot_id=slot_id),
            slot_id=slot_id,
            user_approved=False,
        )
    intraday = _identity(
        caller="telegram-intraday", action="sell", slot_id=slot_id
    )
    with pytest.raises(PaperTradeApprovalRequired):
        _execute(
            executor,
            identity=intraday,
            slot_id=slot_id,
            caller="telegram-intraday",
            action="sell",
            user_approved=False,
            policy_approved=False,
            name=None,
        )
    with pytest.raises(ValueError, match="sells only"):
        executor.execute(
            caller="telegram-intraday",
            action="buy",
            slot_id=slot_id,
            identity=_identity(slot_id=slot_id),
            policy_approved=True,
            ticker="005930",
            name="삼성전자",
            quantity=1,
            price=80_000,
        )

    assert opener.calls == []
    assert _paper_state(writer.db_path, slot_id) == before


def test_bad_payload_and_identity_scope_are_rejected_before_http(consumer_stack):
    executor, _, writer, opener, _, slots = consumer_stack
    slot_a = slots["콴텍"]
    slot_b = slots["키움"]
    before_a = _paper_state(writer.db_path, slot_a)
    before_b = _paper_state(writer.db_path, slot_b)

    with pytest.raises(ValueError):
        _execute(
            executor,
            identity=_identity(slot_id=slot_a),
            slot_id=slot_a,
            quantity=0,
        )
    with pytest.raises(ValueError, match="scope does not match"):
        _execute(
            executor,
            identity=_identity(slot_id=slot_a),
            slot_id=slot_b,
        )

    assert opener.calls == []
    assert _paper_state(writer.db_path, slot_a) == before_a
    assert _paper_state(writer.db_path, slot_b) == before_b


def test_full_buy_flow_and_applied_replay(consumer_stack):
    executor, _, writer, opener, _, slots = consumer_stack
    slot_id = slots["콴텍"]
    identity = _identity(slot_id=slot_id)

    first = _execute(executor, identity=identity, slot_id=slot_id)
    replay = _execute(executor, identity=identity, slot_id=slot_id)

    assert first.state == "applied"
    assert first.replayed is False
    assert replay.state == "applied"
    assert replay.replayed is True
    assert replay.result == first.result
    assert writer.current_version(slot_id) == 1
    assert len(_paper_state(writer.db_path, slot_id)[2]) == 1
    assert [path for _, path in opener.calls].count(
        "/v1/private/paper/write/approvals/apply"
    ) == 1


@pytest.mark.parametrize("partial_state", ["pending", "approved"])
def test_executor_resumes_partial_request(consumer_stack, partial_state):
    executor, client, writer, _, _, slots = consumer_stack
    slot_id = slots["콴텍"]
    identity = _identity(slot_id=slot_id)
    args = _identity_args(identity)
    preflight = client.preflight_paper_trade_write(**args)
    client.submit_paper_trade_write(
        **args,
        expected_version=preflight["expected_version"],
        payload=_buy_payload(slot_id),
    )
    if partial_state == "approved":
        client.approve_paper_trade_write(
            slot_id=slot_id,
            request_id=identity.request_id,
            approval_id=identity.approval_id,
        )

    result = _execute(executor, identity=identity, slot_id=slot_id)

    assert result.state == "applied"
    assert result.replayed is True
    assert writer.current_version(slot_id) == 1


def test_executor_refuses_rejected_intent(consumer_stack):
    executor, client, writer, _, _, slots = consumer_stack
    slot_id = slots["콴텍"]
    identity = _identity(slot_id=slot_id)
    args = _identity_args(identity)
    client.submit_paper_trade_write(
        **args,
        expected_version=0,
        payload=_buy_payload(slot_id),
    )
    client.reject_paper_trade_write(
        slot_id=slot_id,
        request_id=identity.request_id,
        approval_id=identity.approval_id,
    )

    with pytest.raises(PaperTradeTerminalState) as error:
        _execute(executor, identity=identity, slot_id=slot_id)

    assert error.value.state == "rejected"
    assert writer.current_version(slot_id) == 0
    assert _paper_state(writer.db_path, slot_id)[2] == []


def test_executor_detects_pending_payload_drift(consumer_stack):
    executor, client, writer, _, _, slots = consumer_stack
    slot_id = slots["콴텍"]
    identity = _identity(slot_id=slot_id)
    args = _identity_args(identity)
    client.submit_paper_trade_write(
        **args,
        expected_version=0,
        payload=_buy_payload(slot_id, quantity=5),
    )

    with pytest.raises(PrivateAPIWriteConflict) as error:
        _execute(executor, identity=identity, slot_id=slot_id, quantity=10)

    assert error.value.code == "idempotency_conflict"
    assert writer.current_version(slot_id) == 0
    assert _paper_state(writer.db_path, slot_id)[2] == []


def test_user_buy_then_policy_approved_intraday_sell(consumer_stack):
    executor, _, writer, _, _, slots = consumer_stack
    slot_id = slots["콴텍"]
    _execute(
        executor,
        identity=_identity(slot_id=slot_id, event_id="buy-event"),
        slot_id=slot_id,
    )
    sell_identity = _identity(
        caller="telegram-intraday",
        action="sell",
        slot_id=slot_id,
        event_id="risk-cycle-1",
    )

    sold = _execute(
        executor,
        identity=sell_identity,
        slot_id=slot_id,
        caller="telegram-intraday",
        action="sell",
        user_approved=False,
        policy_approved=True,
        name=None,
        price=90_000,
    )

    assert sold.result["side"] == "sell"
    assert writer.current_version(slot_id) == 2
    assert [row[0] for row in _paper_state(writer.db_path, slot_id)[2]] == [
        "buy",
        "sell",
    ]


def test_domain_failure_and_api_outage_have_no_direct_db_fallback(
    consumer_stack, monkeypatch
):
    executor, _, writer, opener, _, slots = consumer_stack
    slot_id = slots["콴텍"]
    before = _paper_state(writer.db_path, slot_id)
    too_expensive = _identity(slot_id=slot_id, event_id="too-expensive")

    with pytest.raises(PrivateAPIWriteConflict) as error:
        _execute(
            executor,
            identity=too_expensive,
            slot_id=slot_id,
            quantity=2_000_000_000,
            price=1_000_000,
        )
    assert error.value.code == "paper_insufficient_capital"
    assert _paper_state(writer.db_path, slot_id) == before

    calls_before = len(opener.calls)
    monkeypatch.setattr(
        executor.client,
        "preflight_paper_trade_write",
        lambda **kwargs: (_ for _ in ()).throw(PrivateAPIUnavailable("offline")),
    )
    with pytest.raises(PrivateAPIUnavailable):
        _execute(
            executor,
            identity=_identity(slot_id=slot_id, event_id="offline"),
            slot_id=slot_id,
        )

    assert len(opener.calls) == calls_before
    assert _paper_state(writer.db_path, slot_id) == before
