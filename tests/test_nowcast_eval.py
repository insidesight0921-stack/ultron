"""test_nowcast_eval.py — 지표가 다음 날 방향을 맞히는가 (hermetic, 순수 함수만).

2026-08-31 결정: 나우캐스팅 정답을 **월 단위 국면 → 일 단위 코스피 방향**으로
바꿨다. 월 단위로는 적중률 60%를 가리는 데만 23년이 걸린다(일 단위 1.1년).

이 도구의 실패 모드는 **시장 편향을 실력으로 읽는 것**이다. 실측 표본에서
코스피는 64.5%의 날에 올랐다 — "항상 상승"만 찍어도 62.6%가 나온다.
"""
from __future__ import annotations

import pytest  # noqa: E402
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
    # 표본은 MIN_PREDICTIONS 이상이어야 순열검정까지 간다(v3.64에서 게이트 추가).
    r = ne.evaluate(_preds("udud" * 10), _truth("uudd" * 10))
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
    """v3.64: VKOSPI +1, 원/달러 −1(후행 판명) → 검정 지표 4종, 필요 표본 276."""
    p = ne.progress(99)
    assert p["need"] == 276 and p["pct"] == 35.9
    assert p["trading_days_left"] == 177


def test_adding_an_indicator_costs_sample():
    """**지표 추가는 공짜가 아니다.** 여럿을 동시에 보면 그중 하나가 우연히
    잘 나올 확률이 커져 필요 표본이 는다. 늘려놓고 기준을 그대로 두면
    '우연히 잘 나온 지표'를 발견으로 오독한다."""
    assert ne.required_n(0.60, k=5) > ne.required_n(0.60, k=4)


def test_the_count_is_hypotheses_tested_not_indicators_collected():
    """**세는 것은 수집이 아니라 검정이다.** 원/달러는 계속 수집하지만 답이
    이미 나왔으므로(후행) 검정하지 않는다 — 이미 답이 난 질문을 계속 세면
    다른 지표의 표본만 축낸다. 줄었다고 기준이 느슨해진 것이 아니다."""
    import inspect

    src = inspect.getsource(ne._cli)
    assert "원/달러 환율" not in src, "검정 목록에 남아 있으면 k와 어긋난다"
    assert ne.N_INDICATORS == 4


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


# ─── 이겨야 할 상대는 동전이 아니다 (2026-09-01) ────
#
# progress()는 "적중률 60%를 무작위 50%와 가르려면 276건"이라고 표시해 왔다.
# 그런데 이 표본의 '항상 상승'은 **63.4%**다 — 60%는 목표가 아니라 기준선
# **미달**이었고, 진행률은 없는 목표를 향해 51.4%를 가리키고 있었다.


def test_the_bar_is_the_up_share_not_a_coin():
    truth = {f"d{i}": (ne.UP if i % 10 < 7 else ne.DOWN) for i in range(100)}
    assert ne.base_rate(truth) == pytest.approx(0.70)


def test_an_empty_truth_has_no_bar_rather_than_a_fake_one():
    assert ne.base_rate({}) is None


def test_beating_a_higher_bar_costs_far_more_sample():
    """기준선이 높을수록 같은 우위를 증명하기 어렵다."""
    easy = ne.required_n(0.60, 0.50, k=4)
    hard = ne.required_n(0.70, 0.634, k=4)
    assert hard > easy


def test_the_bar_table_speaks_in_margins_not_fixed_targets():
    """기준선은 표본마다 다르다 — 차이(%p)로 말해야 뜻이 유지된다."""
    rows = ne.bar_table(0.634, margins=(0.05, 0.10))
    assert rows[0]["target"] == pytest.approx(0.684)
    assert rows[0]["need"] > rows[1]["need"]      # 작은 우위일수록 표본이 많이 든다


def test_a_one_sided_sample_is_named_as_such():
    """**표본 수만 채우면 되는 게 아니다.** 국면이 안 바뀌면 판정할 수 없다."""
    truth = {f"d{i}": (ne.UP if i % 10 < 7 else ne.DOWN) for i in range(100)}
    warn = ne.regime_warning(truth)
    assert warn and "한 국면에 쏠려" in warn


def test_a_balanced_sample_gets_no_warning():
    truth = {f"d{i}": (ne.UP if i % 2 else ne.DOWN) for i in range(100)}
    assert ne.regime_warning(truth) is None


def test_a_one_sided_down_sample_is_also_named():
    truth = {f"d{i}": (ne.DOWN if i % 10 < 7 else ne.UP) for i in range(100)}
    warn = ne.regime_warning(truth)
    assert warn and "하락일" in warn


def test_the_report_leads_with_the_bar_not_with_a_progress_bar():
    truth = {f"d{i}": (ne.UP if i % 10 < 7 else ne.DOWN) for i in range(100)}
    msg = ne.format_report({}, truth)
    assert "이겨야 할 기준선" in msg
    assert "무작위 50%가 아니다" in msg


