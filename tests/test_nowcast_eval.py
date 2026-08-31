"""test_nowcast_eval.py — 지표가 다음 날 방향을 맞히는가 (hermetic, 순수 함수만).

2026-08-31 결정: 나우캐스팅 정답을 **월 단위 국면 → 일 단위 코스피 방향**으로
바꿨다. 월 단위로는 적중률 60%를 가리는 데만 23년이 걸린다(일 단위 1.1년).

이 도구의 실패 모드는 **시장 편향을 실력으로 읽는 것**이다. 실측 표본에서
코스피는 64.5%의 날에 올랐다 — "항상 상승"만 찍어도 62.6%가 나온다.
"""
from __future__ import annotations

import nowcast_eval as ne


def _truth(pattern: str) -> dict:
    """'uud' → {d0: up, d1: up, d2: down}"""
    return {f"d{i}": (ne.UP if c == "u" else ne.DOWN)
            for i, c in enumerate(pattern)}


def _preds(pattern: str):
    out = []
    for i, c in enumerate(pattern):
        out.append((f"d{i}", ne.UP if c == "u" else (ne.DOWN if c == "d" else None)))
    return out


# ─── 정답 시계열 ─────────────────────────────────────


def test_the_answer_comes_from_the_next_day():
    """t일 지표로 t+1을 맞히는지 본다 — 하루라도 밀리면 적중률이 가짜로 오른다."""
    s = ne.direction_series(["d0", "d1", "d2"], [100, 110, 105])
    assert s == [("d0", ne.UP), ("d1", ne.DOWN)]


def test_a_flat_day_has_no_direction():
    """보합을 억지로 한쪽에 넣으면 편향이 생긴다."""
    assert ne.direction_series(["d0", "d1"], [100, 100]) == []


def test_a_missing_close_is_skipped():
    assert ne.direction_series(["d0", "d1", "d2"], [100, 0, 120]) == []


def test_the_last_day_has_no_answer_yet():
    s = ne.direction_series(["d0", "d1"], [100, 110])
    assert len(s) == 1 and s[0][0] == "d0"


# ─── 채점 ────────────────────────────────────────────


def test_a_perfect_prediction_scores_full():
    r = ne.score(_preds("uud"), _truth("uud"))
    assert r["n"] == 3 and r["hits"] == 3 and r["rate"] == 100.0


def test_days_without_a_prediction_are_not_counted_as_hits():
    """판정 안 한 날을 맞힌 것으로 세면 적중률이 부풀려진다."""
    r = ne.score(_preds("u-d"), _truth("uud"))
    assert r["n"] == 2 and r["skipped"] == 1 and r["rate"] == 100.0


def test_coverage_shows_how_often_it_declines_to_call():
    r = ne.score(_preds("u--"), _truth("uud"))
    assert r["covered"] == round(1 / 3 * 100, 1)


def test_days_without_an_answer_are_ignored():
    r = ne.score(_preds("uu"), {"d0": ne.UP})
    assert r["n"] == 1


# ─── 상수 예측 (2026-08-31 실측에서 걸린 함정) ───────


def test_a_constant_predictor_is_flagged():
    """**순열검정으로는 잡히지 않는다.**

    라벨 구성을 보존한 채 날짜만 섞으므로, 라벨이 전부 같으면 섞어도 결과가
    같다 → 백분위가 항상 50이다. 실측에서 200일선 기울기가 평가 구간 100일
    내내 risk_on이라 '항상 상승'과 완전히 같은 예측이었는데, 순열검정은
    "우연 범위"라고만 했다. 잡아낸 것은 음성 대조였다.
    """
    truth = _truth("uuuud")
    r = ne.evaluate(_preds("uuuuu"), truth)
    assert r["constant"] is True
    assert "상수 예측" in r["verdict"]
    assert r["percentile"] is None       # 순열검정에 태우지 않는다


def test_a_varying_predictor_is_not_flagged_constant():
    r = ne.evaluate(_preds("udud"), _truth("uudd"))
    assert r["constant"] is False and r["percentile"] is not None


def test_is_constant_ignores_the_no_call_days():
    assert ne.is_constant(_preds("u-u-")) is True
    assert ne.is_constant(_preds("u-d-")) is False


def test_an_always_up_control_matches_the_market_drift():
    """상승장에서는 '항상 상승'만으로도 적중률이 높다 — 그게 기준선이다."""
    truth = _truth("uuuuuuuud")      # 8/9 상승
    r = ne.score(ne.always(ne.UP, truth), truth)
    assert r["rate"] == round(8 / 9 * 100, 1)


# ─── 순열 대조 ───────────────────────────────────────


def test_the_permutation_preserves_the_prediction_mix():
    """상승 예측 개수가 바뀌면, 상승장에서 '상승을 많이 찍는 지표'가 유리해진 것을
    실력으로 오독한다."""
    preds = _preds("uuddu")
    for sim in ne.permuted(preds, trials=5):
        assert sorted(p for _, p in sim) == sorted(p for _, p in preds)


def test_a_real_signal_beats_the_shuffle():
    truth = _truth("ud" * 20)
    r = ne.evaluate(_preds("ud" * 20), truth, trials=300)
    assert r["rate"] == 100.0 and r["percentile"] >= 95


def test_noise_stays_in_the_chance_range():
    """무작위 예측이 '기준선 초과'로 나오면 이 도구는 해롭다."""
    truth = _truth("uuduuddudu" * 4)
    r = ne.evaluate(_preds("uduuddudud" * 4), truth, trials=300)
    assert r["verdict"] == "우연 범위"


def test_no_prediction_yields_no_verdict():
    r = ne.evaluate(_preds("---"), _truth("uud"))
    assert r["n"] == 0 and r["verdict"] == "예측 없음"


# ─── 표본 계산 ───────────────────────────────────────


def test_the_required_sample_matches_the_plan_figures():
    """계획서에 적은 값과 어긋나면 둘 중 하나가 틀린 것이다."""
    assert ne.required_n(0.60, k=4) == 276
    assert ne.required_n(0.55, k=4) == 1112
    assert ne.required_n(0.65, k=4) == 121


def test_progress_does_not_hide_how_far_it_is():
    p = ne.progress(99)
    assert p["need"] == 276 and p["pct"] == 35.9
    assert p["trading_days_left"] == 177


def test_a_finished_sample_has_nothing_left():
    assert ne.progress(500)["trading_days_left"] == 0


# ─── 표시 ────────────────────────────────────────────


def test_the_report_states_the_market_drift():
    """편향을 안 밝히면 62.6%가 실력으로 보인다."""
    truth = _truth("uuuuuuuudd")
    text = ne.format_report({"x": ne.evaluate(_preds("uuuuuuuudd"), truth,
                                              trials=50)}, truth)
    assert "상승 8일(80.0%)" in text


def test_the_report_warns_that_permutation_misses_constants():
    truth = _truth("uud")
    text = ne.format_report({"x": ne.evaluate(_preds("uuu"), truth)}, truth)
    assert "상수 예측" in text and "음성 대조가 잡습니다" in text


def test_an_empty_truth_says_so():
    assert "정답 표본 없음" in ne.format_report({}, {})
