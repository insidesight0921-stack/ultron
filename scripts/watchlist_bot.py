"""User-facing private watchlist tool.

This is the only application layer allowed to translate a company name into a
KRX ticker before calling :mod:`watchlist_store`.  The resulting data remains
in the local ignored private database and is never exposed through MCP.
"""
from __future__ import annotations

import logging
from pathlib import Path

from invest_bot import resolve_ticker
from private_data_api_client import (
    PrivateAPIError,
    PrivateDataClient,
    private_api_enabled,
)
import watchlist_store as store
from telegram_write_identity import (
    TelegramWriteIdentity,
    validate_watchlist_write_identity,
)
from private_watchlist_write_consumer import WatchlistWriteExecutor


log = logging.getLogger("watchlist_bot")


def _db_path(db_path: Path | str | None) -> Path | str:
    return db_path if db_path is not None else store.DEFAULT_DB_PATH


def _find_stored(query: str, *, db_path: Path | str) -> store.WatchlistItem | None:
    normalized = " ".join(str(query or "").split()).casefold()
    if not normalized:
        return None
    for item in store.list_items(db_path=db_path):
        if normalized in {item.ticker.casefold(), item.name.casefold()}:
            return item
    return None


def _find_in_items(query: str, items: list) -> object | None:
    normalized = " ".join(str(query or "").split()).casefold()
    if not normalized:
        return None
    for item in items:
        if normalized in {
            _item_value(item, "ticker").casefold(),
            _item_value(item, "name").casefold(),
        }:
            return item
    return None


def _resolve(query: str) -> tuple[str, str] | None:
    value = " ".join(str(query or "").split())
    if not value:
        return None
    return resolve_ticker(value)


def _item_value(item, field: str) -> str:
    if isinstance(item, dict):
        return str(item[field])
    return str(getattr(item, field))


def _format_items(items: list) -> str:
    if not items:
        return "⭐ 등록된 관심종목이 없습니다."
    lines = [f"⭐ 관심종목 ({len(items)}개)"]
    lines.extend(
        f"{idx}. {_item_value(item, 'name')} ({_item_value(item, 'ticker')})"
        for idx, item in enumerate(items, 1)
    )
    return "\n".join(lines)


def _list_for_display(
    *,
    db_path: Path | str | None,
    private_client: PrivateDataClient | None,
) -> list:
    # 명시적 db_path는 테스트·복구 도구의 격리 경계이므로 API를 사용하지 않는다.
    if db_path is None and (private_client is not None or private_api_enabled()):
        try:
            return (private_client or PrivateDataClient()).list_watchlist()
        except (PrivateAPIError, ValueError) as exc:
            # Token·URL·Private 응답 본문은 로그에 넣지 않는다.
            log.warning(
                "Private API watchlist 읽기 실패, 로컬 DB fallback: %s",
                type(exc).__name__,
            )
    return store.list_items(db_path=_db_path(db_path))


def run(
    action: str,
    ticker_or_name: str | None = None,
    *,
    db_path: Path | str | None = None,
    source: str = "telegram",
    private_client: PrivateDataClient | None = None,
    write_identity: TelegramWriteIdentity | None = None,
    write_executor: WatchlistWriteExecutor | None = None,
    write_user_approved: bool = False,
) -> tuple[str, list[dict]]:
    """Execute ``add``, ``remove`` or ``list`` and return bot response/chunks."""
    action_n = str(action or "").strip().lower()
    path = _db_path(db_path)

    if action_n == "list":
        return _format_items(
            _list_for_display(db_path=db_path, private_client=private_client)
        ), []

    if action_n not in {"add", "remove"}:
        return "❌ 지원하지 않는 관심종목 작업입니다.", []

    if write_identity is not None:
        validate_watchlist_write_identity(write_identity, action=action_n)
    if write_executor is not None and db_path is not None:
        raise ValueError("explicit db_path and Private write executor cannot be combined")

    query = " ".join(str(ticker_or_name or "").split())
    if not query:
        verb = "추가할" if action_n == "add" else "삭제할"
        return f"⚠️ {verb} 종목명이나 6자리 종목코드를 알려주세요.", []

    if write_executor is not None:
        if write_identity is None:
            raise ValueError("Private write executor requires Telegram write identity")
        write_executor.require_ready(user_approved=write_user_approved)
        stored = None
        if action_n == "remove":
            stored = _find_in_items(query, write_executor.client.list_watchlist())
        if stored is not None:
            ticker = _item_value(stored, "ticker")
            name = _item_value(stored, "name")
        else:
            resolved = _resolve(query)
            if resolved is None:
                return f"⚠️ KRX 종목을 찾지 못했습니다: {query}", []
            ticker, name = resolved
        outcome = write_executor.execute(
            action=action_n,
            ticker=ticker,
            name=name,
            identity=write_identity,
            user_approved=write_user_approved,
        )
        if action_n == "add":
            created = bool(outcome.result["created"])
            prefix = "✅ 관심종목에 추가" if created else "ℹ️ 이미 관심종목에 있습니다"
            return f"{prefix}: {outcome.result['name']} ({outcome.result['ticker']})", []
        removed = bool(outcome.result["removed"])
        prefix = "✅ 관심종목에서 삭제" if removed else "ℹ️ 관심종목에 없는 종목입니다"
        return f"{prefix}: {name} ({outcome.result['ticker']})", []

    if action_n == "remove":
        existing = _find_stored(query, db_path=path)
        if existing is not None:
            removed = store.remove(existing.ticker, db_path=path)
            if removed:
                return f"✅ 관심종목에서 삭제: {existing.name} ({existing.ticker})", []

    resolved = _resolve(query)
    if resolved is None:
        return f"⚠️ KRX 종목을 찾지 못했습니다: {query}", []
    ticker, name = resolved

    if action_n == "add":
        item, created = store.add(ticker, name, source=source, db_path=path)
        if created:
            return f"✅ 관심종목에 추가: {item.name} ({item.ticker})", []
        return f"ℹ️ 이미 관심종목에 있습니다: {item.name} ({item.ticker})", []

    removed = store.remove(ticker, db_path=path)
    if removed:
        return f"✅ 관심종목에서 삭제: {name} ({ticker})", []
    return f"ℹ️ 관심종목에 없는 종목입니다: {name} ({ticker})", []
