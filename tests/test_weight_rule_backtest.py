"""test_weight_rule_backtest.py — 비중 규칙 백테스트.

이 파일이 지키는 것:
- **미래를 보지 않는다**(그날 종가로 그날 비중을 정하지 않는다)
- **비중을 낮추면 MDD는 당연히 준다** → 같은 평균 비중 고정이 대조군이다
- **뭉침을 보존한 채 정렬만 깬다**(무작위 셔플이 아니라 회전)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import weight_rule_backtest as wb  # noqa: E402


# ─── 이동평균·비중 ────────────────────────────────────

def test_the_average_is_none_until_the_window_fills():
    """**채워 넣지 않는다.** 창이 차기 전 값을 만들면 없는 판정이 생긴다."""
    got = wb.moving_average([1, 2, 3, 4], window=3)
    assert got[:2] == [None, None]
    assert got[2] == 2.0 and got[3] == 3.0


def test_the_weight_uses_yesterday_not_today():
    """**그날 종가를 보고 그날 하루를 다시 사는 것은 미래 참조다.**"""
    closes = [10, 10, 10, 100]          # 마지막 날 폭등
    w = wb.ma_weights(closes, window=2, above=0.7, below=0.5)
    assert w[0] is None                  # 첫날은 판정 없음
    # 폭등한 날(i=3)의 비중은 **전일까지**의 정보로 정해진다
    assert w[3] == 0.7


def test_a_fall_below_the_line_lowers_the_weight():
    closes = [10, 12, 14, 16, 5, 5]
    w = wb.ma_weights(closes, window=3, above=0.7, below=0.5)
    assert w[-1] == 0.5


def test_days_without_a_weight_are_dropped_not_zeroed():
    rets = [None, 0.01, 0.02]
    weights = [None, None, 0.7]
    assert wb.strategy_returns(rets, weights) == [0.02 * 0.7]


# ─── 성과 ────────────────────────────────────────────

def test_max_drawdown_is_a_positive_percent():
    assert wb.max_drawdown([100, 120, 60, 90]) == 50.0


def test_a_flat_curve_has_no_drawdown():
    assert wb.max_drawdown([100, 100, 100]) == 0.0


def test_total_return_and_sharpe():
    assert wb.total_return([100, 110]) == 10.0
    assert wb.sharpe([0.01, 0.01, 0.01]) is None       # 변동이 없으면 정의되지 않는다
    assert wb.sharpe([0.01]) is None


def test_switches_counts_only_changes():
    assert wb.switches([None, 0.7, 0.7, 0.5, 0.5, 0.7]) == 2


# ─── 대조군이 이 측정의 전부다 ─────────────────────────

def test_lowering_the_weight_alone_lowers_the_drawdown():
    """**이것이 이 파일의 이유다.**

    아무 규칙 없이 비중만 낮춰도 MDD는 준다. 그러니 「규칙이 MDD를
    줄였다」는 문장은 그 자체로 아무 말도 아니다.
    """
    rets = [None, 0.05, -0.20, 0.05, -0.10]
    high = wb.performance(rets, [None, 0.9, 0.9, 0.9, 0.9])
    low = wb.performance(rets, [None, 0.3, 0.3, 0.3, 0.3])
    assert low["mdd"] < high["mdd"]


def test_the_control_has_the_same_average_weight():
    weights = [None, 0.7, 0.5, 0.7, 0.5]
    avg = wb.average_weight(weights)
    assert avg == 0.6
    const = wb.constant_weights(weights, avg)
    assert wb.average_weight(const) == avg
    assert const[0] is None              # 비교 구간이 어긋나지 않는다


def test_the_comparison_includes_the_equal_average_control():
    rets = [None] + [0.01, -0.02, 0.03, -0.01] * 10
    weights = [None] + [0.7, 0.5, 0.7, 0.5] * 10
    table = wb.compare(rets, weights)
    assert any("같은 평균 비중" in k for k in table)


def test_the_report_warns_that_a_lower_weight_lowers_mdd():
    rets = [None] + [0.01, -0.02] * 10
    weights = [None] + [0.7, 0.5] * 10
    msg = wb.format_compare(wb.compare(rets, weights))
    assert "비중을 낮추면 MDD는 당연히 준다" in msg
    assert "같은 평균 비중" in msg


# ─── 회전 검정 ───────────────────────────────────────

def test_rotation_preserves_the_run_structure():
    """**무작위 셔플이 아니다.** 뭉침이 깨지면 무엇이든 유의해진다."""
    seq = [1, 1, 1, 0, 0, 0]
    got = wb.rotate(seq, 2)
    assert got == [1, 0, 0, 0, 1, 1]
    assert sorted(got) == sorted(seq)


def test_rotating_by_the_full_length_changes_nothing():
    assert wb.rotate([1, 2, 3], 3) == [1, 2, 3]


def test_a_short_sample_is_refused_not_judged():
    rets = [None, 0.01, 0.02]
    weights = [None, 0.7, 0.5]
    got = wb.rotation_test(rets, weights, wb._mdd_metric)
    assert got["percentile"] is None and got["reason"] == "표본 부족"


def test_the_rotation_test_returns_a_percentile_on_a_long_sample():
    rets = [None] + [0.01 if i % 3 else -0.02 for i in range(400)]
    weights = [None] + [0.7 if i % 50 < 30 else 0.5 for i in range(400)]
    got = wb.rotation_test(rets, weights, wb._return_metric)
    assert got["percentile"] is not None and 0 <= got["percentile"] <= 100
    assert got["trials"] > 10


def test_an_empty_series_does_not_crash():
    assert wb.rotate([], 3) == []
    assert wb.average_weight([None, None]) is None
    assert wb.max_drawdown([]) is None
