"""test_condition_review.py — 조건 심사 결과가 화면까지 가는지 (hermetic).

2026-08-31 재측정: **전·후반을 모두 통과한 조건이 없다.** 관찰 등급은 비어 있다.

직전 표(08-29)는 전후반 원수익을 비교해 신고가돌파를 "안정적"으로 봤는데,
같은 구간에서 무작위 기준선도 +2.87%→+0.67%로 떨어졌다. **시장이 내려간 것을
조건의 안정성으로 오독**한 것이다. 구간별 기준선 대비로 바꾸면 신고가돌파는
전반 −0.46%p(백분위 0) / 후반 +0.69%p(백분위 100)로 부호가 뒤집힌다.

조건 이름만 화면에 보이면 "검증된 전략"으로 읽히므로, 근거의 얇음이 같이 따라가야 한다.
"""
from __future__ import annotations

from pathlib import Path

import entry_backtest as eb


# ─── 심사 상태 ───────────────────────────────────────


def test_nothing_is_under_observation_right_now():
    """빈 등급을 그대로 두는 것이 요점이다.

    한 구간만 통과한 조건을 '관찰'로 올리면 등급이 근거처럼 굳는다. 전·후반을
    모두 통과한 조건이 생기면 그때 채운다.
    """
    watching = [k for k, v in eb.CONDITION_REVIEW.items() if v["status"] == "관찰"]
    assert watching == []


def test_a_condition_that_passes_only_one_half_is_held_not_observed():
    """신고가돌파는 전체 기간 백분위 100이지만 전반이 0이다."""
    r = eb.CONDITION_REVIEW["신고가돌파"]
    assert r["percentile"] == 100
    assert r["half_pct"][0] < 95 and r["half_pct"][1] >= 95
    assert eb.half_split_holds(r) is False
    assert r["status"] == "보류"


def test_the_half_split_gate_requires_both_sides():
    ok = {"halves": (0.01, 0.01), "half_pct": (99, 99)}
    assert eb.half_split_holds(ok) is True
    for bad in ({"halves": (-0.01, 0.01), "half_pct": (99, 99)},
                {"halves": (0.01, 0.01), "half_pct": (94, 99)},
                {"halves": (0.01, 0.01), "half_pct": (99, 94)}):
        assert eb.half_split_holds(bad) is False


def test_halves_are_excess_over_a_period_matched_baseline():
    """원수익을 그대로 담으면 시장 드리프트를 조건의 성적으로 읽는다.

    무작위 기준선이 전반 +2.87% → 후반 +0.67%로 떨어진 구간이라, 원수익끼리
    비교하면 어떤 조건이든 '후반에 약해졌다'로 보인다. 초과수익은 그렇지 않다 —
    부호가 양쪽으로 갈린다.
    """
    halves = [v["halves"] for v in eb.CONDITION_REVIEW.values()]
    assert any(h1 < 0 for h1, _ in halves) and any(h1 > 0 for h1, _ in halves)
    assert all(abs(h) < 0.05 for pair in halves for h in pair)


def test_a_condition_worse_than_random_is_rejected():
    r = eb.CONDITION_REVIEW["과낙폭반등"]
    assert r["status"] == "기각" and r["actual"] < r["baseline"]


def test_a_condition_inside_the_chance_range_is_rejected():
    """백분위 94는 100에 가까워 보이지만 95 미만이면 우연 범위다."""
    for name in ("정배열", "추세위+눌림"):
        r = eb.CONDITION_REVIEW[name]
        assert r["percentile"] < 95 and r["status"] == "기각"


def test_no_condition_passes_both_halves():
    assert not [k for k, v in eb.CONDITION_REVIEW.items() if eb.half_split_holds(v)]


def test_an_unreviewed_condition_says_so():
    assert eb.condition_status("듣도보도못한조건") == "미심사"


# ─── 표시 순서 ───────────────────────────────────────


def test_hold_sorts_before_unreviewed_before_rejected():
    ranks = [eb.condition_rank(c)
             for c in ("신고가돌파", "듣도보도못한조건", "과낙폭반등")]
    assert ranks == sorted(ranks) and len(set(ranks)) == 3


def test_scan_puts_the_best_surviving_condition_first():
    """화면에서 기각된 조건이 앞에 오면 안 된다."""
    matched = sorted(["과낙폭반등", "신고가돌파", "정배열"],
                     key=lambda c: (eb.condition_rank(c), c))
    assert matched[0] == "신고가돌파" and eb.condition_status(matched[-1]) == "기각"


# ─── 근거 문구 ───────────────────────────────────────


def test_the_note_states_the_thin_edge():
    """효과 크기를 숨기면 '백분위 100'만 남아 강해 보인다."""
    note = eb.review_note("신고가돌파")
    assert "+0.54%p" in note and "백분위 100" in note and "1위유지 59%" in note


def test_the_note_flags_the_half_split_flip_with_numbers():
    """'뒤집힘'만 적으면 얼마나 뒤집혔는지 알 수 없다."""
    note = eb.review_note("신고가돌파")
    assert "전후반 뒤집힘" in note and "0/100" in note


def test_a_rejected_condition_says_it_lost_to_random():
    assert "무작위보다 나쁨" in eb.review_note("과낙폭반등")


def test_a_condition_rejected_for_being_ordinary_does_not_claim_it_lost():
    """정배열은 무작위보다 나쁘지 않다 — 구분되지 않을 뿐이다."""
    note = eb.review_note("정배열")
    assert "우연 범위" in note and "무작위보다 나쁨" not in note


# ─── 화면 배선 ───────────────────────────────────────


def _ui():
    return (Path(__file__).resolve().parents[1] / "scripts"
            / "paper_ui.py").read_text(encoding="utf-8")


def test_the_scan_result_carries_the_review():
    src = (Path(__file__).resolve().parents[1] / "scripts"
           / "entry_backtest.py").read_text(encoding="utf-8")
    body = src[src.index("def scan_signals("):]
    body = body[:body.index("\ndef ", 10)]
    assert '"review":' in body and '"best_status":' in body


def test_the_screen_shows_the_status_per_condition():
    assert "it.review" in _ui()


def test_the_screen_says_it_is_observation_not_adoption():
    ui = _ui()
    assert "채택이 아니라 관찰" in ui


def test_the_screen_says_the_observation_grade_is_empty():
    """'관찰 등급이 있다'로 읽히면 비어 있다는 사실이 사라진다."""
    ui = _ui()
    assert "관찰 등급도 비어 있습니다" in ui
    assert "전·후반을 모두 통과한 조건이" in ui


def test_the_screen_admits_the_previous_table_was_misread():
    """틀린 표를 조용히 갈아치우면 같은 오독이 다시 나온다."""
    ui = _ui()
    assert "오독" in ui and "+2.87%" in ui


def test_the_screen_states_how_much_evidence_would_be_needed():
    """'언제쯤 알 수 있나'를 안 적으면 보류가 무기한 근거처럼 굳는다."""
    ui = _ui()
    assert "809건" in ui and "3.7년" in ui
