"""test_migrate_slot_allocation.py — 슬롯 비중 정정 (hermetic, tmp DB만).

되돌리기 어려운 작업이므로 미리보기가 기본이고, 적용은 백업을 먼저 뜬 뒤에만
한다. 적용 후 검증에 실패하면 스스로 되돌린다.
"""
from __future__ import annotations

import sqlite3

import migrate_slot_allocation as mig
import pytest


def _db(tmp_path, rows, seed=100_000_000):
    path = tmp_path / "paper.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE portfolios(id INTEGER PRIMARY KEY, seed_capital REAL)")
    con.execute("CREATE TABLE slots(id INTEGER PRIMARY KEY, name TEXT, "
                "allocation_pct REAL, current_capital REAL)")
    con.execute("INSERT INTO portfolios VALUES (1, ?)", (seed,))
    for i, (name, pct, cap) in enumerate(rows, 1):
        con.execute("INSERT INTO slots VALUES (?,?,?,?)", (i, name, pct, cap))
    con.commit(); con.close()
    return path


BROKEN = [("콴텍", 0.40, 36_000_000), ("키움", 0.40, 31_000_000),
          ("IPO", 0.20, 20_000_000), ("마이퀀트", 0.10, 10_000_000)]


# ─── 미리보기 ────────────────────────────────────────


def test_the_real_case_is_planned_correctly(tmp_path):
    p = mig.preview(_db(tmp_path, BROKEN))
    assert p["before"]["total"] == 1.1 and p["after"]["ok"]
    changed = {i["name"]: (i["from"], i["to"]) for i in p["plan"]}
    assert changed == {"콴텍": (0.40, 0.35), "키움": (0.40, 0.35)}


def test_an_already_correct_db_needs_no_change(tmp_path):
    ok = [("콴텍", 0.35, 1), ("키움", 0.35, 1), ("IPO", 0.20, 1), ("마이퀀트", 0.10, 1)]
    assert mig.preview(_db(tmp_path, ok))["plan"] == []


def test_an_unknown_slot_is_left_alone(tmp_path):
    """정의에 없는 슬롯을 임의로 건드리면 사용자가 만든 것이 사라진다."""
    rows = BROKEN + [("실험", 0.05, 1)]
    p = mig.preview(_db(tmp_path, rows))
    assert p["unknown"] == ["실험"]
    assert all(i["name"] != "실험" for i in p["plan"])


# ─── 적용 ────────────────────────────────────────────


def test_apply_fixes_the_total_and_leaves_a_backup(tmp_path):
    db = _db(tmp_path, BROKEN)
    p = mig.preview(db)
    backup = mig.apply(db, p)
    assert backup.exists()
    assert mig.preview(db)["before"]["ok"]


def test_apply_does_not_touch_capital_or_trades(tmp_path):
    """current_capital은 실제 매매의 결과다 — 비중만 바꾼다."""
    db = _db(tmp_path, BROKEN)
    before = {r["name"]: r["current_capital"] for r in mig._rows(db)}
    mig.apply(db, mig.preview(db))
    after = {r["name"]: r["current_capital"] for r in mig._rows(db)}
    assert before == after


def test_apply_refuses_when_the_result_would_still_be_wrong(tmp_path):
    """맞지도 않을 변경을 적용하면 상태만 헝클어진다."""
    rows = [("콴텍", 0.40, 1), ("키움", 0.40, 1)]      # IPO·마이퀀트 없음 → 70%
    db = _db(tmp_path, rows)
    with pytest.raises(SystemExit):
        mig.apply(db, mig.preview(db))


def test_restoring_the_backup_brings_the_old_values_back(tmp_path):
    import shutil
    db = _db(tmp_path, BROKEN)
    backup = mig.apply(db, mig.preview(db))
    assert mig.preview(db)["before"]["total"] == 1.0
    shutil.copy2(backup, db)
    assert mig.preview(db)["before"]["total"] == 1.1


def test_the_target_matches_the_code_definition(tmp_path):
    """마이그레이션 목표가 SLOT_DEFINITIONS와 어긋나면 다음 seed에서 되돌아간다."""
    import paper_db
    assert mig.TARGET == dict(paper_db.SLOT_DEFINITIONS)
    assert abs(sum(mig.TARGET.values()) - 1.0) < 1e-9
