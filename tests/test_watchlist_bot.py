"""Watchlist application tool and Telegram dispatch tests."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_TELEGRAM_USER_ID", "1")

import telegram_bot as tb
import watchlist_bot as wb


def test_telegram_private_write_runtime_is_default_disabled():
    assert tb._PRIVATE_WRITE_CLIENT is None
    assert tb._PRIVATE_WRITE_EXECUTOR is None
    assert tb._PRIVATE_SCHEDULE_WRITE_EXECUTOR is None
from private_data_api_client import PrivateAPIUnavailable


def test_add_list_remove_flow(monkeypatch, tmp_path):
    db = tmp_path / "private.db"
    monkeypatch.setattr(wb, "resolve_ticker", lambda q: ("005930", "삼성전자"))

    added, chunks = wb.run("add", "삼성전자", db_path=db)
    duplicate, _ = wb.run("add", "삼성전자", db_path=db)
    listed, _ = wb.run("list", db_path=db)
    removed, _ = wb.run("remove", "삼성전자", db_path=db)

    assert "추가" in added and "005930" in added
    assert chunks == []
    assert "이미" in duplicate
    assert "삼성전자 (005930)" in listed
    assert "삭제" in removed
    assert "없습니다" in wb.run("list", db_path=db)[0]


def test_remove_stored_name_does_not_require_network(monkeypatch, tmp_path):
    db = tmp_path / "private.db"
    monkeypatch.setattr(wb, "resolve_ticker", lambda q: ("000660", "SK하이닉스"))
    wb.run("add", "SK하이닉스", db_path=db)

    monkeypatch.setattr(
        wb,
        "resolve_ticker",
        lambda q: (_ for _ in ()).throw(AssertionError("resolver should not run")),
    )
    answer, _ = wb.run("remove", "SK하이닉스", db_path=db)
    assert "삭제" in answer


def test_unknown_company_is_not_persisted(monkeypatch, tmp_path):
    db = tmp_path / "private.db"
    monkeypatch.setattr(wb, "resolve_ticker", lambda q: None)

    answer, _ = wb.run("add", "없는회사", db_path=db)

    assert "찾지 못했습니다" in answer
    assert wb.run("list", db_path=db)[0].endswith("없습니다.")


def test_list_prefers_private_api_without_touching_local_db(monkeypatch):
    class Client:
        def list_watchlist(self):
            return [
                {
                    "ticker": "005930",
                    "name": "삼성전자",
                    "created_at": "2026-08-23 12:00:00",
                }
            ]

    monkeypatch.setattr(
        wb.store,
        "list_items",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("local DB must not be read")
        ),
    )
    answer, _ = wb.run("list", private_client=Client())
    assert "삼성전자 (005930)" in answer


def test_list_falls_back_to_local_db_when_private_api_is_unavailable(monkeypatch, tmp_path):
    db = tmp_path / "private.db"
    wb.store.add("000660", "SK하이닉스", db_path=db)

    class Client:
        def list_watchlist(self):
            raise PrivateAPIUnavailable("down")

    monkeypatch.setattr(wb.store, "DEFAULT_DB_PATH", db)
    answer, _ = wb.run("list", private_client=Client())
    assert "SK하이닉스 (000660)" in answer


@pytest.mark.parametrize("action", ["", "delete", "buy"])
def test_rejects_unknown_action(action, tmp_path):
    answer, _ = wb.run(action, "삼성전자", db_path=tmp_path / "private.db")
    assert "지원하지 않는" in answer


class _Notice:
    def __init__(self):
        self.edits: list[str] = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class _Message:
    def __init__(self, text):
        self.text = text
        self.notice = _Notice()
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return self.notice


def test_telegram_dispatches_watchlist_tool(monkeypatch):
    update = SimpleNamespace(
        update_id=7001,
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=99),
        message=_Message("삼성전자 관심종목에 추가해줘"),
    )
    update.message.message_id = 3001
    called = {}

    monkeypatch.setattr(tb, "is_authorized", lambda update: True)
    monkeypatch.setattr(tb.agent_bot, "is_compound_command", lambda text: False)
    monkeypatch.setattr(
        tb,
        "route",
        lambda text, history: {
            "tool": "watchlist_bot",
            "args": {"action": "add", "ticker_or_name": "삼성전자"},
            "mode": "fast",
        },
    )

    def fake_run(**kwargs):
        called.update(kwargs)
        return "✅ 관심종목에 추가: 삼성전자 (005930)", []

    monkeypatch.setattr(tb, "watchlist_run", fake_run)
    asyncio.run(tb.handle_text(update, None))

    assert called["action"] == "add"
    assert called["ticker_or_name"] == "삼성전자"
    identity = called["write_identity"]
    assert identity.operation == "watchlist.add"
    assert identity.idempotency_key.startswith("tg-watchlist-")
    assert any("관심종목 처리 중" in text for text in update.message.notice.edits)
    assert update.message.notice.edits[-1].startswith("✅ 관심종목에 추가")


@pytest.mark.parametrize("action", ["add", "delete", "complete"])
def test_telegram_dispatches_schedule_mutation_with_identity(monkeypatch, action):
    update = SimpleNamespace(
        update_id=8001,
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=99),
        message=_Message("일정 변경해줘"),
    )
    update.message.message_id = 4001
    called = {}
    args = {"action": action}
    if action == "add":
        args.update(title="회의", when_at="2026-09-01T10:00:00")
    else:
        args["event_id"] = 7

    monkeypatch.setattr(tb, "is_authorized", lambda update: True)
    monkeypatch.setattr(tb.agent_bot, "is_compound_command", lambda text: False)
    monkeypatch.setattr(
        tb,
        "route",
        lambda text, history: {"tool": "schedule_bot", "args": args, "mode": "fast"},
    )

    def fake_run(**kwargs):
        called.update(kwargs)
        return "✅ 일정 처리 완료", []

    monkeypatch.setattr(tb, "schedule_run", fake_run)
    asyncio.run(tb.handle_text(update, None))

    identity = called["write_identity"]
    assert identity.operation == f"schedule.{action}"
    assert identity.idempotency_key.startswith("tg-schedule-")
    assert called["chat_id"] == "99"


def test_telegram_schedule_read_has_no_write_identity(monkeypatch):
    update = SimpleNamespace(
        update_id=8002,
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=99),
        message=_Message("일정 보여줘"),
    )
    update.message.message_id = 4002
    called = {}

    monkeypatch.setattr(tb, "is_authorized", lambda update: True)
    monkeypatch.setattr(tb.agent_bot, "is_compound_command", lambda text: False)
    monkeypatch.setattr(
        tb,
        "route",
        lambda text, history: {
            "tool": "schedule_bot",
            "args": {"action": "list"},
            "mode": "fast",
        },
    )

    def fake_run(**kwargs):
        called.update(kwargs)
        return "📅 일정 없음", []

    monkeypatch.setattr(tb, "schedule_run", fake_run)
    asyncio.run(tb.handle_text(update, None))

    assert called["write_identity"] is None


def test_telegram_schedule_executor_is_injected_only_when_runtime_enabled(monkeypatch):
    update = SimpleNamespace(
        update_id=8003,
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=99),
        message=_Message("회의 일정 잡아줘"),
    )
    update.message.message_id = 4003
    called = {}
    executor = object()

    monkeypatch.setattr(tb, "is_authorized", lambda update: True)
    monkeypatch.setattr(tb.agent_bot, "is_compound_command", lambda text: False)
    monkeypatch.setattr(tb, "_PRIVATE_SCHEDULE_WRITE_EXECUTOR", executor)
    monkeypatch.setattr(
        tb,
        "route",
        lambda text, history: {
            "tool": "schedule_bot",
            "args": {
                "action": "add",
                "title": "회의",
                "when_at": "2026-09-01T10:00:00",
            },
            "mode": "fast",
        },
    )

    def fake_run(**kwargs):
        called.update(kwargs)
        return "✅ 일정 처리 완료", []

    monkeypatch.setattr(tb, "schedule_run", fake_run)
    asyncio.run(tb.handle_text(update, None))

    assert called["write_executor"] is executor
    assert called["write_user_approved"] is True
