"""test_exit_backtest.py — 트레일링 백테스트 순수 코어 (hermetic, 합성 경로)."""
from __future__ import annotations

import exit_backtest as bt


def _path(*pcts):
    """수익률 시퀀스 → 진입가 100 기준 가격 경로."""
    return [100.0 * (1 + p) for p in pcts]


# ─── simulate_exit ───────────────────────────────────


def test_stop_line_exit():
    r = bt.simulate_exit(_path(0.01, -0.03, -0.08), 100.0, arm=None, drop=None)
    assert r["reason"] == "손절선" and r["day"] == 2
    assert round(r["ret"], 2) == -0.08


def test_hard_stop_gap():
    r = bt.simulate_exit(_path(-0.15), 100.0, arm=None, drop=None)
    assert r["reason"] == "급락" and r["day"] == 0


def test_take_profit_exit():
    r = bt.simulate_exit(_path(0.05, 0.21), 100.0, arm=None, drop=None)
    assert r["reason"] == "익절선" and r["day"] == 1


def test_expiry_when_no_trigger():
    r = bt.simulate_exit(_path(0.02, 0.05, 0.03), 100.0, arm=None, drop=None)
    assert r["reason"] == "만기" and round(r["ret"], 2) == 0.03


def test_trailing_locks_in_gain():
    # +12% 피크 후 +4%로 반납: 트레일링(arm10/drop7)은 +4%에서 익절,
    # 트레일링 없으면 이후 -8%까지 하락해 손절.
    prices = _path(0.12, 0.04, -0.08)
    with_trail = bt.simulate_exit(prices, 100.0, arm=0.10, drop=0.07)
    without = bt.simulate_exit(prices, 100.0, arm=None, drop=None)
    assert with_trail["reason"] == "트레일링" and with_trail["day"] == 1
    assert round(with_trail["ret"], 2) == 0.04
    assert without["reason"] == "손절선" and round(without["ret"], 2) == -0.08


def test_trailing_not_armed_below_threshold():
    r = bt.simulate_exit(_path(0.09, 0.01), 100.0, arm=0.10, drop=0.07)
    assert r["reason"] == "만기"


def test_empty_path():
    assert bt.simulate_exit([], 100.0, arm=None, drop=None)["reason"] == "만기"


# ─── grid_search / format ────────────────────────────


def test_grid_includes_baseline_and_sorted():
    paths = [(_path(0.12, 0.04, -0.08), 100.0), (_path(-0.08), 100.0)]
    rows = bt.grid_search(paths, arms=(0.10,), drops=(0.07,))
    assert len(rows) == 2  # 베이스라인 + 1조합
    assert rows[0]["total_ret"] >= rows[1]["total_ret"]
    trail_row = next(r for r in rows if r["arm"] == 0.10)
    assert trail_row["n_trail"] == 1 and trail_row["n_stop"] == 1
    base = next(r for r in rows if r["arm"] is None)
    assert base["n_trail"] == 0 and base["n_stop"] == 2


def test_grid_trailing_beats_baseline_on_giveback_path():
    paths = [(_path(0.12, 0.04, -0.08), 100.0)]
    rows = bt.grid_search(paths, arms=(0.10,), drops=(0.07,))
    assert rows[0]["arm"] == 0.10  # 이익 반납 경로에선 트레일링이 우위


def test_format_grid():
    paths = [(_path(0.12, 0.04, -0.08), 100.0)]
    out = bt.format_grid(bt.grid_search(paths, arms=(0.10,), drops=(0.07,)))
    assert "그리드서치" in out and "베이스라인" in out and "arm+10%" in out
