"""test_slot_allocation.py — 슬롯 비중 합계 검증 (hermetic, 순수 함수만).

2026-08-29 실측: 합계가 110%였다(콴텍 40 + 키움 40 + IPO 20 + 마이퀀트 10).
시드를 `비중 × 포트폴리오 시드`로 계산하므로 **넣지 않은 1,000만원 위에서**
수익률을 재고 있었다. 초과수익이 +2.09%p → −0.12%p로 뒤집힌다.
"""
from __future__ import annotations

import slot_allocation as sa


def _slots(*pairs):
    return [{"name": n, "allocation_pct": p} for n, p in pairs]


REAL = _slots(("콴텍", 0.4), ("키움", 0.4), ("IPO", 0.2), ("마이퀀트", 0.1))


# ─── 합계 ────────────────────────────────────────────


def test_the_real_case_is_caught():
    """이 케이스가 통과하면 검증이 있으나 마나다."""
    result = sa.check(REAL)
    assert result["ok"] is False
    assert result["total"] == 1.1
    assert result["gap"] == 0.1


def test_a_correct_total_passes():
    assert sa.check(_slots(("콴텍", 0.35), ("키움", 0.35),
                           ("IPO", 0.2), ("마이퀀트", 0.1)))["ok"]


def test_floating_point_noise_is_tolerated():
    """0.1+0.2 같은 합이 0.30000000000000004가 되는 것으로 경고를 띄우면 안 된다."""
    assert sa.check(_slots(("a", 0.1), ("b", 0.2), ("c", 0.3), ("d", 0.4)))["ok"]


def test_under_allocation_is_also_flagged():
    """미달도 문제다 — 쓰지 않는 자본이 분모에서 빠져 수익률이 부풀려진다."""
    r = sa.check(_slots(("콴텍", 0.4), ("키움", 0.4)))
    assert r["ok"] is False and r["gap"] < 0 and "미달" in r["detail"]


def test_no_slots_is_not_ok():
    assert sa.check([])["ok"] is False


def test_non_numeric_values_are_ignored_not_crashed():
    total = sa.total_allocation(
        [{"allocation_pct": None}, {"allocation_pct": "0.4"},
         {"allocation_pct": True}, {"allocation_pct": 0.4}])
    assert total == 0.4


# ─── 문구 ────────────────────────────────────────────


def test_the_message_says_what_breaks():
    """'합계가 110%'만 적으면 표시 오류로 읽힌다 — 무엇이 왜곡되는지 말해야 한다."""
    detail = sa.check(REAL)["detail"]
    assert "110.0%" in detail and "+10.0%p" in detail
    assert "수익률" in detail and "왜곡" in detail


def test_the_message_lists_the_slots():
    assert "마이퀀트" in sa.check(REAL)["detail"]


# ─── 추가 시점 차단 ──────────────────────────────────


def test_adding_a_slot_that_overflows_is_detected():
    """추가 시점에 막는 것이 가장 싸다 — 거래가 쌓인 뒤엔 소급 왜곡이 생긴다."""
    existing = _slots(("콴텍", 0.4), ("키움", 0.4), ("IPO", 0.2))
    assert sa.would_exceed(existing, 0.1) is True


def test_adding_a_slot_that_fits_is_allowed():
    existing = _slots(("콴텍", 0.4), ("키움", 0.4))
    assert sa.would_exceed(existing, 0.2) is False


def test_exactly_one_hundred_is_allowed():
    existing = _slots(("콴텍", 0.5))
    assert sa.would_exceed(existing, 0.5) is False


def test_a_nonsense_pct_does_not_block():
    """숫자가 아닌 값으로 슬롯 추가가 막히면 원인을 찾기 어렵다."""
    assert sa.would_exceed(_slots(("콴텍", 0.5)), None) is False


# ─── 시드 계산 ───────────────────────────────────────


def test_a_broken_total_yields_no_seed():
    """틀린 분모로 계산한 수익률을 내놓느니 '계산하지 않았다'가 낫다."""
    assert sa.seed_of(REAL[0], 100_000_000, REAL) is None


def test_a_valid_total_yields_the_seed():
    ok = _slots(("콴텍", 0.35), ("키움", 0.35), ("IPO", 0.2), ("마이퀀트", 0.1))
    assert sa.seed_of(ok[0], 100_000_000, ok) == 35_000_000


def test_skipping_validation_is_possible_for_callers_that_checked():
    assert sa.seed_of(REAL[0], 100_000_000) == 40_000_000


def test_a_missing_pct_yields_no_seed():
    assert sa.seed_of({"name": "x"}, 100_000_000) is None
