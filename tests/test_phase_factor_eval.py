"""test_phase_factor_eval.py — 탭 C: 부호만 본다.

평균 차이는 잴 수 없다는 것이 2026-09-02 실측이다(에피소드 18개, MDE
월 4.91%p). 부호로 질문을 바꾸되, 바꾸면서 세 가지가 무너지지 않게 한다:
가설의 사전 등록 · 우연 기대(50%가 아니다) · 발표 시차.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import phase_factor_eval as pf  # noqa: E402

WEIGHTS = {
    "Recovery":    {"Size": 0.20, "Momentum": 0.30},
    "Expansion":   {"Size": 0.10, "Momentum": 0.40},
    "Slowdown":    {"Size": 0.05, "Momentum": 0.10},
    "Contraction": {"Size": 0.00, "Momentum": 0.00},
}


# ─── 가설은 봇이 이미 써뒀다 ───────────────────────────

def test_the_hypothesis_comes_from_the_bots_own_weights():
    """**사후에 방향을 정하면 무엇이든 맞는다.**

    `PHASE_FACTOR_WEIGHT`는 이 측정보다 먼저 코드에 있었다. 국면별로
    Size를 얼마나 사는지가 곧 봇의 가설이다.
    """
    got = pf.expected_signs(WEIGHTS, "Size")
    assert got == {"Recovery": 1, "Expansion": 1, "Slowdown": -1, "Contraction": -1}


def test_a_flat_weight_makes_no_prediction():
    flat = {p: {"Size": 0.1} for p in ("Recovery", "Expansion", "Slowdown")}
    assert set(pf.expected_signs(flat, "Size").values()) == {0}


def test_a_missing_factor_makes_no_prediction():
    assert set(pf.expected_signs(WEIGHTS, "없는팩터").values()) == {0}


def test_the_real_bot_weights_predict_small_caps_in_the_upswing():
    """실제 코드가 이 방향을 담고 있는지 — 여기가 어긋나면 가설이 바뀐 것이다."""
    import quant_bot as qb
    got = pf.expected_signs(qb.PHASE_FACTOR_WEIGHTS_FALLBACK, "Size")
    assert got["Recovery"] == 1 and got["Expansion"] == 1
    assert got["Slowdown"] == -1 and got["Contraction"] == -1


def test_the_live_hypothesis_is_recorded_not_assumed():
    """**가중치는 wiki에서 온다 — 사람이 고칠 수 있다.**

    그러면 가설이 바뀐다. 바뀐 줄 모르고 옛 가설로 판정하면 사후 맞춤과
    구분되지 않으므로, 도구는 그날 쓴 값을 그대로 남겨야 한다.
    """
    import quant_bot as qb
    live = qb.parse_phase_weights_from_wiki()
    note = pf.hypothesis_note(live, "Size")
    assert "Size" in note
    for phase in ("Recovery", "Expansion", "Slowdown", "Contraction"):
        assert phase in note


# ─── 발표 시차 ───────────────────────────────────────

def test_the_phase_is_shifted_by_the_publication_lag():
    """M월 국면은 M+2월에야 알 수 있다."""
    got = pf.shift({"2020-01": "Expansion"}, 2)
    assert got == {"2020-03": "Expansion"}


def test_shifting_crosses_the_year_boundary():
    assert pf.shift({"2020-11": "X"}, 2) == {"2021-01": "X"}


def test_no_lag_leaves_it_alone():
    assert pf.shift({"2020-01": "X"}, 0) == {"2020-01": "X"}


def test_the_lag_default_matches_what_the_bot_actually_sees():
    assert pf.LAG_DEFAULT == 2


# ─── 에피소드 ────────────────────────────────────────

def test_consecutive_months_become_one_episode():
    phases = {"2020-01": "A", "2020-02": "A", "2020-03": "B"}
    assert pf.episodes(phases) == [("A", "2020-01", "2020-02"), ("B", "2020-03", "2020-03")]


def test_episodes_can_start_late():
    phases = {"2009-01": "A", "2010-01": "A", "2010-02": "B"}
    assert pf.episodes(phases, start="2010-01")[0] == ("A", "2010-01", "2010-01")


def test_an_episode_takes_the_mean_of_its_months():
    values = {"2020-01": 0.02, "2020-02": 0.04}
    rows = pf.episode_rows(values, [("A", "2020-01", "2020-02")])
    assert rows[0]["months"] == 2 and abs(rows[0]["mean"] - 0.03) < 1e-12
    assert rows[0]["sign"] == 1


def test_an_episode_without_data_is_dropped_not_zero():
    assert pf.episode_rows({"2020-01": 0.02}, [("A", "2025-01", "2025-12")]) == []


# ─── 우연 기대 ───────────────────────────────────────

def test_the_baseline_is_not_fifty_percent():
    """**늘 같은 부호로 찍어도 맞는다.** nowcast의 「늘 오른다 63.4%」와 같다."""
    rows = [{"sign": -1}] * 13 + [{"sign": 1}] * 5
    got = pf.constant_baseline(rows)
    assert got["n"] == 18 and abs(got["rate"] - 13 / 18) < 1e-12
    assert got["sign"] == -1


def test_the_bar_uses_the_larger_of_chance_and_a_coin():
    rows = [{"sign": -1}] * 13 + [{"sign": 1}] * 5
    line = pf.bar(rows)
    assert line["p0"] > 0.5
    assert line["need"] is not None and line["need"] > 13


def test_a_balanced_sample_falls_back_to_a_coin():
    rows = [{"sign": -1}] * 9 + [{"sign": 1}] * 9
    assert pf.bar(rows)["p0"] == 0.5


def test_the_bar_is_computed_before_the_answer():
    """합격선은 n과 우연 기대만으로 정해진다 — 적중 수가 들어가지 않는다."""
    rows = [{"sign": 1}] * 10 + [{"sign": -1}] * 8
    assert pf.bar(rows)["need"] == pf.bar(list(reversed(rows)))["need"]


# ─── 이항검정 ────────────────────────────────────────

def test_all_hits_is_the_smallest_p():
    assert pf.binom_p_ge(18, 18, 0.5) < pf.binom_p_ge(17, 18, 0.5)


def test_every_outcome_is_at_least_as_likely_as_certain():
    assert abs(pf.binom_p_ge(0, 10, 0.5) - 1.0) < 1e-12


def test_required_hits_for_a_coin_and_eighteen():
    """n=18, 우연 50%면 13/18(72.2%)부터 유의하다 — 얇다는 것을 숫자로 본다."""
    assert pf.required_hits(18, 0.5) == 13


def test_a_higher_baseline_demands_more():
    assert pf.required_hits(18, 0.72) > pf.required_hits(18, 0.5)


def test_an_impossible_bar_is_none():
    assert pf.required_hits(3, 0.95) is None


# ─── 판정 ────────────────────────────────────────────

def _rows(signs, phase="Expansion"):
    return [{"phase": phase, "start": "2020-01", "end": "2020-02",
             "months": 2, "mean": 0.01 * s, "sign": s} for s in signs]


def test_a_perfect_hit_rate_passes():
    rows = _rows([1] * 9) + _rows([-1] * 9, phase="Contraction")
    got = pf.verdict(rows, {"Expansion": 1, "Contraction": -1})
    assert got["hits"] == 18 and got["passed"] is True
    assert got["verdict"] == "우위 확인"


def test_matching_the_baseline_does_not_pass():
    """**늘 대형 우위로 찍은 것과 같은 성적은 국면이 한 일이 없다는 뜻이다.**

    부호 13:5로 음수가 많은 표본. 예측이 그 다수를 그대로 따라가면
    적중 13/18이 나오지만, 「늘 음수」도 13/18이다.
    """
    rows = (_rows([-1] * 13, phase="Contraction") + _rows([1] * 5, phase="Slowdown"))
    got = pf.verdict(rows, {"Contraction": -1, "Slowdown": -1})
    assert got["n"] == 18 and got["hits"] == 13
    assert abs(got["bar"]["p0"] - 13 / 18) < 1e-12
    assert got["passed"] is False


def test_a_thin_sample_refuses_to_report_a_rate():
    got = pf.verdict(_rows([1] * 5), {"Expansion": 1})
    assert got["verdict"] == "표본 부족" and got["passed"] is None


def test_ties_and_unpredicted_phases_are_excluded():
    rows = _rows([1, 0, -1]) + _rows([1], phase="없는국면")
    got = pf.score(rows, {"Expansion": 1})
    assert got["n"] == 2 and got["hits"] == 1


def test_the_report_says_it_is_not_a_proof_of_absence():
    rows = (_rows([-1] * 13, phase="Contraction") + _rows([1] * 5, phase="Slowdown"))
    expected = {"Contraction": -1, "Slowdown": -1}
    msg = pf.format_verdict(pf.verdict(rows, expected), expected, rows)
    assert "없다는 증명이 아닙니다" in msg


def test_the_report_shows_the_pre_registered_prediction():
    rows = _rows([1] * 18)
    expected = {"Expansion": 1}
    msg = pf.format_verdict(pf.verdict(rows, expected), expected, rows)
    assert "사전 등록된 예측" in msg
    assert "PHASE_FACTOR_WEIGHT" in msg


# ─── 여러 팩터를 동시에 재는 것의 대가 ─────────────────

def test_testing_four_factors_lowers_the_bar_for_each():
    """**넷을 α=0.05로 재면 하나라도 걸릴 확률이 18.5%다.**

    탭 D에서 「축마다 같은 검정을 대면 안 된다」고 적어둔 것과 같은 문제.
    """
    assert pf.family_alpha(4) == 0.05 / 4
    assert pf.family_alpha(1) == 0.05


def test_a_zero_or_negative_count_does_not_divide_by_zero():
    assert pf.family_alpha(0) == 0.05


def test_the_family_bar_is_stricter_than_the_single_bar():
    rows = _rows([1] * 11) + _rows([-1] * 7, phase="Contraction")
    single = pf.verdict(rows, {"Expansion": 1, "Contraction": -1})
    fam = pf.family_verdict({"Size": single, "Value": single,
                             "LowVol": single, "Dividend": single})
    got = fam["results"]["Size"]
    assert got["family_need"] >= single["bar"]["need"]
    assert fam["alpha"] == 0.05 / 4


def test_a_result_that_passes_alone_can_fail_in_the_family():
    """**혼자 재면 발견, 넷 중 하나면 아니다.** 이것이 다중비교다."""
    rows = (_rows([1] * 7) + _rows([-1] * 3)
            + _rows([-1] * 6, phase="Contraction") + _rows([1] * 2, phase="Contraction"))
    expected = {"Expansion": 1, "Contraction": -1}
    single = pf.verdict(rows, expected)
    assert single["n"] == 18 and single["hits"] == 13
    assert single["bar"]["p0"] == 0.5          # 부호가 9대 9 — 우연은 동전이다
    assert single["passed"] is True
    fam = pf.family_verdict({f"f{i}": single for i in range(4)})
    assert fam["results"]["f0"]["family_passed"] is False


def test_all_episodes_sharing_one_sign_cannot_be_judged():
    """**「늘 같은 쪽」이 100%면 무엇도 우연을 못 이긴다.**

    부호가 한쪽으로만 나오면 국면이 무엇을 더했는지 가릴 방법이 없다.
    p를 내지 않고 판정 불가라고 말한다.
    """
    rows = _rows([1] * 18)
    got = pf.verdict(rows, {"Expansion": 1})
    assert got["passed"] is None and got["p"] is None
    assert "판정 불가" in got["verdict"]
    assert "가릴 수 없습니다" in pf.format_verdict(got, {"Expansion": 1}, rows)


def test_the_family_report_says_the_list_was_fixed_in_advance():
    rows = _rows([1] * 10) + _rows([-1] * 8, phase="Contraction")
    single = pf.verdict(rows, {"Expansion": 1, "Contraction": -1})
    msg = pf.format_family(pf.family_verdict({"Size": single, "Value": single}))
    assert "재기 전에 고정" in msg
    assert "18.5%" in msg


def test_a_thin_factor_is_reported_not_silently_dropped():
    thin = pf.verdict(_rows([1] * 5), {"Expansion": 1})
    msg = pf.format_family(pf.family_verdict({"Value": thin}))
    assert "표본 부족" in msg


# ─── 검정 가족은 재기 전에 고정한다 ────────────────────

def test_the_family_is_declared_in_code_not_chosen_later():
    """**목록을 나중에 정하면 좋아 보이는 것만 남긴다.**"""
    assert len(pf.FAMILY) == 3
    keys = [s["key"] for s in pf.FAMILY]
    assert keys == ["Size(코스피)", "Size(KRX TMI)", "Value+LowVol"]


def test_every_pair_names_a_service_for_each_leg():
    """가치저변동성은 파생상품지수에, 코스피 200은 KOSPI 시리즈에 있다."""
    for spec in pf.FAMILY:
        for leg in ("long", "short"):
            service, name = spec[leg]
            assert service and name


def test_no_tr_index_is_paired_with_a_price_index():
    """**TR과 가격지수를 비교하면 배당만큼 가짜 초과수익이 생긴다.**"""
    for spec in pf.FAMILY:
        tr = ["TR" in spec[leg][1] for leg in ("long", "short")]
        assert tr[0] == tr[1], spec["key"]


def test_only_factors_the_bot_actually_bets_on_are_tested():
    """가설이 없는 팩터를 재면 방향을 내가 정하게 된다 — 사후 맞춤이다."""
    import quant_bot as qb
    for spec in pf.FAMILY:
        for factor in spec["factors"]:
            assert factor in qb.SCORING_FACTORS, factor


def test_two_factors_average_into_one_prediction():
    """가치저변동성은 한 지수가 Value와 LowVol을 겸한다."""
    weights = {"A": {"Value": 0.4, "LowVol": 0.0},
               "B": {"Value": 0.0, "LowVol": 0.0}}
    got = pf.expected_signs(weights, ("Value", "LowVol"))
    assert got == {"A": 1, "B": -1}


def test_a_single_factor_still_works_as_a_string():
    weights = {"A": {"Size": 0.2}, "B": {"Size": 0.0}}
    assert pf.expected_signs(weights, "Size") == {"A": 1, "B": -1}


def test_the_note_names_both_factors():
    weights = {"A": {"Value": 0.4, "LowVol": 0.2}}
    assert "Value+LowVol" in pf.hypothesis_note(weights, ("Value", "LowVol"))


def test_a_missing_index_is_reported_as_thin_not_as_a_result():
    """캐시에 없는 지수를 0으로 세면 없는 결과가 생긴다."""
    got, rows, expected = pf.run_one(
        pf.FAMILY[0], lambda service, name: {}, {}, {})
    assert got["verdict"] == "표본 부족" and got["missing"]


# ─── 팩터보다 앞선 질문 ───────────────────────────────

def test_the_market_hypothesis_comes_from_the_phase_names():
    """**내가 고른 방향이 아니다.** Recovery·Expansion은 정의상 오르는 국면이다."""
    got = pf.MARKET_TEST["expected"]
    assert got == {"Recovery": 1, "Expansion": 1,
                   "Slowdown": -1, "Contraction": -1}


def test_the_market_test_has_no_short_leg():
    """시장 그 자체를 보는 것이므로 반대 다리가 없다 — 대신 우연 기대가 높다."""
    assert pf.MARKET_TEST["short"] is None


def test_a_rising_market_makes_the_bar_high_not_fifty():
    """**시장은 대체로 오른다.** 「늘 상승」으로 찍은 성적을 넘어야 한다."""
    rows = [{"sign": 1}] * 12 + [{"sign": -1}] * 6
    line = pf.bar(rows)
    assert abs(line["p0"] - 12 / 18) < 1e-12
    assert line["baseline_sign"] == 1


def test_the_report_uses_the_words_of_the_question_being_asked():
    """시장 방향 검정에서 「늘 소형 우위」라고 적으면 읽는 사람이 오해한다."""
    rows = (_rows([-1] * 13, phase="Contraction") + _rows([1] * 5, phase="Slowdown"))
    expected = {"Contraction": -1, "Slowdown": -1}
    msg = pf.format_verdict(pf.verdict(rows, expected), expected, rows,
                            words=("상승", "하락"), subject="시장 방향")
    assert "소형" not in msg and "대형" not in msg
    assert "하락" in msg          # 이 표본의 예측은 둘 다 하락이다
    assert "시장 방향을 예측한다는 근거" in msg


# ─── 판정 원장 ───────────────────────────────────────

def _fam(verdict_word="우위 확인 불가"):
    single = {"n": 18, "hits": 11, "bar": {"p0": 0.556, "need": 15},
              "p": 0.41, "family_need": 15, "family_verdict": verdict_word}
    return {"k": 3, "alpha": 0.0167, "results": {"Size(코스피)": single}}


def test_the_record_keeps_what_was_measured():
    rec = pf.validation_record(_fam(), None, at="2026-09-02T10:00:00")
    assert rec["tests"]["Size(코스피)"]["hits"] == 11
    assert rec["any_passed"] is False and rec["passed"] == []


def test_a_passing_test_is_named():
    rec = pf.validation_record(_fam("우위 확인"), None, at="2026-09-02T10:00:00")
    assert rec["any_passed"] is True and rec["passed"] == ["Size(코스피)"]


def test_the_market_test_joins_the_record():
    market = {"n": 18, "hits": 13, "bar": {"p0": 0.667, "need": 16},
              "p": 0.412, "verdict": "우위 확인 불가"}
    rec = pf.validation_record(_fam(), market, at="2026-09-02T10:00:00")
    assert rec["tests"]["시장 방향"]["hits"] == 13


def test_no_record_means_unmeasured_not_verified():
    """**기록이 없는 것과 검증된 것은 다르다.**"""
    line = pf.validation_line(None)
    assert "미측정" in line and "검증" in line


def test_the_line_says_unverified_when_nothing_passed():
    line = pf.validation_line(pf.validation_record(_fam(), None, at="2026-09-02T10:00:00"))
    assert "미검증" in line and "11/18" in line


def test_the_ledger_only_appends(tmp_path):
    """**지난 판정을 고치지 않는다** — 임계값 원장과 같은 규칙.

    판정이 달라지면 새 줄이 붙고, 먼저 있던 줄은 그대로 남는다.
    """
    path = tmp_path / "v.json"
    pf.append_validation({"at": "1", "tests": {"A": {"n": 18, "hits": 11}}}, path)
    pf.append_validation({"at": "2", "tests": {"A": {"n": 18, "hits": 15}}}, path)
    got = pf.latest_validation(path)
    assert got["at"] == "2"
    import json
    log = json.loads(path.read_text(encoding="utf-8"))
    assert len(log) == 2 and log[0]["at"] == "1"


def test_a_broken_ledger_is_not_fatal_and_not_overwritten(tmp_path):
    """깨진 원장은 빈 것으로 읽되 **그 위에 쓰지 않는다**(이력 보호)."""
    path = tmp_path / "v.json"
    path.write_text("{망가진", encoding="utf-8")
    assert pf.latest_validation(path) is None
    assert pf.append_validation({"at": "1"}, path) == []
    assert path.read_text(encoding="utf-8") == "{망가진"


def test_the_same_finding_is_not_written_twice(tmp_path):
    """**같은 값이 반복되면 변화 지점이 묻힌다.**

    원장은 「무엇이 언제 바뀌었나」를 위한 것이다. 새 표본 없이 다시 돌린
    것은 새 판정이 아니다. (2026-09-02: 같은 결과가 2건 쌓여 발견)
    """
    path = tmp_path / "v.json"
    rec = pf.validation_record(_fam(), None, at="2026-09-02T10:00:00")
    later = pf.validation_record(_fam(), None, at="2026-09-02T18:00:00")
    pf.append_validation(rec, path)
    log = pf.append_validation(later, path)
    assert len(log) == 1
    assert pf.latest_validation(path)["at"] == "2026-09-02T10:00:00"


def test_a_changed_finding_is_appended(tmp_path):
    path = tmp_path / "v.json"
    pf.append_validation(pf.validation_record(_fam(), None, at="1"), path)
    log = pf.append_validation(
        pf.validation_record(_fam("우위 확인"), None, at="2"), path)
    assert len(log) == 2 and log[-1]["any_passed"] is True


def test_the_time_alone_does_not_make_a_new_finding():
    a = pf.validation_record(_fam(), None, at="2026-09-02T10:00:00")
    b = pf.validation_record(_fam(), None, at="2027-01-01T00:00:00")
    assert pf.same_finding(a, b) is True


def test_a_different_number_makes_a_new_finding():
    a = pf.validation_record(_fam(), None, at="1")
    fam = _fam()
    fam["results"]["Size(코스피)"] = {**fam["results"]["Size(코스피)"], "hits": 15}
    assert pf.same_finding(a, pf.validation_record(fam, None, at="2")) is False


def test_the_first_record_always_lands(tmp_path):
    path = tmp_path / "v.json"
    assert len(pf.append_validation(pf.validation_record(_fam(), None, at="1"), path)) == 1


# ─── 검정력 — α와 같다 (리뷰 2026-09-02) ───────────────

def _episodes(lengths, phase="Expansion"):
    return [{"phase": phase, "start": "s", "end": "e", "months": L,
             "mean": 0.01, "sign": 1} for L in lengths]


def test_power_is_near_alpha_for_a_realistic_effect():
    """**월 sd 6%p·18개 에피소드에서 0.5%p/월 우위를 알아볼 확률은 α 수준이다.**

    「우위 확인 불가」가 진위와 무관하게 거의 항상 나온다는 뜻이다.
    """
    rows = _episodes([8, 10, 17, 20, 4, 17, 2, 18, 6, 23, 3, 16, 6, 17, 16, 4, 3, 9])
    power = pf.sign_power(rows, {"Expansion": 1}, sd_month=0.0607, need=15)
    assert power is not None and power < 0.10


def test_a_large_effect_is_detectable():
    rows = _episodes([12] * 18)
    power = pf.sign_power(rows, {"Expansion": 1}, sd_month=0.0607, need=15,
                          effect=0.03)
    assert power > 0.6


def test_a_lower_bar_or_lower_noise_raises_power():
    rows = _episodes([12] * 18)
    base = pf.sign_power(rows, {"Expansion": 1}, sd_month=0.06, need=15)
    assert pf.sign_power(rows, {"Expansion": 1}, sd_month=0.06, need=13) > base
    assert pf.sign_power(rows, {"Expansion": 1}, sd_month=0.03, need=15) > base


def test_longer_episodes_raise_power():
    short = pf.sign_power(_episodes([2] * 18), {"Expansion": 1}, sd_month=0.06, need=15)
    long_ = pf.sign_power(_episodes([24] * 18), {"Expansion": 1}, sd_month=0.06, need=15)
    assert long_ > short


def test_power_needs_the_inputs():
    assert pf.sign_power([], {"Expansion": 1}, sd_month=0.06, need=15) is None
    assert pf.sign_power(_episodes([5]), {"Expansion": 1}, sd_month=0, need=15) is None
    assert pf.sign_power(_episodes([5]), {"Expansion": 1}, sd_month=0.06, need=None) is None


def test_the_report_says_it_cannot_measure_when_power_is_low():
    rows = _episodes([2] * 13) + [{"phase": "Contraction", "start": "s", "end": "e",
                                   "months": 2, "mean": -0.01, "sign": -1}] * 5
    expected = {"Expansion": 1, "Contraction": -1}
    msg = pf.format_verdict(pf.verdict(rows, expected), expected, rows, sd_month=0.06)
    assert "잴 수 없다" in msg and "검정력" in msg
