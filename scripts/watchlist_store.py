"""Private watchlist persistence.

The watchlist is personal data (plan section 1.5), so it is stored only in the
ignored local ``data/private.db`` database.  This module deliberately contains
no MCP or network surface.  Name/ticker resolution belongs to the caller; the
store accepts only a validated six-digit KRX ticker.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "private.db"


@dataclass(frozen=True)
class WatchlistItem:
    ticker: str
    name: str
    source: str
    created_at: str


def _normalize_ticker(ticker: str) -> str:
    value = str(ticker or "").strip()
    if len(value) != 6 or not value.isdigit():
        raise ValueError("ticker는 6자리 숫자여야 합니다.")
    return value


def _normalize_text(value: str, *, field: str, max_length: int) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        raise ValueError(f"{field}은(는) 비어 있을 수 없습니다.")
    if len(normalized) > max_length:
        raise ValueError(f"{field}은(는) {max_length}자 이하여야 합니다.")
    return normalized


@contextmanager
def _conn(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                ticker     TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'local',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """
        )
        yield con
        con.commit()
    finally:
        con.close()


def _to_item(row: sqlite3.Row) -> WatchlistItem:
    return WatchlistItem(
        ticker=str(row["ticker"]),
        name=str(row["name"]),
        source=str(row["source"]),
        created_at=str(row["created_at"]),
    )


def add(
    ticker: str,
    name: str,
    *,
    source: str = "local",
    db_path: Path | str = DEFAULT_DB_PATH,
) -> tuple[WatchlistItem, bool]:
    """Add an item idempotently and return ``(item, created)``.

    A repeated ticker never creates a duplicate.  If the existing row only had
    the ticker as its display name, a later resolved company name enriches it.
    """
    ticker_n = _normalize_ticker(ticker)
    name_n = _normalize_text(name, field="name", max_length=100)
    source_n = _normalize_text(source, field="source", max_length=40)

    with _conn(db_path) as con:
        row = con.execute(
            "SELECT ticker, name, source, created_at FROM watchlist WHERE ticker = ?",
            (ticker_n,),
        ).fetchone()
        if row is not None:
            if row["name"] == ticker_n and name_n != ticker_n:
                con.execute(
                    "UPDATE watchlist SET name = ? WHERE ticker = ?",
                    (name_n, ticker_n),
                )
                row = con.execute(
                    "SELECT ticker, name, source, created_at FROM watchlist WHERE ticker = ?",
                    (ticker_n,),
                ).fetchone()
            return _to_item(row), False

        con.execute(
            "INSERT INTO watchlist(ticker, name, source) VALUES (?, ?, ?)",
            (ticker_n, name_n, source_n),
        )
        row = con.execute(
            "SELECT ticker, name, source, created_at FROM watchlist WHERE ticker = ?",
            (ticker_n,),
        ).fetchone()
        return _to_item(row), True


def remove(ticker: str, *, db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    """Remove one ticker.  Missing items return ``False`` without error."""
    ticker_n = _normalize_ticker(ticker)
    with _conn(db_path) as con:
        cur = con.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker_n,))
        return cur.rowcount > 0


def list_items(*, db_path: Path | str = DEFAULT_DB_PATH) -> list[WatchlistItem]:
    """Return items in insertion order."""
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT ticker, name, source, created_at "
            "FROM watchlist ORDER BY created_at, ticker"
        ).fetchall()
    return [_to_item(row) for row in rows]


def contains(ticker: str, *, db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    ticker_n = _normalize_ticker(ticker)
    with _conn(db_path) as con:
        row = con.execute(
            "SELECT 1 FROM watchlist WHERE ticker = ?", (ticker_n,)
        ).fetchone()
    return row is not None

