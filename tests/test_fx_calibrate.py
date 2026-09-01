"""test_fx_calibrate.py — '안 도는 가지'와 '죽은 가지'를 구분해서 다룬다.

환율 항은 임계값이 안 걸린 게 아니라 **변화율을 한 번도 계산하지 않았다**
(`fx_change = None`). 그런데도 지표 목록에는 이름이 올라 있어 밖에서 보면
지표 4개를 보고 있는 것처럼 보였다. 이 파일은 그 상태가 되돌아오는 것을 막는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fx_calibrate as fx  # noqa: E402
import nowcast_eval as ne  # noqa: E402


def test_the_head_of_the_change_series_is_none_not_zero():
    """0으로 채우면 '변화 없음'이라는 없는 사실이 생기고 발동률이 낮게 나온다."""
    out = fx.window_change_pct([100.0] * 5 + [110.0], window=3)
    assert out[:3] == [None, None, None]
    assert out[3] == pytest.approx(0.0)
    assert out[5] == pytest.approx(10.0)


def test_a_short_series_yields_no_change_at_all():
    assert fx.window_change_pct([100.0, 101.0], window=20) == [None, None]


def test_coverage_names_a_dead_branch():
    changes = [0.1, -0.2, 0.3] * 20
    cov = fx.coverage(changes, threshold=2.0)
    assert cov["dead_weak"] is True and cov["dead_strong"] is True
    assert cov["neutral"] == cov["n"]


def test_coverage_counts_both_sides():
    cov = fx.coverage([3.0, -3.0, 0.5], threshold=2.0)
    assert (cov["weak_krw"], cov["neutral"], cov["strong_krw"]) == (1, 1, 1)


def test_an_all_none_series_gets_no_verdict():
    assert fx.coverage([None, None], threshold=2.0) == {"n": 0}


# ─── 겹치는 날만 쓴다 ────────────────────────────────


def test_only_overlapping_days_are_compared():
    """환율은 휴장일에도 고시되는 날이 있다 — 억지로 맞추면 짝이 어긋난다."""
    days, a, b = fx.aligned({"d1": 1.0, "d2": 2.0, "d3": 3.0},
                            {"d2": 20.0, "d3": 30.0, "d9": 90.0})
    assert days == ["d2", "d3"]
    assert a == [2.0, 3.0] and b == [20.0, 30.0]


def test_a_thin_overlap_refuses_to_produce_a_correlation():
    out = fx.same_day_relation({"d1": 1.0}, {"d1": 2.0})
    assert out.get("corr") is None


# ─── 예측 방향 ───────────────────────────────────────


def test_weak_krw_predicts_down_and_strong_krw_predicts_up():
    """`proxy_indicators.fx_state`가 부호를 뒤집는 것과 같은 방향이어야 한다."""
    days = [f"2026{i:04d}" for i in range(1, 26)]
    rising = {d: 1000.0 + i * 5 for i, d in enumerate(days)}     # 원화 약세
    preds = dict(fx.next_day_predictions(rising, window=20, threshold=2.0))
    assert preds[days[-1]] == ne.DOWN
    falling = {d: 1000.0 - i * 5 for i, d in enumerate(days)}    # 원화 강세
    preds = dict(fx.next_day_predictions(falling, window=20, threshold=2.0))
    assert preds[days[-1]] == ne.UP


def test_the_neutral_band_predicts_nothing():
    days = [f"2026{i:04d}" for i in range(1, 26)]
    flat = {d: 1000.0 + (i % 2) for i, d in enumerate(days)}
    preds = dict(fx.next_day_predictions(flat, window=20, threshold=2.0))
    assert preds[days[-1]] is None


def test_days_before_the_window_predict_nothing():
    days = [f"2026{i:04d}" for i in range(1, 26)]
    rising = {d: 1000.0 + i * 5 for i, d in enumerate(days)}
    preds = dict(fx.next_day_predictions(rising, window=20, threshold=2.0))
    assert preds[days[0]] is None


# ─── 보고 문구 ───────────────────────────────────────


def test_the_report_separates_same_day_from_next_day():
    """①이 강해도 ②는 없을 수 있다 — 합치면 이미 일어난 일을 예측이라 부른다."""
    days = [f"2026{i:04d}" for i in range(1, 60)]
    fx_s = {d: 1300.0 + (i % 7) for i, d in enumerate(days)}
    kospi = {d: 2500.0 + (i % 5) * 3 for i, d in enumerate(days)}
    msg = fx.format_report(fx_s, kospi, window=20, threshold=2.0)
    assert "① 같은 날 관계" in msg
    assert "② 다음 날 관계" in msg
    assert msg.index("① 같은 날") < msg.index("② 다음 날")


def test_a_threshold_that_never_fires_is_named_in_the_report():
    days = [f"2026{i:04d}" for i in range(1, 60)]
    fx_s = {d: 1300.0 + (i % 3) * 0.1 for i, d in enumerate(days)}
    kospi = {d: 2500.0 + (i % 5) for i, d in enumerate(days)}
    msg = fx.format_report(fx_s, kospi, window=20, threshold=2.0)
    assert "한 번도 안 걸린다" in msg


# ─── ECOS 일간 조회 ─────────────────────────────────


def test_the_daily_cycle_is_supported_now():
    """v3.64 전까지 cycle='D'는 ValueError였고, 그래서 환율은 최신값뿐이었다."""
    import inspect

    import quant_bot as qb

    src = inspect.getsource(qb._fetch_ecos_series_raw)
    assert 'cycle == "D"' in src
    assert "%Y%m%d" in src


def test_the_monthly_path_is_unchanged():
    """일간을 열면서 월간 경로를 건드리면 콴텍 국면 판정이 조용히 바뀐다."""
    import inspect

    import quant_bot as qb

    src = inspect.getsource(qb._fetch_ecos_series_raw)
    assert "months + 3" in src
    assert 'end = today.strftime("%Y%m")' in src


# ─── 정렬이 틀리면 진짜 관계가 사라진다 (v3.64) ─────
#
# ECOS 731Y001은 매매기준율이고, 그 값이 **어느 날의 시장**을 반영하는지는
# 확인한 적이 없다. 하루 어긋난 채로 "같은 날 상관 +0.03"을 읽으면
# "관계 없음"이라고 결론 내리게 된다 — 실제로는 다른 lag에 있을 수 있다.


def _lagged(n=200, shift=0):
    """주가와 환율을 정확히 shift일 어긋나게 만든 인공 계열."""
    days = [f"2026{i:04d}" for i in range(1, n + 1)]
    import math
    k, f = {}, {}
    kv, fv = 2500.0, 1300.0
    for i, d in enumerate(days):
        kr = math.sin(i / 3.0) * 0.01
        kv *= 1 + kr
        k[d] = kv
        j = i - shift
        fr = -math.sin(j / 3.0) * 0.01 if 0 <= j else 0.0
        fv *= 1 + fr
        f[d] = fv
    return f, k


def test_the_scan_finds_the_alignment_when_there_is_no_shift():
    f, k = _lagged(shift=0)
    scan = fx.lag_scan(f, k)
    best = max(scan, key=lambda r: abs(r["corr"]))
    assert best["lag"] == 0
    assert best["corr"] < -0.5          # 원화 약세 ↔ 주가 하락


def test_the_scan_finds_a_one_day_shift():
    """하루 밀린 계열에서 lag 0만 보면 관계가 없다고 결론 내린다."""
    f, k = _lagged(shift=1)
    scan = fx.lag_scan(f, k)
    best = max(scan, key=lambda r: abs(r["corr"]))
    assert best["lag"] != 0
    at_zero = [r for r in scan if r["lag"] == 0][0]
    assert abs(at_zero["corr"]) < abs(best["corr"])


def test_a_lag_that_leaves_too_few_days_is_dropped_not_guessed():
    days = [f"2026{i:04d}" for i in range(1, 41)]
    f = {d: 1300.0 + i for i, d in enumerate(days)}
    k = {d: 2500.0 + i for i, d in enumerate(days)}
    out = fx.same_day_relation(f, k, lag=35)
    assert out.get("corr") is None


# ─── 밴드별 결과 ────────────────────────────────────


def test_band_outcomes_split_the_days_without_losing_any():
    days = [f"2026{i:04d}" for i in range(1, 80)]
    f = {d: 1300.0 * (1.001 ** i) for i, d in enumerate(days)}
    k = {d: 2500.0 + (i % 3) for i, d in enumerate(days)}
    bo = fx.band_outcomes(f, k, window=20, threshold=2.0)
    parts = bo["weak_krw"]["n"] + bo["neutral"]["n"] + bo["strong_krw"]["n"]
    assert parts <= bo["all"]["n"]          # 앞쪽 window는 밴드가 없다
    assert bo["all"]["n"] > 0


def test_a_shift_is_tested_against_random_draws_not_eyeballed():
    """표본이 작으면 10%p 차이는 흔하다 — 놀라기 전에 대조한다."""
    base = {"n": 300, "up": 190}
    same = {"n": 60, "up": 38}              # 전체와 같은 비율
    p_same = fx.shift_significance(same, base)
    assert p_same is not None and p_same > 0.3
    extreme = {"n": 60, "up": 15}           # 크게 낮다
    p_extreme = fx.shift_significance(extreme, base)
    assert p_extreme < 0.01


def test_a_tiny_band_gets_no_p_value_instead_of_a_fake_one():
    assert fx.shift_significance({"n": 5, "up": 1}, {"n": 300, "up": 190}) is None


def test_the_report_shows_the_band_split_and_the_base_rate():
    days = [f"2026{i:04d}" for i in range(1, 90)]
    f = {d: 1300.0 * (1.001 ** i) for i, d in enumerate(days)}
    k = {d: 2500.0 + (i % 5) * 2 for i, d in enumerate(days)}
    msg = fx.format_report(f, k, window=20, threshold=2.0)
    assert "③ 밴드별 다음 날 상승 비율" in msg
    assert "이겨야 할 기준" in msg
    assert "lag 스캔" in msg
