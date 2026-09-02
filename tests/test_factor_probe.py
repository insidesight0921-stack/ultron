"""test_factor_probe.py — 소급 깊이 측정.

여기서 지키는 것 두 가지:
1. **빈 목록과 이름 없음을 섞지 않는다** — API 구간 문제를 지수 문제로 읽으면
   "1990년대엔 소형주 지수가 없었다"는 거짓이 나온다.
2. **이분법의 가정을 되짚는다** — 단조성이 깨지면 경계는 뜻이 없다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import factor_probe as fp  # noqa: E402


# ─── 월 목록·기준일 ───────────────────────────────────

def test_months_between_is_inclusive_and_ordered():
    m = fp.months_between((2025, 11), (2026, 2))
    assert m == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


def test_a_single_month_range_is_one_month():
    assert fp.months_between((2020, 5), (2020, 5)) == [(2020, 5)]


def test_the_phase_history_span_is_406_months():
    """계획서에 적힌 수치와 어긋나면 둘 중 하나가 틀린 것이다."""
    assert len(fp.months_between((1992, 9), (2026, 6))) == 406


def test_probe_days_avoid_month_end():
    """말일은 연휴에 걸리기 쉽다 — 중순부터 본다."""
    assert fp.MONTH_PROBE_DAYS[0] == 15
    assert max(fp.MONTH_PROBE_DAYS) <= 20


def test_candidate_dates_are_zero_padded():
    assert fp.candidate_dates(2001, 3)[0] == "20010315"


# ─── 상태 판정 ───────────────────────────────────────

def test_an_empty_list_is_the_api_window_not_a_missing_index():
    """**섞으면 안 되는 두 가지.** 빈 목록은 지수의 부재가 아니다."""
    assert fp.name_state([], "코스피 소형주") == fp.EMPTY


def test_a_list_without_the_name_means_the_index_did_not_exist_yet():
    rows = [{"IDX_NM": "코스피 200"}, {"IDX_NM": "코스피 대형주"}]
    assert fp.name_state(rows, "코스피 소형주") == fp.ABSENT


def test_the_name_is_matched_exactly_not_partially():
    """부분일치 금지 — '변동성'이 든 지수가 6개였던 2026-09-01 사례."""
    rows = [{"IDX_NM": "코스피 소형주 TR"}]
    assert fp.name_state(rows, "코스피 소형주") == fp.ABSENT


def test_spacing_does_not_decide_a_match():
    rows = [{"IDX_NM": "코스피200 가치저변동성"}]
    assert fp.name_state(rows, "코스피 200 가치저변동성") == fp.PRESENT


def test_other_name_fields_are_read():
    assert fp.name_state([{"IDX_NAME": "KRX 소형 TMI"}], "KRX 소형 TMI") == fp.PRESENT


# ─── 이분법 ──────────────────────────────────────────

def test_bisect_finds_the_first_true():
    items = list(range(20))
    assert fp.bisect_first_true(items, lambda x: x >= 7) == 7


def test_bisect_returns_none_when_never_true():
    assert fp.bisect_first_true([1, 2, 3], lambda x: False) is None


def test_bisect_handles_true_from_the_start():
    assert fp.bisect_first_true([1, 2, 3], lambda x: True) == 0


def test_bisect_costs_a_logarithmic_number_of_calls():
    """406개월을 선형으로 훑으면 406회다. 이분법이면 9회면 된다."""
    calls = []
    items = list(range(406))

    def pred(x):
        calls.append(x)
        return x >= 300

    assert fp.bisect_first_true(items, pred) == 300
    assert len(calls) <= 9


# ─── 가정 되짚기 ──────────────────────────────────────

def test_a_monotone_series_has_no_violations():
    items = list(range(50))
    assert fp.monotonicity_violations(items, lambda x: x >= 10, 10) == []


def test_a_hole_after_the_boundary_is_reported():
    """등재됐다가 사라지는 지수라면 경계는 뜻이 없다."""
    items = list(range(50))
    holes = {25, 26, 27, 28, 29, 30, 35, 40, 45, 49}

    def pred(x):
        return x >= 10 and x not in holes

    assert fp.monotonicity_violations(items, pred, 10)


def test_an_early_appearance_is_reported():
    items = list(range(50))
    assert fp.monotonicity_violations(items, lambda x: x <= 2 or x >= 10, 10)


def test_sampling_can_miss_a_violation_between_samples():
    """**되짚기는 표본이다 — 전수가 아니다.**

    경계 앞 한 달만 어긋나 있으면 3점 표본은 그것을 지나칠 수 있다.
    이 한계를 테스트로 박아 둔다. 「위반 0건」은 「위반이 없다」가 아니라
    「본 자리에서는 없었다」는 뜻이다.
    """
    items = list(range(50))
    missed = fp.monotonicity_violations(items, lambda x: x == 3 or x >= 10, 10)
    assert missed == []
    assert 3 not in fp.sample_indices(0, 9, 3)


def test_samples_are_spread_not_adjacent():
    """경계 바로 옆만 보면 먼 곳의 위반을 놓친다."""
    got = fp.sample_indices(0, 100, 3)
    assert got[0] == 0 and got[-1] == 100 and len(got) == 3


def test_a_narrow_range_samples_what_exists():
    assert fp.sample_indices(5, 6, 3) == [5, 6]


def test_an_empty_range_samples_nothing():
    assert fp.sample_indices(5, 4) == []


# ─── 소급 깊이 ───────────────────────────────────────

def test_coverage_counts_months_inclusive():
    assert fp.coverage((2026, 1), (2026, 3)) == 3


def test_coverage_of_a_missing_index_is_zero():
    assert fp.coverage(None, (2026, 3)) == 0


# ─── 통합(가짜 fetch) ─────────────────────────────────

def _fake_service(birth: dict, *, api_from=(1995, 1), filler="코스피 200"):
    """birth: 이름 → 산출 개시 (year, month). api_from 이전은 목록 자체가 없다.

    **`filler`가 있어야 진짜를 흉내낸다.** 실제 KRX는 그 날짜를 제공하기만 하면
    수백 개 지수가 함께 온다 — 목록이 비는 것은 오직 제공 구간 밖일 때다.
    filler 없이 만들면 「아직 안 태어난 지수」와 「구간 밖」이 둘 다 빈 목록이
    되어, 구간 경계를 가르려는 검사 자체가 무의미해진다.
    """
    def fetch_month(bas_dd: str) -> list:
        y, m = int(bas_dd[:4]), int(bas_dd[4:6])
        if (y, m) < api_from:
            return []
        rows = [{"IDX_NM": filler}] if filler else []
        return rows + [{"IDX_NM": n} for n, b in birth.items() if (y, m) >= b]
    return fetch_month


def test_the_birth_month_is_found():
    fetch = _fake_service({"코스피 소형주": (2001, 6)})
    got = fp.earliest_month(fetch, "코스피 소형주", fp.months_between((1995, 1), (2026, 8)))
    assert got["first"] == (2001, 6)
    assert got["months"] == len(fp.months_between((2001, 6), (2026, 8)))
    assert got["violations"] == []


def test_a_recent_index_shows_a_short_history():
    """밸류업처럼 최근 산출 개시면 표본이 짧다 — 그 사실이 판정을 바꾼다."""
    fetch = _fake_service({"코리아 밸류업 지수": (2024, 9)})
    got = fp.earliest_month(fetch, "코리아 밸류업 지수", fp.months_between((1995, 1), (2026, 8)))
    assert got["first"] == (2024, 9)
    assert got["months"] < 30


def test_an_api_window_is_not_reported_as_a_missing_index():
    """**이 테스트가 이 파일의 이유다.** 목록이 오지 않는 구간을 '지수 없음'이라 하면 오답이다."""
    def fetch_month(bas_dd: str) -> list:
        return []
    got = fp.earliest_month(fetch_month, "무엇이든", fp.months_between((2020, 1), (2020, 6)))
    assert got["first"] is None
    assert got["api_window"] is False
    msg = fp.format_availability({"무엇이든": got})
    assert "API가 그 시기를 주지 않습니다" in msg


def test_an_index_absent_everywhere_is_distinguished_from_an_api_window():
    fetch = _fake_service({"다른 지수": (1995, 1)})
    got = fp.earliest_month(fetch, "없는 지수", fp.months_between((1995, 1), (2000, 1)))
    assert got["first"] is None and got["api_window"] is True
    assert "조회 구간 어디에도 없습니다" in fp.format_availability({"없는 지수": got})


def test_probing_several_names_shares_one_month_cache():
    """이름마다 캐시를 따로 두면 같은 달을 두 번 부른다 — 조회는 네트워크다."""
    inner = _fake_service({"A": (2000, 1), "B": (2010, 1)})
    months = fp.months_between((1995, 1), (2026, 8))

    def counting():
        seen = []

        def fetch_month(bas_dd):
            seen.append(bas_dd[:6])
            return inner(bas_dd)
        return seen, fetch_month

    shared_seen, shared_fetch = counting()
    fp.probe_names(shared_fetch, ["A", "B"], months)

    apart = 0
    for name in ("A", "B"):
        seen, fetch_month = counting()
        fp.probe_names(fetch_month, [name], months)   # 한 번에 하나씩 재면
        apart += len(set(seen))

    assert len(set(shared_seen)) < apart, "캐시를 공유해도 조회가 줄지 않는다"
    # 응답이 온 달은 하루만 부르고 끝난다(휴장일 재시도가 남아 돌지 않는지).
    assert any(shared_seen.count(m) == 1 for m in set(shared_seen))


def test_a_holiday_month_falls_through_to_another_day():
    """15일이 휴장이면 그 달을 '없음'으로 세면 안 된다."""
    def fetch_month(bas_dd):
        if bas_dd.endswith("15"):
            return []
        return [{"IDX_NM": "코스피 소형주"}]
    rows = fp.month_rows(fetch_month, 2020, 1, {})
    assert fp.name_state(rows, "코스피 소형주") == fp.PRESENT


def test_the_report_compares_against_the_phase_history():
    got = {"코스피 소형주": {"first": (2001, 6), "months": 203, "violations": [],
                          "api_window": True, "calls": 9}}
    msg = fp.format_availability(got, phase_months=406)
    assert "203개월" in msg and "50%" in msg


def test_the_report_warns_when_the_assumption_broke():
    got = {"X": {"first": (2001, 6), "months": 10,
                 "violations": [("뒤인데 없음", (2010, 3))], "api_window": True}}
    assert "단조성 위반" in fp.format_availability(got)


def test_the_report_says_it_does_not_choose():
    got = {"X": {"first": (2001, 6), "months": 10, "violations": [], "api_window": True}}
    msg = fp.format_availability(got)
    assert "사람이 정합니다" in msg
    assert "두 다리 모두" in msg


# ─── API 구간 경계와 산출 개시를 가르기 ────────────────

def test_the_api_window_start_is_found():
    fetch = _fake_service({"X": (1995, 1)}, api_from=(2010, 1))
    months = fp.months_between((1992, 9), (2026, 8))
    assert fp.api_first_month(fetch, months) == (2010, 1)


def test_an_index_older_than_the_api_is_flagged_not_reported_as_new():
    """**이 테스트가 오늘 막은 구멍이다.**

    1995년생 지수인데 API가 2010년부터 준다면 첫 등장은 2010-01이다.
    그것을 「2010년 개시」로 적으면 거짓이 된다 — 구분 불가라고 말해야 한다.
    """
    fetch = _fake_service({"코스피 대형주": (1995, 1)}, api_from=(2010, 1))
    got = fp.probe_names(fetch, ["코스피 대형주"], fp.months_between((1992, 9), (2026, 8)))
    r = got["코스피 대형주"]
    assert r["first"] == (2010, 1)
    assert r["at_api_edge"] is True
    msg = fp.format_availability(got)
    assert "구분할 수 없습니다" in msg
    assert "2010-01" in msg


def test_an_index_born_after_the_api_window_is_not_flagged():
    """API가 2010년부터인데 지수가 2024년생이면 그 개시는 진짜다."""
    fetch = _fake_service({"코리아 밸류업 지수": (2024, 9)}, api_from=(2010, 1))
    got = fp.probe_names(fetch, ["코리아 밸류업 지수"], fp.months_between((1992, 9), (2026, 8)))
    r = got["코리아 밸류업 지수"]
    assert r["first"] == (2024, 9) and r["at_api_edge"] is False
    assert "구분할 수 없습니다" not in fp.format_availability(got)


def test_the_api_window_is_probed_once_for_every_name():
    """이름마다 구간을 다시 잡으면 조회가 배로 든다."""
    seen = []
    inner = _fake_service({"A": (2015, 1), "B": (2018, 1)}, api_from=(2010, 1))

    def fetch_month(bas_dd):
        seen.append(bas_dd[:6])
        return inner(bas_dd)

    got = fp.probe_names(fetch_month, ["A", "B"], fp.months_between((1992, 9), (2026, 8)))
    assert got["A"]["api_first"] == got["B"]["api_first"] == (2010, 1)
    assert len(set(seen)) <= 30
