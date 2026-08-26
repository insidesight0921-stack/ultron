from __future__ import annotations

import hashlib
import sqlite3

import pytest

from private_read_store import (
    MAX_PAPER_PORTFOLIOS,
    MAX_PAPER_POSITIONS,
    MAX_PAPER_IPO_RECORDS,
    MAX_PAPER_SLOTS,
    MAX_PAPER_TRADES,
    MAX_SCHEDULE_EVENTS,
    MAX_WATCHLIST_ITEMS,
    PrivateReadStoreError,
    get_paper_myquant_tags,
    get_paper_performance,
    list_paper_ipo_records,
    list_paper_ipo_stats,
    list_paper_portfolios,
    list_paper_positions,
    list_paper_slots,
    list_paper_trades,
    list_schedule_events,
    list_watchlist_items,
)


def _db(path, rows=()):
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE watchlist (ticker TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "source TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        con.executemany(
            "INSERT INTO watchlist(ticker, name, source, created_at) VALUES (?, ?, ?, ?)",
            rows,
        )


def test_watchlist_read_is_allowlisted_and_does_not_modify_database(tmp_path):
    path = tmp_path / "assistant.db"
    _db(path, [("005930", "삼성전자", "telegram", "2026-08-23 12:00:00")])
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before_files = sorted(item.name for item in tmp_path.iterdir())

    assert list_watchlist_items(db_path=path) == [
        {
            "ticker": "005930",
            "name": "삼성전자",
            "created_at": "2026-08-23 12:00:00",
        }
    ]

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert sorted(item.name for item in tmp_path.iterdir()) == before_files


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(PrivateReadStoreError):
        list_watchlist_items(db_path=path)
    assert not path.exists()


def test_invalid_private_row_fails_closed(tmp_path):
    path = tmp_path / "assistant.db"
    _db(path, [("bad", "삼성전자", "telegram", "2026-08-23")])
    with pytest.raises(PrivateReadStoreError):
        list_watchlist_items(db_path=path)


def test_watchlist_size_is_bounded(tmp_path):
    path = tmp_path / "assistant.db"
    rows = [
        (f"{index:06d}", f"종목{index}", "test", "2026-08-23")
        for index in range(MAX_WATCHLIST_ITEMS + 1)
    ]
    _db(path, rows)
    with pytest.raises(PrivateReadStoreError, match="초과"):
        list_watchlist_items(db_path=path)


def _schedule_db(path, events=(), alerts=()):
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
            "when_at TEXT NOT NULL, notes TEXT, chat_id TEXT, completed INTEGER NOT NULL, "
            "rrule_freq TEXT, rrule_byday TEXT, rrule_until TEXT, "
            "pre_notify_minutes INTEGER NOT NULL DEFAULT 0)"
        )
        con.execute(
            "CREATE TABLE event_pre_notifications (event_id INTEGER NOT NULL, "
            "minutes_before INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0)"
        )
        con.executemany(
            "INSERT INTO events(id,title,when_at,notes,chat_id,completed,rrule_freq,"
            "rrule_byday,rrule_until,pre_notify_minutes) VALUES (?,?,?,?,?,?,?,?,?,?)",
            events,
        )
        con.executemany(
            "INSERT INTO event_pre_notifications(event_id,minutes_before,notified) "
            "VALUES (?,?,?)",
            alerts,
        )


