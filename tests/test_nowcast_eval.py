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
    # v3.64: 판정이 「우연 범위」와 「음성 대조 미달」로 갈렸다. 둘 다 발견이
    # 아니다 — 문자열이 아니라 `is_finding`으로 묻는다.
    assert ne.is_finding(r) is False
    assert r["percentile"] < 95


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
    """v3.64: VKOSPI가 붙어 지표 4→5 → 필요 표본 276→289."""
    p = ne.progress(99)
    assert p["need"] == 289 and p["pct"] == 34.3
    assert p["trading_days_left"] == 190


def test_adding_an_indicator_costs_sample():
    """**지표 추가는 공짜가 아니다.** 여럿을 동시에 보면 그중 하나가 우연히
    잘 나올 확률이 커져 필요 표본이 는다. 늘려놓고 기준을 그대로 두면
    '우연히 잘 나온 지표'를 발견으로 오독한다."""
    assert ne.required_n(0.60, k=5) > ne.required_n(0.60, k=4)
    assert ne.N_INDICATORS == 5


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


# ─── 음성 대조를 판정에 넣는다 (v3.64) ──────────────
#
# 순열검정은 "이 지표의 예측 구성으로 무작위로 찍었을 때"와 비교할 뿐,
# **시장 상승 편향을 이기는지는 묻지 않는다.** 2026-08-31에 200일선 기울기가
# 그 틈으로 빠져나갈 뻔했고(상수라서 다른 경로로 잡혔다), 상수가 아닌 지표는
# 그 경로로도 안 잡힌다.


def test_the_control_is_measured_on_the_days_the_indicator_actually_predicted():
    """전체 기간의 상승 비율과 비교하면 안 된다 — 예측한 날의 성격이 다르다."""
    truth = {"d1": ne.UP, "d2": ne.UP, "d3": ne.DOWN, "d4": ne.DOWN}
    # d1·d2(상승)에는 예측을 안 하고, d3·d4(하락)에만 예측한다.
    preds = [("d1", None), ("d2", None), ("d3", ne.DOWN), ("d4", ne.UP)]
    assert ne.control_rate(preds, truth) == 0.0      # 예측한 날은 둘 다 하락
    # 전체 기간 기준이라면 50%가 나왔을 것이다.
    assert ne.score(ne.always(ne.UP, list(truth)), truth)["rate"] == 50.0


def test_an_indicator_that_loses_to_always_up_is_called_out():
    """순열은 통과해도 '항상 상승'을 못 이기면 방향 정보가 없다."""
    days = [f"d{i}" for i in range(40)]
    # 시장은 70% 상승. 지표는 상승·하락을 반반 찍는다 → 적중률이 70%에 못 미친다.
    truth = {d: (ne.UP if i % 10 < 7 else ne.DOWN) for i, d in enumerate(days)}
    preds = [(d, ne.UP if i % 2 == 0 else ne.DOWN) for i, d in enumerate(days)]
    r = ne.evaluate(preds, truth)
    assert r["constant"] is False
    assert r["control_rate"] > r["rate"]
    assert r["verdict"] == "음성 대조 미달"


def test_an_indicator_that_beats_the_control_is_not_called_out():
    days = [f"d{i}" for i in range(40)]
    truth = {d: (ne.UP if i % 2 == 0 else ne.DOWN) for i, d in enumerate(days)}
    preds = [(d, truth[d]) for d in days]            # 완벽한 예측
    r = ne.evaluate(preds, truth)
    assert r["rate"] == 100.0
    assert r["verdict"] != "음성 대조 미달"


# ─── VKOSPI 예측기 ──────────────────────────────────


def test_the_vkospi_band_maps_high_to_down_and_low_to_up(tmp_path, monkeypatch):
    import json
    import price_sanity as ps

    root = tmp_path / "indices"
    root.mkdir(parents=True)
    (root / "market_index_VKOSPI_20260901.json").write_text(json.dumps({
        "series": {"date": ["20260101", "20260102", "20260103"],
                   "close": [70.0, 40.0, 10.0]}}), encoding="utf-8")
    monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)

    out = dict(ne._vkospi_predictions(thresholds=(60.6, 20.7)))
    assert out["20260101"] == ne.DOWN     # 상단 초과 → 위험회피
    assert out["20260103"] == ne.UP       # 하단 미만 → 위험선호
    assert out["20260102"] is None        # **중립 밴드는 예측하지 않는다**


def test_the_vkospi_predictor_is_empty_without_a_cache(tmp_path, monkeypatch):
    import price_sanity as ps

    monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
    assert ne._vkospi_predictions(thresholds=(60.6, 20.7)) == []
