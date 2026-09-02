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


# ─── VKOSPI 오버레이(봇의 실제 규칙) ────────────────────

def test_the_overlay_matches_the_bot_rule():
    """`kium_bot.compute_weight_recommendation`과 같은 값을 내야 한다."""
    assert wb.vkospi_adjust(0.70, 70.0) == 0.60      # > HIGH → 채권 +10%p
    assert wb.vkospi_adjust(0.70, 15.0) == 0.80      # < LOW  → 주식 +10%p
    assert wb.vkospi_adjust(0.70, 30.0) == 0.70      # 중립


def test_an_unknown_vkospi_leaves_the_base_alone():
    """**모르는 날을 중립으로 세면 「미확보」가 하나의 판정이 된다.**"""
    assert wb.vkospi_adjust(0.50, None) == 0.50


def test_the_overlay_respects_the_clamp():
    assert wb.vkospi_adjust(0.85, 15.0) == 0.90      # 상한 90%
    assert wb.vkospi_adjust(0.35, 70.0) == 0.30      # 하한 30%


def test_no_base_means_no_weight():
    assert wb.vkospi_adjust(None, 70.0) is None


def test_the_combined_weight_uses_yesterdays_vkospi():
    """**오늘 VKOSPI를 보고 오늘 비중을 정하는 것은 미래 참조다.**"""
    closes = [10, 10, 10, 10, 10]
    days = ["d1", "d2", "d3", "d4", "d5"]
    vk = {"d4": 70.0}                     # d4에 공포 급등
    got = wb.combined_weights(closes, days, vk, window=2)
    assert got[4] == 0.60                 # d5 비중이 d4 값으로 정해진다
    assert got[3] == 0.70                 # d4 비중은 d3 값(없음)으로


def test_the_overlay_only_leg_keeps_the_same_eligible_days():
    """두 다리를 비교하려면 **판정 가능한 날이 같아야** 한다."""
    closes = [10, 11, 12, 13, 14]
    days = ["d1", "d2", "d3", "d4", "d5"]
    ma = wb.ma_weights(closes, window=3)
    only = wb.overlay_only_weights(closes, days, {}, window=3)
    assert [w is None for w in ma] == [w is None for w in only]


def test_the_overlay_only_leg_ignores_the_moving_average():
    """200일선 아래여도 VKOSPI만 보는 다리는 기준 비중을 유지한다."""
    closes = [20, 18, 16, 14, 12]          # 계속 하락 → 200일선 아래
    days = ["d1", "d2", "d3", "d4", "d5"]
    only = wb.overlay_only_weights(closes, days, {}, window=3, base=0.70)
    assert [w for w in only if w is not None] == [0.70, 0.70]


def test_restrict_keeps_series_aligned():
    days = ["a", "b", "c"]
    got_days, got_x = wb.restrict(days, [1, 2, 3], keep=lambda d: d != "b")
    assert got_days == ["a", "c"] and got_x == [1, 3]


def test_the_weight_is_rounded_where_the_bot_rounds_it():
    """봇은 `round(base_equity, 2)`로 내보낸다. 여기서 안 하면 0.7+0.1이
    0.7999999999999999가 되어 **봇이 실제로 쓰는 값과 어긋난다.**"""
    assert wb.vkospi_adjust(0.70, 15.0) == 0.80
    assert isinstance(wb.vkospi_adjust(0.50, 70.0), float)


# ─── 판정 원장 (2026-09-02) ────────────────────────────

def _table():
    return {"합친 규칙(200일선+VKOSPI)": {"n": 407, "avg_weight": 0.666, "switches": 43,
                                    "total_return": 106.7, "mdd": 24.50, "sharpe": 1.82},
            "VKOSPI만": {"n": 407, "avg_weight": 0.70, "switches": 38,
                        "total_return": 117.8, "mdd": 24.69, "sharpe": 1.87},
            "200일선만": {"n": 407, "avg_weight": 0.666, "switches": 7,
                        "total_return": 101.4, "mdd": 28.18, "sharpe": 1.63}}


def test_the_record_keeps_every_leg():
    """어느 다리가 일했는지가 이 측정의 결론이다 — 합계만 남기면 사라진다."""
    rec = wb.validation_record(_table(), at="2026-09-02T15:00:00", rule="combined",
                               mdd_test={"percentile": 89.2},
                               ret_test={"percentile": 59.2})
    assert set(rec["legs"]) == set(_table())
    assert rec["legs"]["VKOSPI만"]["sharpe"] == 1.87


def test_a_percentile_below_95_is_not_passed():
    rec = wb.validation_record(_table(), at="t", rule="combined",
                               mdd_test={"percentile": 89.2})
    assert rec["passed"] is False
    assert "미검증" in wb.verification_note(rec)


def test_a_percentile_at_95_passes():
    rec = wb.validation_record(_table(), at="t", rule="combined",
                               mdd_test={"percentile": 95.0})
    assert rec["passed"] is True
    assert "검증됨" in wb.verification_note(rec)


def test_the_note_names_the_lowest_drawdown_leg():
    note = wb.verification_note(
        wb.validation_record(_table(), at="t", rule="combined",
                             mdd_test={"percentile": 89.2}))
    assert "합친 규칙(200일선+VKOSPI)" in note


def test_no_record_says_unmeasured_not_verified():
    """**기록이 없는 것과 검증된 것은 다르다.**"""
    assert "미측정" in wb.verification_note(None)


def test_a_missing_ledger_does_not_break_the_bot(tmp_path):
    assert wb.load_validation(tmp_path / "없음.json") is None



def test_the_bot_reads_only_its_own_rule(tmp_path):
    """**다른 규칙의 진단이 마지막 줄이어도 자기 것처럼 보여주지 않는다.**

    리뷰(2026-09-02): ma_only `--record`가 마지막 줄이 되면 봇 화면이
    2,158일짜리 다른 규칙의 백분위 47.9를 자기 규칙 판정으로 띄웠다.
    """
    import finding_ledger as fl
    p = tmp_path / "w.json"
    fl.append(wb.validation_record(_table(), at="1", rule="combined",
                                   mdd_test={"percentile": 89.2}), p)
    fl.append(wb.validation_record(_table(), at="2", rule="ma_only",
                                   mdd_test={"percentile": 47.9}), p)
    got = wb.load_validation(p)
    assert got["rule"] == "combined" and got["mdd_percentile"] == 89.2


def test_the_note_has_no_markdown_stars():
    """주간 리포트는 parse_mode=HTML — 별표가 그대로 보인다."""
    note = wb.verification_note(
        wb.validation_record(_table(), at="t", rule="combined",
                             mdd_test={"percentile": 89.2}))
    assert "**" not in note
