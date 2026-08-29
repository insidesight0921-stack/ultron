"""test_condition_review.py — 조건 심사 결과가 화면까지 가는지 (hermetic).

2026-08-29 백테스트에서 신고가돌파만 세 검사를 통과했다. 다만 **채택이 아니라
관찰**이다 — 무작위 대비 +0.32%p로 효과가 얇고, 기간은 14개월 한 국면이며,
부트스트랩 1위 유지율 41%는 과반이 아니다. 조건 이름만 화면에 보이면
"검증된 전략"으로 읽히므로, 근거의 얇음이 같이 따라가야 한다.
"""
from __future__ import annotations

from pathlib import Path

import entry_backtest as eb


# ─── 심사 상태 ───────────────────────────────────────


def test_only_one_condition_is_under_observation():
    """통과 기준을 느슨하게 잡으면 전부 '관찰'이 되어 구분이 사라진다."""
    watching = [k for k, v in eb.CONDITION_REVIEW.items() if v["status"] == "관찰"]
    assert watching == ["신고가돌파"]


def test_a_condition_worse_than_random_is_rejected():
    r = eb.CONDITION_REVIEW["과낙폭반등"]
    assert r["status"] == "기각" and r["actual"] < r["baseline"]


def test_conditions_that_flip_between_halves_are_held():
    """전반에 강하고 후반에 무너지면 조건이 아니라 국면을 탄 것이다."""
    for name in ("추세위+눌림", "정배열"):
        h1, h2 = eb.CONDITION_REVIEW[name]["halves"]
        assert h2 < h1 * 0.5
        assert eb.CONDITION_REVIEW[name]["status"] == "보류"


def test_the_observed_condition_is_consistent_across_halves():
    h1, h2 = eb.CONDITION_REVIEW["신고가돌파"]["halves"]
    assert h1 > 0 and h2 > 0 and h2 >= h1 * 0.9


def test_an_unreviewed_condition_says_so():
    assert eb.condition_status("듣도보도못한조건") == "미심사"


# ─── 표시 순서 ───────────────────────────────────────


def test_observation_sorts_before_hold_before_rejected():
    ranks = [eb.condition_rank(c)
             for c in ("신고가돌파", "추세위+눌림", "듣도보도못한조건", "과낙폭반등")]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4


def test_scan_puts_the_observed_condition_first():
    """화면에서 관찰 대상이 먼저 보여야 한다."""
    matched = sorted(["과낙폭반등", "신고가돌파", "정배열"],
                     key=lambda c: (eb.condition_rank(c), c))
    assert matched[0] == "신고가돌파" and matched[-1] == "과낙폭반등"


# ─── 근거 문구 ───────────────────────────────────────


def test_the_note_states_the_thin_edge():
    """+0.32%p라는 사실을 숨기면 '백분위 100'만 남아 강해 보인다."""
    note = eb.review_note("신고가돌파")
    assert "+0.32%p" in note and "백분위 100" in note and "1위유지 41%" in note


def test_the_note_flags_a_half_split_flip():
    assert "전후반 뒤집힘" in eb.review_note("추세위+눌림")


def test_the_observed_condition_is_not_flagged_as_flipped():
    assert "전후반 뒤집힘" not in eb.review_note("신고가돌파")


def test_a_rejected_condition_says_it_lost_to_random():
    assert "무작위보다 나쁨" in eb.review_note("과낙폭반등")


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


def test_the_screen_admits_the_edge_is_thin():
    ui = _ui()
    assert "효과는 얇습니다" in ui and "+0.32%p" in ui


def test_the_screen_states_how_much_evidence_would_be_needed():
    """'언제쯤 알 수 있나'를 안 적으면 관찰이 무기한 근거처럼 굳는다."""
    ui = _ui()
    assert "880건" in ui and "4.4년" in ui
