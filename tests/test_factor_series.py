"""test_factor_series.py — 스프레드 원자료.

이 파일이 지키는 것:
- **덮어쓰지 않는다**(2026-09-01 VKOSPI 407일 → 780바이트 사고)
- **끊긴 구간을 이어 붙이지 않는다**(3개월 수익률을 1개월로 적는 일)
- **에피소드가 독립 단위다**(달로 세면 표본이 4배로 부푼다)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import factor_series as fs  # noqa: E402


# ─── 종가 읽기 ───────────────────────────────────────

def test_the_close_is_read_by_exact_name():
    rows = [{"IDX_NM": "코스피 대형주", "CLSPRC_IDX": "2,345.67"}]
    assert fs.close_of(rows, "코스피 대형주") == 2345.67


def test_a_partial_name_does_not_match():
    rows = [{"IDX_NM": "코스피 대형주 TR", "CLSPRC_IDX": "100"}]
    assert fs.close_of(rows, "코스피 대형주") is None


def test_spacing_is_ignored_but_letters_are_not():
    rows = [{"IDX_NM": "코스피200 중소형주", "CLSPRC_IDX": "100"}]
    assert fs.close_of(rows, "코스피 200 중소형주") == 100.0


def test_a_zero_or_unreadable_close_is_not_a_price():
    assert fs.close_of([{"IDX_NM": "X", "CLSPRC_IDX": "0"}], "X") is None
    assert fs.close_of([{"IDX_NM": "X", "CLSPRC_IDX": "-"}], "X") is None


def test_month_end_candidates_start_at_the_last_day():
    assert fs.month_end_candidates(2010, 2)[0] == "20100231"


# ─── 병합 ────────────────────────────────────────────

def test_merging_never_drops_existing_months():
    """**2026-09-01 사고의 재발 방지.** 새 수집이 옛 이력을 지우면 안 된다."""
    old = {"2010-01": 100.0, "2010-02": 101.0}
    assert fs.merge_series(old, {"2010-03": 102.0}) == {
        "2010-01": 100.0, "2010-02": 101.0, "2010-03": 102.0}


def test_a_newer_value_wins_for_the_same_month():
    assert fs.merge_series({"2010-01": 100.0}, {"2010-01": 111.0})["2010-01"] == 111.0


def test_a_none_does_not_erase_a_known_value():
    assert fs.merge_series({"2010-01": 100.0}, {"2010-01": None})["2010-01"] == 100.0


def test_saving_merges_with_what_is_on_disk(tmp_path):
    p = tmp_path / "c.json"
    fs.save_cache(p, {"A": {"2010-01": 1.0}})
    fs.save_cache(p, {"B": {"2010-01": 2.0}})
    fs.save_cache(p, {"A": {"2010-02": 3.0}})
    got = json.loads(p.read_text(encoding="utf-8"))
    assert got["A"] == {"2010-01": 1.0, "2010-02": 3.0}
    assert got["B"] == {"2010-01": 2.0}


def test_a_broken_cache_file_is_not_fatal(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{망가진", encoding="utf-8")
    assert fs.load_cache(p) == {}


# ─── 수익률 ──────────────────────────────────────────

def test_returns_use_the_previous_month():
    got = fs.monthly_returns({"2010-01": 100.0, "2010-02": 110.0})
    assert abs(got["2010-02"] - 0.10) < 1e-12
    assert "2010-01" not in got


def test_a_gap_does_not_become_a_one_month_return():
    """**끊긴 구간을 이으면 3개월 수익률이 1개월로 적힌다.**"""
    got = fs.monthly_returns({"2010-03": 100.0, "2010-06": 130.0})
    assert got == {}


def test_january_looks_back_to_december():
    got = fs.monthly_returns({"2009-12": 100.0, "2010-01": 105.0})
    assert abs(got["2010-01"] - 0.05) < 1e-12


# ─── 스프레드 ────────────────────────────────────────

def test_the_spread_needs_both_legs():
    long_r = {"2010-02": 0.05, "2010-03": 0.01}
    short_r = {"2010-02": 0.02}
    got = fs.spread(long_r, short_r)
    assert list(got) == ["2010-02"] and abs(got["2010-02"] - 0.03) < 1e-12


def test_the_spread_removes_the_market_move():
    """두 다리가 같이 10% 오르면 스프레드는 0이다 — 시장 상승 편향이 빠진다."""
    assert fs.spread({"2010-02": 0.10}, {"2010-02": 0.10})["2010-02"] == 0.0


# ─── 에피소드·검출력 ──────────────────────────────────

def test_block_means_average_within_an_episode():
    values = {"2010-01": 0.02, "2010-02": 0.04, "2011-05": 0.10}
    got = fs.block_means(values, [("2010-01", "2010-12"), ("2011-01", "2011-12")])
    assert got == [0.03, 0.10]


def test_an_empty_block_is_dropped_not_counted_as_zero():
    assert fs.block_means({"2010-01": 0.02}, [("2015-01", "2015-12")]) == []


def test_block_means_shrink_the_sample_on_purpose():
    """17개월 한 국면은 **한 사건**이다. 달로 세면 표본이 부푼다."""
    months = {f"2010-{m:02d}": 0.01 for m in range(1, 13)}
    assert len(fs.block_means(months, [("2010-01", "2010-12")])) == 1
    assert len(months) == 12


def test_the_mde_grows_as_the_sample_shrinks():
    small = fs.mde(0.01, 4, 14)
    big = fs.mde(0.01, 40, 140)
    assert small > big > 0


def test_the_mde_needs_a_spread_of_its_own():
    assert fs.mde(None, 5, 13) is None
    assert fs.mde(0.0, 5, 13) is None


def test_stdev_needs_two_points():
    assert fs.stdev([0.1]) is None
    assert abs(fs.stdev([0.0, 2.0]) - (2.0 ** 0.5)) < 1e-9


def test_the_report_says_it_has_not_looked_at_the_phases_yet():
    """**판정선을 정하기 전에 국면별 평균을 보면 사후 맞춤이 된다.**"""
    msg = fs.format_readiness(0.03, 0.01, {"Expansion": 5, "Slowdown": 4})
    assert "아직 보지 않았습니다" in msg
    assert "%p/월" in msg


def test_the_report_names_the_block_sd_as_the_one_that_counts():
    msg = fs.format_readiness(0.03, 0.009, {"Expansion": 5})
    assert "검정이 쓰는 값" in msg


# ─── 수집(가짜 fetch) ─────────────────────────────────

def _fake_market(prices: dict, *, trading_day=28):
    """prices: 'YYYY-MM' → {이름: 종가}. 그 달의 trading_day에만 응답한다."""
    def fetch_day(bas_dd: str) -> list:
        if int(bas_dd[6:]) != trading_day:
            return []
        key = f"{bas_dd[:4]}-{bas_dd[4:6]}"
        got = prices.get(key)
        if not got:
            return []
        return [{"IDX_NM": n, "CLSPRC_IDX": str(v)} for n, v in got.items()]
    return fetch_day


def test_collect_walks_back_to_the_last_trading_day():
    fetch = _fake_market({"2010-01": {"A": 100.0}})
    got = fs.collect(fetch, ["A"], [(2010, 1)])
    assert got["A"] == {"2010-01": 100.0}


def test_collect_skips_months_it_already_has():
    """**이어 받기** — 200개월을 받다 끊겼을 때 처음부터 다시 받지 않는다."""
    calls = []
    inner = _fake_market({"2010-01": {"A": 100.0}, "2010-02": {"A": 101.0}})

    def fetch(bas):
        calls.append(bas)
        return inner(bas)

    fs.collect(fetch, ["A"], [(2010, 1), (2010, 2)], have={"A": {"2010-01": 100.0}})
    assert all(b.startswith("201002") for b in calls)


def test_collect_takes_both_legs_from_one_call():
    """한 번의 조회에 51개 지수가 온다 — 다리마다 따로 부르면 두 배다."""
    calls = []
    inner = _fake_market({"2010-01": {"A": 100.0, "B": 50.0}})

    def fetch(bas):
        calls.append(bas)
        return inner(bas)

    got = fs.collect(fetch, ["A", "B"], [(2010, 1)])
    assert got["A"]["2010-01"] == 100.0 and got["B"]["2010-01"] == 50.0
    assert len(calls) <= 4


def test_a_missing_leg_leaves_the_month_out_rather_than_guessing():
    fetch = _fake_market({"2010-01": {"A": 100.0}})
    got = fs.collect(fetch, ["A", "B"], [(2010, 1)])
    assert "2010-01" not in got["B"]


def test_collect_saves_as_it_goes(tmp_path):
    """중간 저장이 없으면 실패가 곧 포기가 된다."""
    p = tmp_path / "c.json"
    prices = {f"2010-{m:02d}": {"A": 100.0 + m} for m in range(1, 13)}
    fs.collect(_fake_market(prices), ["A"], [(2010, m) for m in range(1, 13)],
               path=p, save_every=3)
    assert len(json.loads(p.read_text(encoding="utf-8"))["A"]) == 12


# ─── 오염 탐지 (2026-09-02) ───────────────────────────
#
# 200개월을 받아 스프레드까지 만들었는데 전부 못 쓰는 것이었다.
# 「코스피 대형주」레벨이 2025-06 3,068에서 2026-06 9,421로 갔다 —
# 대형주 가격지수로는 불가능한 값이다. 그런데 스프레드의 겉모습
# (누적 −89.9%p · 월 sd 6.07%p)은 조금도 이상해 보이지 않았다.
# **유일한 낌새는 소형-대형 상관 +0.52였다**(정상이면 0.8대).


def test_two_rows_with_the_same_name_yield_no_value():
    """첫 행을 집으면 달마다 다른 지수를 집을 수 있다 — 값을 내지 않는다."""
    rows = [{"IDX_NM": "코스피 대형주", "CLSPRC_IDX": "2500"},
            {"IDX_NM": "코스피 대형주", "CLSPRC_IDX": "9400"}]
    assert fs.close_of(rows, "코스피 대형주") is None


def test_a_single_match_still_works():
    rows = [{"IDX_NM": "코스피 대형주", "CLSPRC_IDX": "2500"},
            {"IDX_NM": "코스피 소형주", "CLSPRC_IDX": "2400"}]
    assert fs.close_of(rows, "코스피 대형주") == 2500.0


def test_ambiguity_shows_what_got_mixed():
    rows = [{"IDX_NM": "코스피 대형주", "IDX_IND_CD": "1", "CLSPRC_IDX": "2500"},
            {"IDX_NM": "코스피 대형주", "IDX_IND_CD": "9", "CLSPRC_IDX": "9400"}]
    got = fs.ambiguity(rows, "코스피 대형주")
    assert len(got) == 2
    assert {i["close"] for i in got} == {2500.0, 9400.0}
    assert got[0]["id"]["IDX_IND_CD"] == "1"


def test_collect_refuses_an_ambiguous_month_instead_of_guessing():
    def fetch(bas_dd):
        if not bas_dd.endswith("28"):
            return []
        return [{"IDX_NM": "A", "CLSPRC_IDX": "100"},
                {"IDX_NM": "A", "CLSPRC_IDX": "900"}]
    got = fs.collect(fetch, ["A"], [(2026, 5)])
    assert got["A"] == {}
    assert got["__ambiguous__"]["A"] == ["2026-05"]


def test_a_real_market_regime_is_not_called_contamination():
    """**2026-09-02에 내가 저지른 반대 방향의 실패.**

    소형-대형 상관 +0.52와 월 33% 급변을 자동 실격 사유로 삼아 멀쩡한
    200개월을 「전부 못 쓴다」고 판정했다. 실제로는 KOSPI 자체가 1년 만에
    3,071 → 8,476으로 간 대형주 주도 장세였고, 코스피 대형주와 KOSPI의
    상관은 +0.996이었다. **임계값은 진짜와 가짜를 가르지 못한다.**
    """
    kospi = {"2026-04": 6598.87, "2026-05": 8476.15, "2026-06": 8476.48}
    large = {"2026-04": 7022.94, "2026-05": 9341.25, "2026-06": 9421.19}
    small = {"2026-04": 3022.74, "2026-05": 2585.47, "2026-06": 2299.17}
    got = audit_pair(large, small, kospi)
    assert got["ok"], got["blocking"]
    assert "판정하지 않습니다" in fs.format_audit(got)


def audit_pair(large, small, benchmark):
    return fs.audit({"대형": large, "소형": small}, benchmark=benchmark)


def test_the_benchmark_correlation_is_reported_for_each_leg():
    kospi = {f"2010-{m:02d}": 100 + 3 * m for m in range(1, 13)}
    tracks = {f"2010-{m:02d}": 200 + 6 * m for m in range(1, 13)}
    got = fs.audit({"추종": tracks}, benchmark=kospi)
    assert got["benchmark"]["추종"]["corr"] is not None
    assert "벤치마크 상관" in fs.format_audit(got)


def test_a_big_divergence_is_shown_not_judged():
    """초과 −43%p여도 실격이 아니다 — 2026-05 소형주가 실제로 그랬다."""
    kospi = {"2026-04": 6598.87, "2026-05": 8476.15}
    small = {"2026-04": 3022.74, "2026-05": 2585.47}
    got = fs.audit({"소형": small}, benchmark=kospi)
    assert got["ok"]
    month, leg, bench, diff = got["benchmark"]["소형"]["excess"][0]
    assert month == "2026-05" and diff < -0.40


def test_an_impossible_value_still_blocks():
    """**있을 수 없는 값**은 여전히 막는다 — 이상해 보이는 것과 다르다."""
    got = fs.audit({"X": {"2010-01": 100.0, "2010-02": -5.0}})
    assert not got["ok"]
    assert "0 이하" in fs.format_audit(got)


def test_a_non_number_blocks():
    assert not fs.audit({"X": {"2010-01": "없음"}})["ok"]


def test_audit_works_without_a_benchmark():
    got = fs.audit({"X": {"2010-01": 100.0, "2010-02": 101.0}})
    assert got["ok"] and got["benchmark"] == {}


def test_pearson_needs_variation():
    assert fs.pearson([1.0, 1.0], [1.0, 2.0]) is None
    assert abs(fs.pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) - 1.0) < 1e-9


def test_quarantine_moves_rather_than_deletes(tmp_path):
    """**지우면 무엇이 잘못됐었는지 다시 못 본다.**

    그렇다고 남겨두면 다음 수집이 「이미 있는 달」로 건너뛰어 오염이
    살아남는다. 그래서 옮긴다.
    """
    p = tmp_path / "c.json"
    fs.save_cache(p, {"대형": {"2026-05": 9341.0}, "소형": {"2026-05": 2585.0}})
    got = fs.quarantine(p, "대형", note="중복 의심")
    assert "대형" not in got and "소형" in got
    box = got["__quarantine__"]
    assert len(box) == 1
    key = next(iter(box))
    assert key.startswith("대형@") and box[key]["series"]["2026-05"] == 9341.0
    assert box[key]["note"] == "중복 의심"


def test_quarantine_is_a_no_op_for_an_unknown_name(tmp_path):
    p = tmp_path / "c.json"
    fs.save_cache(p, {"소형": {"2026-05": 2585.0}})
    assert "__quarantine__" not in fs.quarantine(p, "없는이름")


def test_a_collected_month_is_refetched_after_quarantine(tmp_path):
    """격리의 목적 — 다음 수집이 그 달을 **다시 받는다**."""
    p = tmp_path / "c.json"
    fs.save_cache(p, {"대형": {"2026-05": 9341.0}})
    fs.quarantine(p, "대형")
    calls = []

    def fetch(bas_dd):
        calls.append(bas_dd)
        return [{"IDX_NM": "대형", "CLSPRC_IDX": "2500"}] if bas_dd.endswith("29") else []

    got = fs.collect(fetch, ["대형"], [(2026, 5)], have=fs.load_cache(p))
    assert calls and got["대형"]["2026-05"] == 2500.0