def test_schedule_read_is_chat_scoped_allowlisted_and_read_only(tmp_path):
    path = tmp_path / "assistant.db"
    _schedule_db(
        path,
        events=[
            (1, "과거 일정", "2000-01-01T10:00:00", "메모", "111", 0, None, None, None, 0),
            (2, "미래 일정", "2099-01-01T10:00:00", None, "111", 0, "weekly", "MO", None, 30),
            (3, "다른 채팅", "2099-01-02T10:00:00", None, "222", 0, None, None, None, 0),
            (4, "완료 일정", "2099-01-03T10:00:00", None, "111", 1, None, None, None, 0),
        ],
        alerts=[(2, 5, 0), (2, 30, 0)],
    )
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before_files = sorted(item.name for item in tmp_path.iterdir())

    all_events = list_schedule_events("111", upcoming_only=False, db_path=path)
    upcoming = list_schedule_events("111", upcoming_only=True, db_path=path)

    assert [event["id"] for event in all_events] == [1, 2]
    assert [event["id"] for event in upcoming] == [2]
    assert set(upcoming[0]) == {
        "id", "title", "when_at", "notes", "completed", "rrule_freq",
        "rrule_byday", "rrule_until", "pre_notify_minutes",
        "pre_notify_minutes_list",
    }
    assert upcoming[0]["pre_notify_minutes_list"] == [30, 5]
    assert "chat_id" not in upcoming[0]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert sorted(item.name for item in tmp_path.iterdir()) == before_files


def test_schedule_requires_numeric_chat_scope(tmp_path):
    path = tmp_path / "assistant.db"
    _schedule_db(path)
    for chat_id in ("", "chat-alpha", "1" * 20):
        with pytest.raises(PrivateReadStoreError):
            list_schedule_events(chat_id, upcoming_only=False, db_path=path)


def test_schedule_size_is_bounded(tmp_path):
    path = tmp_path / "assistant.db"
    events = [
        (index, f"일정{index}", "2099-01-01T10:00:00", None, "111", 0, None, None, None, 0)
        for index in range(1, MAX_SCHEDULE_EVENTS + 2)
    ]
    _schedule_db(path, events=events)
    with pytest.raises(PrivateReadStoreError, match="초과"):
        list_schedule_events("111", upcoming_only=True, db_path=path)


