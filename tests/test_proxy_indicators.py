"""test_proxy_indicators.py — 실시간 대리 지표(순수) 검증 (hermetic, 네트워크 없음)."""
from __future__ import annotations

import proxy_indicators as pi


def _series(n, start=2500.0, step=1.0):
    return [start + step * i for i in range(n)]


# ─── 이동평균·기울기 ─────────────────────────────────


def test_moving_average_needs_a_full_window():
    assert pi.moving_average([1, 2, 3], 5) is None
    assert pi.moving_average([1, 2, 3, 4], 4) == 2.5


def test_slope_needs_window_plus_lookback():
    """200일선의 20일 전 값을 알려면 220일이 있어야 한다."""
    assert pi.ma_slope_pct(_series(219)) is None
    assert pi.ma_slope_pct(_series(220)) is not None


def test_rising_market_gives_a_positive_slope():
    assert pi.ma_slope_pct(_series(300, step=2.0)) > 0


def test_falling_market_gives_a_negative_slope():
    assert pi.ma_slope_pct(_series(300, step=-2.0)) < 0


def test_flat_market_slope_is_zero():
    assert pi.ma_slope_pct([2500.0] * 300) == 0.0


def test_slope_is_far_slower_than_price():
    """200일선은 가격보다 훨씬 느리다 — 그래서 잡음이 아니라 추세를 본다.

    (처음엔 '급락해도 200일선은 오를 수 있다'로 썼다가 실측에서 틀린 것을 확인했다.
     20일치가 평균 안으로 들어오면 200일선도 실제로 꺾인다. 성질은 '느리다'이지
     '안 꺾인다'가 아니다.)
    """
    closes = _series(280, step=2.0) + [2000.0] * 20
    price_drop = (closes[-1] / closes[-21] - 1) * 100
    slope = pi.ma_slope_pct(closes)
    assert slope < 0
    assert abs(slope) < abs(price_drop) / 5


def test_pct_change():
    assert pi.pct_change([100, 110], 1) == 10.0
    assert pi.pct_change([100], 1) is None
    assert pi.pct_change([0, 110], 1) is None


# ─── 방향 판정 ───────────────────────────────────────


def test_slope_states():
    assert pi.slope_state(1.0) == "risk_on"
    assert pi.slope_state(-1.0) == "risk_off"
    assert pi.slope_state(0.1) == "neutral"
    assert pi.slope_state(None) == "unknown"


def test_weak_won_is_risk_off_not_risk_on():
    """환율은 부호가 뒤집힌다 — 여기서 자주 틀린다."""
    assert pi.fx_state(3.0) == "risk_off"      # 환율 상승 = 원화 약세
    assert pi.fx_state(-3.0) == "risk_on"
    assert pi.fx_state(0.5) == "neutral"
    assert pi.fx_state(None) == "unknown"


def test_foreign_flow_direction():
    assert pi.flow_state(1000) == "risk_on"
    assert pi.flow_state(-1000) == "risk_off"
    assert pi.flow_state(0) == "neutral"
    assert pi.flow_state(None) == "unknown"


def test_vix_direction_is_inverted():
    assert pi.vix_state(12.0) == "risk_on"
    assert pi.vix_state(35.0) == "risk_off"
    assert pi.vix_state(22.0) == "neutral"
    assert pi.vix_state(None) == "unknown"


# ─── 지표 항목 ───────────────────────────────────────


def test_indicator_marks_availability():
    assert pi.indicator("x", 1.0, "risk_on")["available"] is True
    assert pi.indicator("x", None, "unknown")["available"] is False


# ─── 집계 ────────────────────────────────────────────


def _items(*states):
    return [pi.indicator(f"i{n}", None if s == "unknown" else 1.0, s)
            for n, s in enumerate(states)]


def test_summary_counts_directions():
    s = pi.summarize(_items("risk_on", "risk_on", "risk_off"))
    assert s["counts"]["risk_on"] == 2 and s["lean"] == "위험선호 우세"


def test_summary_reports_a_tie_as_mixed():
    assert pi.summarize(_items("risk_on", "risk_off"))["lean"] == "혼조"


def test_summary_without_any_data_refuses_to_lean():
    s = pi.summarize(_items("unknown", "unknown"))
    assert s["lean"] == "판정 불가" and s["n_available"] == 0


def test_summary_lists_missing_indicators():
    items = [pi.indicator("환율", None, "unknown"), pi.indicator("VIX", 20.0, "neutral")]
    assert pi.summarize(items)["missing"] == ["환율"]


def test_summary_does_not_produce_a_single_blended_number():
    """단위도 신뢰도도 다른 값을 하나로 합치면 어디서 왔는지 알 수 없게 된다."""
    s = pi.summarize(_items("risk_on", "risk_off", "neutral"))
    assert "score" not in s and "weighted" not in s


# ─── 표시 ────────────────────────────────────────────


def test_format_shows_missing_and_says_it_does_not_fill_them():
    snap = {"indicators": [pi.indicator("환율", None, "unknown"),
                           pi.indicator("VIX", 20.0, "neutral", unit="pt")],
            "summary": pi.summarize([pi.indicator("환율", None, "unknown"),
                                     pi.indicator("VIX", 20.0, "neutral")])}
    text = pi.format_snapshot(snap)
    assert "미확보: 환율" in text and "만들어 채우지 않습니다" in text


def test_format_shows_a_dash_for_missing_values():
    snap = {"indicators": [pi.indicator("환율", None, "unknown", unit="원")],
            "summary": pi.summarize([pi.indicator("환율", None, "unknown")])}
    assert "환율: —" in pi.format_snapshot(snap)


def test_format_carries_the_note_about_thresholds_being_hypotheses():
    snap = {"indicators": [], "summary": pi.summarize([])}
    assert "가설" in pi.format_snapshot(snap)


def test_format_marks_vix_as_a_stand_in():
    item = pi.indicator("VIX", 20.0, "neutral", unit="pt",
                        note="VKOSPI 대용. 같은 지표가 아님")
    text = pi.format_snapshot({"indicators": [item], "summary": pi.summarize([item])})
    assert "같은 지표가 아님" in text
