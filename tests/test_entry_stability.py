"""test_entry_stability.py — 진입 조건 안정성 검사 (hermetic, 합성 시계열).

2026-08-29: 실거래 56건을 **아무 의미 없이** 무작위 4분할하니 "최고 그룹"이
절반의 확률로 승률 50%·평균 +17.6%였다(전체 39.3%·+4.5%). 조건이 아무 정보를
담지 않아도 그 정도는 나온다. 그래서 조건 성적을 혼자 보면 안 되고
"같은 개수를 아무 때나 샀으면 얼마였나"와 나란히 놓아야 한다.
"""
from __future__ import annotations

import random

import entry_backtest as eb


def _series(count=6, bars=300, drift=0.0005, vol=0.02, seed=1):
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        px, closes = 10000.0, []
        for _ in range(bars):
            px *= 1 + rng.gauss(drift, vol)
            closes.append(px)
        out.append({"closes": closes, "volumes": None})
    return out


# ─── 음성 대조 ───────────────────────────────────────


def test_the_unconditional_condition_lands_in_the_chance_range():
    """'무조건 진입'은 정의상 시점을 고르지 않는다 — 순열과 같아야 한다.

    이 검사가 여기서 통과를 내면 검사 자체가 고장난 것이다.
    """
    series = _series()
    fn = eb.CONDITIONS["베이스라인(무조건)"]
    b = eb.permuted_baseline(series, fn, trials=60)
    assert b["n"] > 0
    assert b["percentile_of_actual"] < 95


def test_the_baseline_average_is_close_to_the_actual_for_that_condition():
    series = _series()
    fn = eb.CONDITIONS["베이스라인(무조건)"]
    b = eb.permuted_baseline(series, fn, trials=60)
    assert abs(b["actual"] - b["avg_of_avgs"]) < 0.05


# ─── 경계 ────────────────────────────────────────────


def test_no_entries_is_undecidable_not_zero():
    """진입이 없으면 '성적 0'이 아니라 '판정 불가'다."""
    b = eb.permuted_baseline(_series(), lambda f: False, trials=10)
    assert b["n"] == 0 and b["percentile_of_actual"] is None


def test_a_short_series_does_not_crash():
    short = [{"closes": [10000.0] * (eb.MIN_HISTORY + 2), "volumes": None}]
    b = eb.permuted_baseline(short, eb.CONDITIONS["베이스라인(무조건)"], trials=5)
    assert b["trials"] >= 0


def test_the_same_seed_gives_the_same_answer():
    """무작위 검사가 실행마다 결론을 바꾸면 판단 근거가 될 수 없다."""
    series = _series()
    fn = eb.CONDITIONS["베이스라인(무조건)"]
    a = eb.permuted_baseline(series, fn, trials=30, seed=7)
    b = eb.permuted_baseline(series, fn, trials=30, seed=7)
    assert a == b


def test_entry_indices_respect_the_no_reentry_rule():
    """event_study와 같은 규칙이어야 기준선이 같은 조건에서 비교된다."""
    series = _series(count=1)
    closes = series[0]["closes"]
    fn = eb.CONDITIONS["베이스라인(무조건)"]
    idx = eb._entry_indices(closes, None, fn, eb.DEFAULT_HORIZON,
                            eb.DEFAULT_ARM, eb.DEFAULT_DROP)
    stat = eb.event_study(series, fn, eb.DEFAULT_HORIZON)
    assert len(idx) == stat["n"]
    assert idx == sorted(set(idx))          # 중복·역순 없음


# ─── 부트스트랩 ──────────────────────────────────────


def test_bootstrap_win_rates_sum_to_about_one_hundred():
    boot = eb.bootstrap_conditions(_series(), trials=20)
    assert abs(sum(r["win_rate"] for r in boot) - 100.0) < 1.5


def test_bootstrap_is_sorted_by_win_rate():
    boot = eb.bootstrap_conditions(_series(), trials=20)
    assert [r["win_rate"] for r in boot] == sorted(
        (r["win_rate"] for r in boot), reverse=True)


# ─── 표시 ────────────────────────────────────────────


def test_the_report_calls_the_chance_range_what_it_is():
    text = eb.format_stability(
        {"조건A": {"actual": 0.05, "avg_of_avgs": 0.048, "p95": 0.09,
                   "percentile_of_actual": 60.0, "n": 40}}, [])
    assert "우연 범위" in text and "백분위 60" in text


def test_the_report_marks_a_condition_that_clears_the_baseline():
    text = eb.format_stability(
        {"조건B": {"actual": 0.20, "avg_of_avgs": 0.02, "p95": 0.09,
                   "percentile_of_actual": 99.0, "n": 40}}, [])
    assert "기준선 초과" in text


def test_a_condition_without_a_sample_says_so():
    text = eb.format_stability({"조건C": {"n": 0, "percentile_of_actual": None}}, [])
    assert "판정 불가" in text


# ─── 음성 대조가 검사의 결함을 잡아낸 사례 (2026-08-29) ─
#
# 첫 구현은 무작위 시점 k개를 뽑아 **독립적으로** 시뮬레이션했다. 실제 조건은
# 청산할 때까지 다시 사지 않는데 무작위 쪽은 상승 구간의 여러 시점을 겹쳐 담을
# 수 있어 기준선이 유리해졌다. 실데이터에서 `베이스라인(무조건)`이 백분위 0으로
# 나와 들켰다 — 정의상 기준선과 같아야 하는 조건이었다.


def test_the_unconditional_condition_sits_at_the_middle():
    """진입확률 1.0이면 기준선과 **정확히 같은 값**이다 → 백분위 50.

    동률을 절반으로 세지 않으면 0이 되어 '우연보다 나쁨'으로 읽힌다.
    """
    series = _series()
    b = eb.permuted_baseline(series, eb.CONDITIONS["베이스라인(무조건)"], trials=20)
    assert b["rate"] == 1.0
    assert 45 <= b["percentile_of_actual"] <= 55


def test_the_baseline_uses_the_same_no_reentry_rule():
    """양쪽에 같은 규칙이 적용돼야 공정한 비교다."""
    import inspect
    src = inspect.getsource(eb.permuted_baseline)
    assert "event_study(" in src
    assert "_returns_at" not in src


def test_the_entry_rate_is_estimated_from_the_condition():
    series = _series()
    b = eb.permuted_baseline(series, lambda f: f["ma20_gap"] > 0, trials=10)
    assert 0 < b["rate"] <= 1.0