def _paper_db(path, *, portfolios=(), slots=(), positions=(), trades=(), ipo_records=()):
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE portfolios (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "seed_capital REAL NOT NULL, created_at TEXT NOT NULL)"
        )
        con.execute(
            "CREATE TABLE slots (id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL, "
            "name TEXT NOT NULL, allocation_pct REAL NOT NULL, current_capital REAL NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        con.execute(
            "CREATE TABLE positions (id INTEGER PRIMARY KEY, slot_id INTEGER NOT NULL, "
            "ticker TEXT NOT NULL, name TEXT, quantity INTEGER NOT NULL, avg_price REAL NOT NULL, "
            "opened_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        con.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, slot_id INTEGER NOT NULL, "
            "ticker TEXT NOT NULL, name TEXT, side TEXT NOT NULL, quantity INTEGER NOT NULL, "
            "price REAL NOT NULL, fees REAL NOT NULL, notes TEXT, executed_at TEXT NOT NULL)"
        )
        con.execute(
            "CREATE TABLE ipo_records (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "sub_start TEXT, sub_end TEXT, listing_date TEXT, grade TEXT, score REAL, "
            "factors TEXT, subscribed INTEGER NOT NULL, alloc_amount REAL, "
            "listing_price REAL, return_pct REAL, notes TEXT, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        con.executemany("INSERT INTO portfolios VALUES (?,?,?,?)", portfolios)
        con.executemany("INSERT INTO slots VALUES (?,?,?,?,?,?)", slots)
        con.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)", positions)
        con.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)", trades)
        con.executemany("INSERT INTO ipo_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ipo_records)


def test_paper_reads_are_allowlisted_joined_and_read_only(tmp_path):
    path = tmp_path / "paper.db"
    _paper_db(
        path,
        portfolios=[(1, "모의투자", 100_000_000, "2026-08-24 09:00:00")],
        slots=[(2, 1, "콴텍", 0.4, 40_000_000, "2026-08-24 09:00:00")],
        positions=[
            (3, 2, "005930", "삼성전자", 5, 80_000, "2026-08-24 09:10:00", "2026-08-24 09:10:00")
        ],
    )
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before_files = sorted(item.name for item in tmp_path.iterdir())

    portfolios = list_paper_portfolios(db_path=path)
    positions = list_paper_positions(db_path=path)

    assert portfolios == [{
        "id": 1,
        "name": "모의투자",
        "seed_capital": 100_000_000.0,
        "created_at": "2026-08-24 09:00:00",
    }]
    assert set(positions[0]) == {
        "id", "slot_id", "ticker", "name", "quantity", "avg_price",
        "opened_at", "updated_at", "slot_name",
    }
    assert positions[0]["slot_name"] == "콴텍"
    assert "allocation_pct" not in positions[0]
    assert "current_capital" not in positions[0]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert sorted(item.name for item in tmp_path.iterdir()) == before_files


def test_paper_missing_or_invalid_data_fails_closed(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(PrivateReadStoreError):
        list_paper_positions(db_path=missing)
    assert not missing.exists()

    invalid = tmp_path / "invalid.db"
    _paper_db(
        invalid,
        portfolios=[(1, "모의투자", 1, "2026-08-24")],
        slots=[(1, 1, "콴텍", 1, 1, "2026-08-24")],
        positions=[(1, 1, "bad", None, 1, 1, "2026-08-24", "2026-08-24")],
    )
    with pytest.raises(PrivateReadStoreError):
        list_paper_positions(db_path=invalid)


def test_paper_read_sizes_are_bounded(tmp_path):
    portfolio_path = tmp_path / "many-portfolios.db"
    _paper_db(
        portfolio_path,
        portfolios=[
            (index, f"포트폴리오{index}", 1, "2026-08-24")
            for index in range(1, MAX_PAPER_PORTFOLIOS + 2)
        ],
    )
    with pytest.raises(PrivateReadStoreError, match="초과"):
        list_paper_portfolios(db_path=portfolio_path)

    position_path = tmp_path / "many-positions.db"
    _paper_db(
        position_path,
        portfolios=[(1, "모의투자", 1, "2026-08-24")],
        slots=[(1, 1, "콴텍", 1, 1, "2026-08-24")],
        positions=[
            (index, 1, f"{index:06d}", None, 1, 1, "2026-08-24", "2026-08-24")
            for index in range(1, MAX_PAPER_POSITIONS + 2)
        ],
    )
    with pytest.raises(PrivateReadStoreError, match="초과"):
        list_paper_positions(db_path=position_path)


def test_paper_slots_and_trades_are_allowlisted_aggregated_and_read_only(tmp_path):
    path = tmp_path / "paper.db"
    _paper_db(
        path,
        portfolios=[(1, "모의투자", 100_000_000, "2026-08-24 09:00:00")],
        slots=[(2, 1, "콴텍", 0.4, 39_000_000, "2026-08-24 09:00:00")],
        positions=[(3, 2, "005930", "삼성전자", 5, 80_000, "2026-08-24", "2026-08-24")],
        trades=[(4, 2, "005930", "삼성전자", "buy", 5, 80_000, 600, "테스트", "2026-08-24 09:10:00")],
    )
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before_files = sorted(item.name for item in tmp_path.iterdir())

    slots = list_paper_slots(db_path=path)
    trades = list_paper_trades(db_path=path)

    assert slots[0]["n_positions"] == 1
    assert slots[0]["n_trades"] == 1
    assert set(slots[0]) == {
        "id", "portfolio_id", "name", "allocation_pct", "current_capital",
        "created_at", "n_positions", "n_trades",
    }
    assert set(trades[0]) == {
        "id", "slot_id", "ticker", "name", "side", "quantity", "price",
        "fees", "notes", "executed_at", "slot_name",
    }
    assert trades[0]["slot_name"] == "콴텍"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert sorted(item.name for item in tmp_path.iterdir()) == before_files


def test_paper_slot_and_trade_sizes_are_bounded(tmp_path):
    slot_path = tmp_path / "many-slots.db"
    _paper_db(
        slot_path,
        portfolios=[(1, "모의투자", 1, "2026-08-24")],
        slots=[
            (index, 1, f"슬롯{index}", 0.1, 1, "2026-08-24")
            for index in range(1, MAX_PAPER_SLOTS + 2)
        ],
    )
    with pytest.raises(PrivateReadStoreError, match="초과"):
        list_paper_slots(db_path=slot_path)

    trade_path = tmp_path / "many-trades.db"
    _paper_db(
        trade_path,
        portfolios=[(1, "모의투자", 1, "2026-08-24")],
        slots=[(1, 1, "콴텍", 0.1, 1, "2026-08-24")],
        trades=[
            (index, 1, f"{index:06d}", None, "buy", 1, 1, 0, None, "2026-08-24")
            for index in range(1, MAX_PAPER_TRADES + 2)
        ],
    )
    trades = list_paper_trades(db_path=trade_path)
    assert len(trades) == MAX_PAPER_TRADES
    assert trades[0]["id"] == MAX_PAPER_TRADES + 1


def test_paper_ipo_records_and_stats_are_allowlisted_and_read_only(tmp_path):
    path = tmp_path / "paper.db"
    _paper_db(
        path,
        ipo_records=[
            (
                1, "공모주", "20260801", "20260802", "20260810", "A", 80.0,
                '{"offer_price": 10000}', 1, 1_000_000.0, 12_000.0, 20.0,
                "검증", "2026-08-01 09:00:00", "2026-08-10 09:00:00",
            )
        ],
    )
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before_files = sorted(item.name for item in tmp_path.iterdir())

    records = list_paper_ipo_records(db_path=path)
    stats = list_paper_ipo_stats(db_path=path)

    assert set(records[0]) == {
        "id", "name", "sub_start", "sub_end", "listing_date", "grade", "score",
        "factors", "subscribed", "alloc_amount", "listing_price", "return_pct",
        "notes", "created_at", "updated_at",
    }
    assert records[0]["factors"] == '{"offer_price": 10000}'
    assert stats == [{
        "grade": "A", "n": 1, "avg_return": 20.0, "min_return": 20.0,
        "max_return": 20.0, "n_pos": 1,
    }]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert sorted(item.name for item in tmp_path.iterdir()) == before_files


def test_paper_ipo_records_are_latest_bounded_and_malformed_fails_closed(tmp_path):
    path = tmp_path / "paper.db"
    _paper_db(
        path,
        ipo_records=[
            (
                index, f"공모주{index}", None,
                "9999-12-31" if index == MAX_PAPER_IPO_RECORDS + 1 else f"2026-08-{(index % 28) + 1:02d}",
                None,
                None, None, "not-json" if index == MAX_PAPER_IPO_RECORDS + 1 else None,
                0, None, None, None, None, "2026-08-01", "2026-08-01",
            )
            for index in range(1, MAX_PAPER_IPO_RECORDS + 2)
        ],
    )
    with pytest.raises(PrivateReadStoreError, match="factors"):
        list_paper_ipo_records(db_path=path)


def test_paper_performance_and_myquant_are_computed_without_exposing_or_writing_raw(tmp_path):
    path = tmp_path / "paper.db"
    _paper_db(
        path,
        portfolios=[(1, "모의투자", 1_000_000, "2026-08-24")],
        slots=[(1, 1, "마이퀀트", 0.5, 500_000, "2026-08-24")],
        trades=[
            (1, 1, "005930", "삼성전자", "buy", 10, 10_000, 0, "MQ[정배열]", "2026-08-01"),
            (2, 1, "005930", "삼성전자", "sell", 10, 11_000, 0, None, "2026-08-10"),
        ],
    )
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before_files = sorted(item.name for item in tmp_path.iterdir())

    performance = get_paper_performance(db_path=path)
    tags = get_paper_myquant_tags(db_path=path)

    assert performance[0]["n_closed"] == 1
    assert performance[0]["total_pnl"] == 10_000
    assert tags["tags"]["정배열"] == {"n": 1, "pnl": 10_000, "win_rate": 100.0}
    assert "정배열" in tags["text"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert sorted(item.name for item in tmp_path.iterdir()) == before_files
