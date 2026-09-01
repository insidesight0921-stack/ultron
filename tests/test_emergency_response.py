"""test_emergency_response.py — 긴급 규칙이 상시 조치가 되지 않게, 그리고 팔지 않게.

계획서의 원안(VKOSPI 20/30, 코스피 5일 −7%)을 그대로 붙였다면 **절반의 날에
'전 포지션 50% 축소'가 발동**했다(2026-09-01 실측 48.6%). 이 파일은 두 가지를
고정한다.

1. 긴급은 **드물어야** 한다 — 실측 표본에서 발동 빈도를 검사한다.
2. 이 모듈은 **아무것도 팔지 않는다** — 되돌릴 수 없는 조치는 사람에게 남긴다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import emergency_response as er  # noqa: E402


# ─── 판정 ────────────────────────────────────────────


def test_a_calm_market_is_normal():
    a = er.assess(30.0, [100.0] * 10)
    assert a["level"] == er.NORMAL
    assert er.blocks_new_buys(a) is False


def test_high_vkospi_alone_is_an_alert():
    a = er.assess(90.0, [100.0] * 10)
    assert a["level"] == er.ALERT
    assert er.blocks_new_buys(a) is True


def test_a_crash_alone_is_an_alert():
    closes = [100.0] * 5 + [80.0]      # 5일 −20%
    a = er.assess(30.0, closes)
    assert a["level"] == er.ALERT
    assert a["triggers"][0]["name"] == "코스피급락"


def test_both_triggers_together_are_severe():
    closes = [100.0] * 5 + [80.0]
    a = er.assess(90.0, closes)
    assert a["level"] == er.SEVERE
    assert len(a["triggers"]) == 2


# ─── 모르는 것을 안전으로 세지 않는다 ────────────────


def test_a_missing_vkospi_is_named_not_counted_as_calm():
    """수집이 멈춘 날 조용히 '안전'이라고 말하는 것이 반복된 실패다."""
    a = er.assess(None, [100.0] * 10)
    assert "VKOSPI" in a["unknown"]
    msg = er.format_alert(er.assess(None, [100.0] * 5 + [80.0]), kind="enter")
    assert "미확보" in msg


def test_a_short_price_series_is_unknown_not_zero():
    a = er.assess(30.0, [100.0, 101.0])
    assert any("코스피" in u for u in a["unknown"])
    assert a["window_return"] is None


def test_a_garbage_vkospi_is_unknown_not_a_trigger():
    a = er.assess("몰라", [100.0] * 10)
    assert "VKOSPI" in a["unknown"]
    assert a["level"] == er.NORMAL


# ─── 아무것도 팔지 않는다 ────────────────────────────


def test_the_module_never_sells():
    """되돌릴 수 없는 조치는 사람 판단에 남긴다 — 매도 경로가 있으면 안 된다."""
    src = (ROOT / "scripts" / "emergency_response.py").read_text(encoding="utf-8")
    import ast
    import io
    import tokenize

    code = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        code.append(tok.string)
    code = " ".join(code)
    for forbidden in ("sell", "execute_trade", "paper_db", "order"):
        assert forbidden not in code, f"매도 경로 흔적: {forbidden}"
    assert ast.parse(src)


def test_the_alert_says_what_was_and_was_not_done():
    a = er.assess(90.0, [100.0] * 10)
    msg = er.format_alert(a, kind="enter")
    assert "신규 매수 차단" in msg
    assert "자동으로 하지 않습니다" in msg


# ─── 상태가 바뀔 때만 ────────────────────────────────


def test_the_key_is_a_state_not_a_value():
    """VKOSPI 78.1과 78.4는 같은 상태다 — 값을 넣으면 매일 알림이 간다."""
    a = er.assess(78.1, [100.0] * 10)
    b = er.assess(78.4, [100.0] * 10)
    assert er.state_key(a) == er.state_key(b)


def test_a_different_trigger_set_is_a_different_state():
    a = er.assess(90.0, [100.0] * 10)
    b = er.assess(90.0, [100.0] * 5 + [80.0])
    assert er.state_key(a) != er.state_key(b)


def test_transitions_are_named():
    calm = er.assess(30.0, [100.0] * 10)
    alert = er.assess(90.0, [100.0] * 10)
    severe = er.assess(90.0, [100.0] * 5 + [80.0])
    assert er.transition(er.NORMAL, alert) == "enter"
    assert er.transition(er.ALERT, severe) == "escalate"
    assert er.transition(er.SEVERE, alert) == "deescalate"
    assert er.transition(er.ALERT, calm) == "clear"
    assert er.transition(er.ALERT, alert) is None


def test_clearing_says_the_block_is_lifted():
    calm = er.assess(30.0, [100.0] * 10)
    msg = er.format_alert(calm, kind="clear", previous=er.ALERT)
    assert "해제" in msg and "차단을 풉니다" in msg


# ─── 실측 표본에서 드문가 ────────────────────────────


def _cached():
    d = ROOT / "data" / "shareable" / "cache" / "indices"
    vk = sorted(d.glob("market_index_VKOSPI_*.json"))
    kp = sorted(d.glob("market_index_KOSPI_*.json"))
    if not vk or not kp:
        return None, None
    v = json.loads(vk[-1].read_text(encoding="utf-8"))["series"]
    k = json.loads(kp[-1].read_text(encoding="utf-8"))["series"]
    return ({str(a): float(b) for a, b in zip(v["date"], v["close"])},
            {str(a): float(b) for a, b in zip(k["date"], k["close"])})


def test_the_emergency_is_rare_on_the_measured_sample():
    """**긴급은 드물어야 읽힌다.** 원안대로면 48.6%의 날에 발동했다."""
    vk, kp = _cached()
    if not vk:
        pytest.skip("지수 캐시 없음 — 맥에서만 검증된다")
    days = sorted(vk)
    closes = [kp[d] for d in sorted(kp)]
    hit = sum(1 for d in days if vk[d] >= er.VKOSPI_EMERGENCY)
    assert hit / len(days) < 0.15, "VKOSPI 트리거가 너무 자주 걸린다"
    r5 = [(closes[i] / closes[i - er.DROP_WINDOW] - 1) * 100
          for i in range(er.DROP_WINDOW, len(closes))]
    drops = sum(1 for x in r5 if x <= er.DROP_EMERGENCY_PCT)
    assert drops / len(r5) < 0.05, "급락 트리거가 너무 자주 걸린다"


def test_the_old_textbook_thresholds_are_not_used():
    """VKOSPI 20/30과 −7%는 이 시장에서 상시 조치가 된다."""
    assert er.VKOSPI_EMERGENCY > 30
    assert er.DROP_EMERGENCY_PCT < -7.0


def test_entries_not_days_are_what_the_user_sees():
    """경보는 평균 13.7일 이어진다 — 매일 보내면 2주 내내 같은 말이 온다."""
    vk, _ = _cached()
    if not vk:
        pytest.skip("지수 캐시 없음")
    days = sorted(vk)
    inside = [vk[d] >= er.VKOSPI_EMERGENCY for d in days]
    entries = sum(1 for i in range(1, len(inside)) if inside[i] and not inside[i - 1])
    entries += 1 if inside[0] else 0
    per_year = entries / len(days) * 252
    assert per_year <= 6, f"연 {per_year:.1f}회 — 긴급이라기엔 잦다"
