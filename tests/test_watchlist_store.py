"""Private watchlist store tests (all DB access isolated under tmp_path)."""
from __future__ import annotations

import sqlite3

import pytest

import watchlist_store as ws


def test_add_and_list(tmp_path):
    db = tmp_path / "private.db"
    item, created = ws.add("005930", "삼성전자", source="telegram", db_path=db)

    assert created is True
    assert item.ticker == "005930"
    assert item.name == "삼성전자"
    assert item.source == "telegram"
    assert ws.list_items(db_path=db) == [item]


def test_add_is_idempotent_by_ticker(tmp_path):
    db = tmp_path / "private.db"
    first, first_created = ws.add("000660", "SK하이닉스", db_path=db)
    second, second_created = ws.add("000660", "다른 이름", db_path=db)

    assert first_created is True
    assert second_created is False
    assert second == first
    assert len(ws.list_items(db_path=db)) == 1


def test_add_enriches_ticker_only_name(tmp_path):
    db = tmp_path / "private.db"
    ws.add("035420", "035420", db_path=db)
    item, created = ws.add("035420", "NAVER", db_path=db)

    assert created is False
    assert item.name == "NAVER"


def test_remove_existing_and_missing(tmp_path):
    db = tmp_path / "private.db"
    ws.add("005930", "삼성전자", db_path=db)

    assert ws.remove("005930", db_path=db) is True
    assert ws.remove("005930", db_path=db) is False
    assert ws.list_items(db_path=db) == []


def test_contains(tmp_path):
    db = tmp_path / "private.db"
    ws.add("105560", "KB금융", db_path=db)

    assert ws.contains("105560", db_path=db) is True
    assert ws.contains("055550", db_path=db) is False


@pytest.mark.parametrize("ticker", ["", "5930", "ABCDEF", "0059300", None])
def test_rejects_invalid_ticker(tmp_path, ticker):
    with pytest.raises(ValueError, match="6자리"):
        ws.add(ticker, "삼성전자", db_path=tmp_path / "private.db")


@pytest.mark.parametrize("name", ["", "   ", None])
def test_rejects_blank_name(tmp_path, name):
    with pytest.raises(ValueError, match="name"):
        ws.add("005930", name, db_path=tmp_path / "private.db")


def test_schema_contains_only_private_watchlist_fields(tmp_path):
    db = tmp_path / "private.db"
    ws.add("005930", "삼성전자", db_path=db)

    with sqlite3.connect(db) as con:
        columns = [row[1] for row in con.execute("PRAGMA table_info(watchlist)")]

    assert columns == ["ticker", "name", "source", "created_at"]

