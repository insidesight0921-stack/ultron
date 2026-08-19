"""User-facing private watchlist tool.

This is the only application layer allowed to translate a company name into a
KRX ticker before calling :mod:`watchlist_store`.  The resulting data remains
in the local ignored private database and is never exposed through MCP.
"""
from __future__ import annotations

from pathlib import Path

from invest_bot import resolve_ticker
import watchlist_store as store


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


def _resolve(query: str) -> tuple[str, str] | None:
    value = " ".join(str(query or "").split())
    if not value:
        return None
    return resolve_ticker(value)


def _format_items(items: list[store.WatchlistItem]) -> str:
    if not items:
        return "⭐ 등록된 관심종목이 없습니다."
    lines = [f"⭐ 관심종목 ({len(items)}개)"]
    lines.extend(f"{idx}. {item.name} ({item.ticker})" for idx, item in enumerate(items, 1))
    return "\n".join(lines)


def run(
    action: str,
    ticker_or_name: str | None = None,
    *,
    db_path: Path | str | None = None,
    source: str = "telegram",
) -> tuple[str, list[dict]]:
    """Execute ``add``, ``remove`` or ``list`` and return bot response/chunks."""
    action_n = str(action or "").strip().lower()
    path = _db_path(db_path)

    if action_n == "list":
        return _format_items(store.list_items(db_path=path)), []

    if action_n not in {"add", "remove"}:
        return "❌ 지원하지 않는 관심종목 작업입니다.", []

    query = " ".join(str(ticker_or_name or "").split())
    if not query:
        verb = "추가할" if action_n == "add" else "삭제할"
        return f"⚠️ {verb} 종목명이나 6자리 종목코드를 알려주세요.", []

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

