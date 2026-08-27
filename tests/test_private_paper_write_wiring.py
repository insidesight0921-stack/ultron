from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_TELEGRAM_USER_ID", "1")

import private_data_api
import telegram_bot as tb


class FakeRuntimeBundle:
    def __init__(self, *, schedule: bool, paper: bool):
        self.schedule_writes_enabled = schedule
        self.paper_writes_enabled = paper
        self.activation_permit = object()
        self.paper_activation_permit = object()
        self.watchlist_writer = object()
        self.schedule_writer = object()
        self.paper_writer = object()
        self.common_client = object()
        self.common_executor = object()
        self.schedule_executor = object()
        self.paper_client = object()
        self.paper_executor = object()

    def build_api_writer(self):
        return self.watchlist_writer

    def build_schedule_api_writer(self):
        return self.schedule_writer

    def build_paper_api_writer(self):
        return self.paper_writer

    def build_client(self):
        return self.common_client

    def build_executor(self, client):
        assert client is self.common_client
        return self.common_executor

    def build_schedule_executor(self, client):
        assert client is self.common_client
        return self.schedule_executor

    def build_paper_client(self):
        return self.paper_client

    def build_paper_executor(self, client):
        assert client is self.paper_client
        return self.paper_executor


def test_private_api_runtime_wires_paper_only_for_v3(monkeypatch):
    calls = []
    monkeypatch.setattr(
        private_data_api,
        "create_app",
        lambda token, **kwargs: calls.append((token, kwargs)) or object(),
    )
    v2 = FakeRuntimeBundle(schedule=True, paper=False)
    private_data_api._create_runtime_app("token", v2)
    v2_kwargs = calls[-1][1]
    assert v2_kwargs["enable_watchlist_writes"] is True
    assert v2_kwargs["enable_schedule_writes"] is True
    assert "enable_paper_trade_writes" not in v2_kwargs

    v3 = FakeRuntimeBundle(schedule=True, paper=True)
    private_data_api._create_runtime_app("token", v3)
    v3_kwargs = calls[-1][1]
    assert v3_kwargs["paper_trade_writer"] is v3.paper_writer
    assert v3_kwargs["enable_paper_trade_writes"] is True
    assert v3_kwargs["paper_write_activation_permit"] is v3.paper_activation_permit


def test_telegram_runtime_v2_stays_paper_disabled_and_v3_is_atomic():
    v2 = FakeRuntimeBundle(schedule=True, paper=False)
    tb._configure_private_write_runtime(v2)
    assert tb._PRIVATE_WRITE_CLIENT is v2.common_client
    assert tb._PRIVATE_SCHEDULE_WRITE_EXECUTOR is v2.schedule_executor
    assert tb._PRIVATE_PAPER_WRITE_CLIENT is None
    assert tb._PRIVATE_PAPER_WRITE_EXECUTOR is None

    v3 = FakeRuntimeBundle(schedule=True, paper=True)
    tb._configure_private_write_runtime(v3)
    assert tb._PRIVATE_WRITE_CLIENT is v3.common_client
    assert tb._PRIVATE_SCHEDULE_WRITE_EXECUTOR is v3.schedule_executor
    assert tb._PRIVATE_PAPER_WRITE_CLIENT is v3.paper_client
    assert tb._PRIVATE_PAPER_WRITE_EXECUTOR is v3.paper_executor

    tb._configure_private_write_runtime(None)


def test_telegram_private_paper_execution_binds_identity_and_policy(monkeypatch):
    called = {}

    class Executor:
        def execute(self, **kwargs):
            called.update(kwargs)
            return SimpleNamespace(result={"side": "sell"}, replayed=False)

    monkeypatch.setattr(tb, "_PRIVATE_PAPER_WRITE_EXECUTOR", Executor())
    result = asyncio.run(
        tb._execute_private_paper_write(
            caller="telegram-intraday",
            action="sell",
            slot_id=2,
            actor_id="intraday-policy",
            source_event_id="20260827T0930",
            item_key="position-0",
            ticker="005930",
            quantity=1,
            price=80_000,
            notes="policy sell",
            user_approved=False,
            policy_approved=True,
        )
    )

    assert result.result == {"side": "sell"}
    assert called["identity"].caller == "telegram-intraday"
    assert called["identity"].operation == "paper.sell"
    assert called["identity"].slot_id == 2
    assert called["user_approved"] is False
    assert called["policy_approved"] is True


def test_v3_telegram_reads_have_no_direct_database_fallback(monkeypatch):
    class PaperClient:
        def list_paper_slots(self):
            return [{"id": 2, "name": "키움", "current_capital": 10_000}]

        def list_paper_positions(self, slot=None):
            return [{"slot_id": slot, "ticker": "005930", "quantity": 1}]

    monkeypatch.setattr(tb, "_PRIVATE_PAPER_WRITE_CLIENT", PaperClient())
    monkeypatch.setattr(
        tb._pdb,
        "list_slots",
        lambda: (_ for _ in ()).throw(AssertionError("direct slot read forbidden")),
    )
    monkeypatch.setattr(
        tb._pdb,
        "list_positions",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("direct position read forbidden")
        ),
    )
    monkeypatch.setattr(
        tb._pdb,
        "slot_summary",
        lambda slot_id: (_ for _ in ()).throw(
            AssertionError("direct slot summary forbidden")
        ),
    )

    assert tb._list_runtime_paper_slots()[0]["id"] == 2
    assert tb._list_runtime_paper_positions(slot=2)[0]["slot_id"] == 2
    assert tb._runtime_paper_slot_summary(2)["current_capital"] == 10_000
