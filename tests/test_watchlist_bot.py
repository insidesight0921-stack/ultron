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
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=99),
        message=_Message("삼성전자 관심종목에 추가해줘"),
    )
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

    assert called == {"action": "add", "ticker_or_name": "삼성전자"}
    assert any("관심종목 처리 중" in text for text in update.message.notice.edits)
    assert update.message.notice.edits[-1].startswith("✅ 관심종목에 추가")
