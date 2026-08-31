"""test_equity_drift.py — 목표 비중 괴리 판정 (hermetic, 순수 함수만).

v3.59에서 목표 주식 비중을 매수 예산에 연결했지만 그건 **리밸런싱 시점에만**
적용된다. 그 사이 시세가 움직이거나 자동청산이 나가면 비중이 벗어나고 아무도
모른다. 이 모듈은 그 상태에 이름을 붙이되 **스스로 매매하지 않는다.**
"""
from __future__ import annotations

import equity_drift as ed


def _at(ratio: float, total: float = 100.0, target: float = 0.70, **kw):
    held = total * ratio
    return ed.assess(total - held, held, target, **kw)


# ─── 판정 ────────────────────────────────────────────


def test_on_target_is_normal():
    assert _at(0.70)["state"] == ed.NORMAL


def test_a_small_drift_is_still_normal():
    """밴드가 없으면 시세가 조금만 움직여도 상태가 바뀌어 알림이 소음이 된다."""
    assert _at(0.73)["state"] == ed.NORMAL
    assert _at(0.67)["state"] == ed.NORMAL


def test_a_big_overweight_is_flagged():
    r = _at(0.85)
    assert r["state"] == ed.OVER and r["gap"] > 0


def test_a_big_underweight_is_flagged():
    r = _at(0.40)
    assert r["state"] == ed.UNDER and r["gap"] < 0


def test_the_real_kium_case_is_underweight():
    """2026-08-31 실측: 현금 14,017,018 · 보유 22,262,730 → 61.4%."""
    r = ed.assess(14_017_018, 22_262_730, 0.70)
    assert r["state"] == ed.UNDER
    assert abs(r["current"] - 0.6136) < 0.001
    assert abs(r["need"] - 3_133_093) < 1_000


def test_the_real_quant_case_is_underweight():
    """콴텍 11.5% — 7월 리밸런싱 누락으로 현금만 남은 상태."""
    r = ed.assess(34_276_048, 4_460_500, 0.70)
    assert r["state"] == ed.UNDER and r["need"] > 22_000_000


def test_a_lower_target_can_turn_normal_into_over():
    """코스피가 200일선 아래로 가면 목표가 50%로 내려간다."""
    assert _at(0.70, target=0.70)["state"] == ed.NORMAL
    assert _at(0.70, target=0.50)["state"] == ed.OVER


def test_an_empty_slot_is_not_judged():
    r = ed.assess(0, 0, 0.70)
    assert r["current"] is None and r["state"] == ed.NORMAL


def test_negative_inputs_do_not_produce_nonsense():
    r = ed.assess(-100, -50, 0.70)
    assert r["current"] is None


# ─── 무엇을 하지 않는가 ──────────────────────────────


def test_being_over_blocks_new_buys():
    assert ed.blocks_new_buys(_at(0.85)) is True


def test_being_under_does_not_block_buys():
    assert ed.blocks_new_buys(_at(0.40)) is False


def test_nothing_here_ever_says_to_sell():
    """강제 매도는 되돌릴 수 없다 — 사람 판단에 남긴다(일일 손실 한도와 같은 선)."""
    r = _at(0.95)
    assert "매도" not in r["action"]
    assert "강제 청산하지 않습니다" in r["action"]


def test_the_report_states_that_it_will_not_liquidate():
    text = ed.format_report({"키움": _at(0.95)}, 0.70)
    assert "강제 매도는 하지 않습니다" in text


# ─── 알림 중복 ───────────────────────────────────────


def test_the_same_state_yields_the_same_key():
    a = {"키움": _at(0.61), "콴텍": _at(0.11)}
    b = {"키움": _at(0.63), "콴텍": _at(0.15)}      # 값은 달라도 상태는 같다
    assert ed.state_key(a, 0.70) == ed.state_key(b, 0.70)


def test_a_changed_state_yields_a_new_key():
    a = {"키움": _at(0.61)}
    b = {"키움": _at(0.70)}
    assert ed.state_key(a, 0.70) != ed.state_key(b, 0.70)


def test_a_changed_target_yields_a_new_key():
    """목표가 70%→50%로 바뀌면 상태가 같아 보여도 알려야 한다."""
    a = {"키움": _at(0.70, target=0.70)}
    assert ed.state_key(a, 0.70) != ed.state_key(a, 0.50)


def test_the_key_is_order_independent():
    a = {"키움": _at(0.61), "콴텍": _at(0.11)}
    b = {"콴텍": _at(0.11), "키움": _at(0.61)}
    assert ed.state_key(a, 0.70) == ed.state_key(b, 0.70)


# ─── 표시 ────────────────────────────────────────────


def test_the_report_names_the_target_and_the_reason():
    text = ed.format_report({"키움": _at(0.61)}, 0.70,
                            "KOSPI > 200일선 → 주식 우위(70%)")
    assert "70%" in text and "200일선" in text


def test_a_normal_slot_has_no_action_line():
    text = ed.format_report({"키움": _at(0.70)}, 0.70)
    assert "└" not in text
