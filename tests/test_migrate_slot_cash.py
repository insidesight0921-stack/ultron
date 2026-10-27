"""test_migrate_slot_cash.py — 슬롯 현금 정정 (hermetic, tmp DB만)."""
from __future__ import annotations

import sqlite3

import migrate_slot_cash as mig


def _db(tmp_path, slots, trades, seed=100_000_000):
    path = tmp_path / "paper.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE portfolios(id INTEGER PRIMARY KEY, seed_capital REAL)")
    con.execute("CREATE TABLE slots(id INTEGER PRIMARY KEY, name TEXT, "
                "allocation_pct REAL, current_capital REAL)")
    con.execute("CREATE TABLE trades(id INTEGER PRIMARY KEY, slot_id INTEGER, side TEXT, "
                "quantity INTEGER, price REAL, fees REAL)")
    con.execute("INSERT INTO portfolios VALUES (1, ?)", (seed,))
    for i, (name, pct, cap) in enumerate(slots, 1):
        con.execute("INSERT INTO slots VALUES (?,?,?,?)", (i, name, pct, cap))
    for t in trades:
        con.execute("INSERT INTO trades(slot_id, side, quantity, price, fees) VALUES (?,?,?,?,?)", t)
    con.commit(); con.close()
    return path


def _cash(path):
    con = sqlite3.connect(path)
    try:
        return {n: c for n, c in con.execute("SELECT name, current_capital FROM slots")}
    finally:
        con.close()


# 시드 3,500만 슬롯이 4,000만에서 출발해 매수 1건(100주×1만 + 수수료 1천)을 한 상태
TRADES = [(1, "buy", 100, 10_000, 1_000)]
REAL = [("키움", 0.35, 40_000_000 - 1_001_000), ("IPO", 0.20, 20_000_000)]


def test_expected_cash_matches_paper_db_formula():
    trades = [{"side": "buy", "quantity": 10, "price": 100, "fees": 5},
              {"side": "sell", "quantity": 10, "price": 120, "fees": 6}]
    assert mig.expected_cash(1000, trades) == 1000 - 1005 + 1194


def test_preview_finds_only_the_drifted_slot(tmp_path):
    items = mig.preview(_db(tmp_path, REAL, TRADES))
    assert [(i["name"], round(i["diff"])) for i in items] == [("키움", 5_000_000)]


def test_preview_does_not_write(tmp_path):
    path = _db(tmp_path, REAL, TRADES)
    mig.preview(path)
    assert _cash(path)["키움"] == 40_000_000 - 1_001_000


def test_apply_then_nothing_left_and_backup_and_record(tmp_path):
    path = _db(tmp_path, REAL, TRADES)
    done, backup, record = mig.apply(path, tmp_path / "migrations")
    assert len(done) == 1 and backup.exists() and record.exists()
    assert _cash(path)["키움"] == 35_000_000 - 1_001_000
    assert _cash(path)["IPO"] == 20_000_000
    assert mig.preview(path) == []
    assert _cash(backup)["키움"] == 40_000_000 - 1_001_000     # 사본은 적용 전


def test_apply_is_relative_so_a_later_trade_survives_restore(tmp_path):
    path = _db(tmp_path, REAL, TRADES)
    _, _, record = mig.apply(path, tmp_path / "migrations")
    con = sqlite3.connect(path)                    # 적용 뒤 봇이 매도 1건
    con.execute("INSERT INTO trades(slot_id, side, quantity, price, fees) VALUES (1,'sell',100,11000,1100)")
    con.execute("UPDATE slots SET current_capital = current_capital + ? WHERE id = 1", (1_098_900,))
    con.commit(); con.close()
    mig.restore(path, record)
    assert _cash(path)["키움"] == 40_000_000 - 1_001_000 + 1_098_900


def test_apply_is_idempotent(tmp_path):
    path = _db(tmp_path, REAL, TRADES)
    mig.apply(path, tmp_path / "migrations")
    done, _, _ = mig.apply(path, tmp_path / "migrations")
    assert done == []
    assert _cash(path)["키움"] == 35_000_000 - 1_001_000
