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


# ─── 안정성 검사 (2026-08-27 추가) ───────────────────
#
# 그리드 1위만 보고 파라미터를 바꾸면 그 구간에 맞춘 값을 고르게 된다.
# 실제로 이 검사를 붙이자마자 전후반이 다른 조합을 고르는 것이 드러났다.

import exit_backtest as eb


def _rising(n=6, start=100.0, step=3.0):
    return ([start + step * i for i in range(n)], start)


def _falling(n=6, start=100.0, step=-3.0):
    return ([start + step * i for i in range(n)], start)


def test_combo_key_distinguishes_baseline():
    assert eb.combo_key({"arm": None, "drop": None}) == "baseline"
    assert eb.combo_key({"arm": 0.08, "drop": 0.07}) == "arm0.08/drop0.07"


def test_result_matrix_covers_every_combo_and_path():
    paths = [_rising(), _falling()]
    m = eb.result_matrix(paths, arms=(0.06, 0.08), drops=(0.05,))
    assert len(m) == 3                      # 베이스라인 + 2조합
    assert all(len(v) == 2 for v in m.values())


def test_result_matrix_matches_direct_simulation():
    """부트스트랩이 이 표를 재사용하므로 직접 계산과 같아야 한다."""
    paths = [_rising()]
    m = eb.result_matrix(paths, arms=(0.08,), drops=(0.05,))
    direct = eb.simulate_exit(paths[0][0], paths[0][1], arm=0.08, drop=0.05)["ret"]
    assert m[(0.08, 0.05)][0] == direct


def test_split_halves_flags_disagreement():
    """앞 절반은 오르기만, 뒤 절반은 내리기만 — 같은 조합이 이길 리 없다."""
    paths = [_rising() for _ in range(4)] + [_falling() for _ in range(4)]
    out = eb.split_halves(paths, arms=(0.06, 0.15), drops=(0.03, 0.10))
    assert out["usable"] and out["n_first"] == 4 and out["n_second"] == 4
    assert "agree" in out


def test_split_halves_refuses_tiny_samples():
    out = eb.split_halves([_rising()], arms=(0.08,), drops=(0.05,))
    assert not out["usable"] and "분할 불가" in out["reason"]


def test_bootstrap_is_deterministic_for_a_seed():
    paths = [_rising(), _falling(), _rising(start=50.0)]
    a = eb.bootstrap_winners(paths, arms=(0.06, 0.08), drops=(0.05,), rounds=30, seed=1)
    b = eb.bootstrap_winners(paths, arms=(0.06, 0.08), drops=(0.05,), rounds=30, seed=1)
    assert a == b


def test_bootstrap_win_rates_sum_to_100():
    paths = [_rising(), _falling(), _rising(start=50.0)]
    out = eb.bootstrap_winners(paths, arms=(0.06, 0.08), drops=(0.05,), rounds=50, seed=2)
    assert round(sum(r["win_rate"] for r in out)) == 100


def test_bootstrap_empty_for_single_path():
    assert eb.bootstrap_winners([_rising()], rounds=5) == []


def test_stability_report_warns_when_halves_disagree():
    halves = {"usable": True, "n_first": 3, "n_second": 3,
              "first": {"arm": 0.08, "drop": 0.07, "total_ret": 0.4},
              "second": {"arm": 0.06, "drop": 0.03, "total_ret": -0.3},
              "agree": False}
    text = eb.format_stability(halves, [])
    assert "구간에 맞춘 값일 수 있다" in text


def test_stability_report_says_when_halves_agree():
    same = {"arm": 0.08, "drop": 0.07, "total_ret": 0.4}
    halves = {"usable": True, "n_first": 3, "n_second": 3,
              "first": same, "second": dict(same), "agree": True}
    assert "같은 조합을 고름" in eb.format_stability(halves, [])


def test_stability_report_explains_low_win_rates():
    boot = [{"arm": 0.08, "drop": 0.07, "win_rate": 34.0}]
    assert "운일 가능성" in eb.format_stability({"usable": False, "reason": "x"}, boot)