# ─── 관문이 둘이다 (2026-09-01) ─────────────────────
#
# "142/276건 (51.4%)"는 곧 될 것처럼 보였다. 실제로는 시장이 꺾이기 전까지
# 아무것도 판정할 수 없는 상태였다. 표본과 국면은 다른 관문이다.


def _one_sided(n=400):
    return {f"d{i}": (ne.UP if i % 10 < 7 else ne.DOWN) for i in range(n)}


def _balanced(n=400):
    return {f"d{i}": (ne.UP if i % 2 else ne.DOWN) for i in range(n)}


def test_a_full_sample_in_one_regime_is_still_not_ready():
    """**표본을 다 채워도** 국면이 하나면 판정할 수 없다."""
    r = ne.readiness(_one_sided(), n=100000)
    assert r["sample_ok"] is True
    assert r["regime_ok"] is False
    assert r["ok"] is False
    assert r["blocker"] == "국면"


def test_a_balanced_but_thin_sample_is_blocked_by_sample_size():
    r = ne.readiness(_balanced(), n=5)
    assert r["regime_ok"] is True
    assert r["sample_ok"] is False
    assert r["blocker"] == "표본"


def test_both_gates_open_means_ready():
    r = ne.readiness(_balanced(), n=100000)
    assert r["ok"] is True and r["blocker"] is None


def test_no_truth_is_not_silently_ready():
    assert ne.readiness({}, n=100000)["ok"] is False


def test_the_report_says_the_sample_count_is_not_the_blocker():
    """표본 수 표를 위에 두면 '며칠만 더 모으면 된다'로 읽힌다."""
    msg = ne.format_report({}, _one_sided())
    assert "지금은 표본 수가 문제가 아니다" in msg
    assert msg.index("표본 수가 문제가 아니다") < msg.index("기준선 +5%p")


# ─── 소급 가능한 지표는 로그를 기다리지 않는다 (2026-09-01) ──
#
# VIX는 494일 캐시가 있는데도 화면에 "로그 1일 — 적재 대기"가 떴다. 이미
# 359일을 평가할 수 있는 상태였다. **표본이 없는 것과 안 가져온 것은 다르다.**


def test_vix_is_evaluated_from_cache_not_from_the_log(tmp_path, monkeypatch):
    import json

    import price_sanity as ps

    d = tmp_path / "vix"
    d.mkdir(parents=True)
    (d / "vix_20260301.json").write_text(json.dumps({
        "series": {"date": ["20260101", "20260102", "20260103"],
                   "close": [12.0, 18.0, 30.0]}}), encoding="utf-8")
    monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
    out = dict(ne._vix_predictions(thresholds=(15.7, 20.6)))
    assert out["20260101"] == ne.UP      # 낮은 VIX = 위험선호
    assert out["20260103"] == ne.DOWN
    assert out["20260102"] is None       # 중립 밴드는 예측하지 않는다


def test_the_vix_predictor_is_empty_without_a_cache(tmp_path, monkeypatch):
    import price_sanity as ps

    monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
    assert ne._vix_predictions(thresholds=(15.7, 20.6)) == []


def test_a_tiny_sample_gets_no_hit_rate():
    """1건에서 100%는 결과가 아니라 잡음이다 — 외국인 순매수가 그렇게 떴다.

    (상수가 아닌 예측기로 검사한다 — 상수면 그쪽 진단이 먼저다.)
    """
    truth = {f"d{i}": ne.UP for i in range(50)}
    r = ne.evaluate([("d0", ne.UP), ("d1", ne.DOWN)], truth)
    assert r["n"] == 2
    assert r["constant"] is False
    assert r["rate"] is None
    assert "30건 미만" in r["verdict"]


def test_a_sufficient_sample_still_gets_a_rate():
    truth = {f"d{i}": (ne.UP if i % 2 else ne.DOWN) for i in range(80)}
    preds = [(f"d{i}", truth[f"d{i}"]) for i in range(80)]
    assert ne.evaluate(preds, truth)["rate"] == 100.0


def test_the_report_survives_a_none_rate():
    """표본 미달로 rate=None인 결과가 포맷 오류를 냈다(2026-09-01)."""
    truth = {f"d{i}": ne.UP for i in range(50)}
    results = {"작은 지표": ne.evaluate([("d0", ne.UP), ("d1", ne.DOWN)], truth)}
    msg = ne.format_report(results, truth)
    assert "30건 미만" in msg


def test_a_constant_diagnosis_is_not_hidden_by_a_thin_sample():
    """상수 여부는 예측기의 성질이라 표본 수와 무관하게 진단된다.
    게이트 순서를 뒤집었더니 이 진단이 '표본 미달'에 가려졌다(2026-09-01)."""
    truth = _truth("uuuud")
    r = ne.evaluate(_preds("uuuuu"), truth)
    assert r["constant"] is True
    assert "상수 예측" in r["verdict"]
    assert r["rate"] is None      # 다만 적중률은 표본 미달이라 감춘다
