"""test_vkospi_calibrate.py — VKOSPI 분포 실측·임계값 재설정 (hermetic).

기존 임계값(>30 채권+10%p / <15 주식+10%p)은 **VKOSPI가 15~30을 오간다는
전제**에서 나왔다. 이 시장은 지수 일간 변동 상위가 +17.9%/−12.1%이고 외부
시세 기준 현재 VKOSPI가 50 근처다 — 그대로 붙이면 규칙이 아니라 상수가 된다.

이 프로젝트는 임계값을 바깥 값 그대로 쓴 전례가 있다(200일선 기울기 ±0.5%가
평가 구간 100일 내내 한 번도 안 바뀌었다).
"""
from __future__ import annotations

import vkospi_calibrate as v


# ─── 상수가 된 규칙을 잡아낸다 ───────────────────────


def test_an_always_high_series_is_flagged_as_constant():
    """전 기간 상단 초과면 붙이는 즉시 −10%p 되고 다시 안 올라온다."""
    cov = v.legacy_coverage([45.0] * 100)
    assert cov["constant_high"] is True and cov["above_pct"] == 100.0


def test_a_never_low_series_is_flagged_as_a_dead_branch():
    """하단 분기가 한 번도 안 걸리면 그 가지는 코드에만 있는 것이다."""
    cov = v.legacy_coverage([20.0] * 50 + [40.0] * 50)
    assert cov["dead_low"] is True and cov["below"] == 0


def test_a_series_that_triggers_nothing_is_flagged():
    cov = v.legacy_coverage([20.0] * 100)
    assert cov["constant_none"] is True


def test_a_healthy_series_is_not_flagged():
    vals = [10.0] * 30 + [20.0] * 40 + [40.0] * 30
    cov = v.legacy_coverage(vals)
    assert not cov["constant_high"] and not cov["dead_low"]
    assert cov["above"] == 30 and cov["below"] == 30


def test_the_report_names_the_constant_problem():
    text = v.format_report([45.0] * 100)
    assert "규칙이 아니라 상수" in text
    assert "죽은 가지" in text


# ─── 백분위 제안 ─────────────────────────────────────


def test_the_suggestion_splits_at_the_requested_percentiles():
    vals = list(range(1, 101))
    s = v.suggest([float(x) for x in vals])
    assert s["ok"] is True
    chk = v.legacy_coverage([float(x) for x in vals],
                            high=s["high"], low=s["low"])
    assert 15 <= chk["above_pct"] <= 25
    assert 15 <= chk["below_pct"] <= 25


def test_a_small_sample_refuses_to_suggest():
    """30일 미만이면 백분위가 흔들린다 — 그 값으로 예산을 정하면 안 된다."""
    s = v.suggest([30.0] * 10)
    assert s["ok"] is False and "30일 미만" in s["reason"]


def test_the_suggestion_is_not_rounded_to_pretty_numbers():
    """'30' 같은 떨어지는 숫자는 근거가 아니라 관습이다."""
    vals = [float(x) + 0.37 for x in range(1, 101)]
    s = v.suggest(vals)
    assert s["high"] != round(s["high"] / 10) * 10 or s["high"] % 1 != 0


def test_percentile_interpolates():
    assert v.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5


def test_percentile_of_empty_is_none():
    assert v.percentile([], 50) is None
    assert v.describe([])["n"] == 0
    assert "표본이 없습니다" in v.format_report([])


def test_none_values_are_dropped_not_counted():
    """결측을 0으로 세면 분포가 아래로 끌린다."""
    assert v.describe([10.0, None, 20.0])["n"] == 2


# ─── 실측 시나리오 ───────────────────────────────────


def test_the_expected_market_shape_shows_a_dead_low_branch():
    """외부 시세 기준 52주 범위가 18~98이면 `<15`는 한 번도 안 걸린다."""
    import random
    rng = random.Random(20260901)
    vals = [rng.uniform(18, 98) for _ in range(250)]
    cov = v.legacy_coverage(vals)
    assert cov["below"] == 0
    text = v.format_report(vals)
    assert "죽은 가지" in text


def test_the_report_says_not_to_wire_it_before_recalibrating():
    text = v.format_report([float(x) for x in range(20, 70)])
    assert "예산에 연결하지 않는다" in text
