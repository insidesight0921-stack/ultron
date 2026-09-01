"""emergency_response.py — 시장 급락 긴급 대응 (순수 코어). **매도하지 않는다.**

계획서(v3.17 설계)에는 이렇게 적혀 있었다.

    VKOSPI 20~30 → 전 포지션 75% 축소
    VKOSPI 30 초과 → 전 포지션 50% 축소
    코스피 5일 −7% → 전 슬롯 현금 50% 강제 전환

**그대로 붙이면 규칙이 아니라 상시 조치가 된다.** 2026-09-01 실측(VKOSPI
407일 · 코스피 357일):

    VKOSPI > 30      198일 (48.6%)  ← 절반의 날에 "전 포지션 50% 축소"
    VKOSPI 20~30     154일 (37.8%)
    VKOSPI < 20       55일 (13.5%)  ← "정상 운용"이 오히려 예외
    코스피 5일 −7%    진입 12회 = 연 8.5회

20/30은 VKOSPI가 15~30을 오간다는 전제에서 나온 값이고, −7%는 일간 표준편차가
1% 남짓인 시장을 가정한 값이다. 이 시장은 일간 표준편차가 **2.885%p**다.

**그래서 분포에서 다시 잡았다.** 긴급은 드물어야 읽힌다.

    VKOSPI ≥ 77.8 (p90)      진입 연 1.9회 · 평균 13.7일 지속
    코스피 5일 ≤ −15.0% (p1)  진입 연 2.1회

**조치는 두 갈래로 갈린다.**

  자동으로 하는 것 — 신규 매수 차단. 되돌릴 수 있고 아무것도 팔지 않는다.
  사람에게 남기는 것 — 매도·현금 전환. 되돌릴 수 없는 조치는 승인 없이 하지
  않는다(슬롯 일일 손실 한도·`equity_drift`와 같은 원칙).

그리고 **상태가 바뀔 때만 알린다.** 경보 구간은 평균 13.7일 지속되므로 매일
보내면 2주 내내 같은 말이 오고, 그러면 사람은 그 알림을 안 보게 된다.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

log = logging.getLogger("emergency_response")

# 2026-09-01 실측 백분위. 값의 유래를 상수 옆에 남긴다.
VKOSPI_EMERGENCY = 77.8      # p90 · 진입 연 1.9회
DROP_WINDOW = 5              # 거래일
DROP_EMERGENCY_PCT = -15.0   # 5일 수익률 p1 · 진입 연 2.1회
MEASURED_AT = "2026-09-01"

NORMAL, ALERT, SEVERE = "정상", "경보", "심각"
LEVEL_ORDER = {NORMAL: 0, ALERT: 1, SEVERE: 2}


def window_return_pct(closes: Iterable[float],
                      window: int = DROP_WINDOW) -> Optional[float]:
    """최근 N거래일 수익률(%). 표본이 모자라면 None(순수).

    **0을 돌려주지 않는다** — 0은 '안 움직였다'는 사실이고 모르는 것과 다르다.
    """
    vals = [float(c) for c in (closes or []) if c is not None]
    if len(vals) <= window:
        return None
    base = vals[-1 - window]
    if not base:
        return None
    return round((vals[-1] / base - 1) * 100, 3)


def assess(vkospi: Optional[float], kospi_closes: Optional[list] = None, *,
           vkospi_high: float = VKOSPI_EMERGENCY,
           drop_pct: float = DROP_EMERGENCY_PCT,
           window: int = DROP_WINDOW) -> dict:
    """지금 긴급 상태인가(순수).

    **모르는 것은 정상으로 세지 않는다.** VKOSPI를 못 읽었으면 그 트리거는
    '미확보'이지 '안 걸림'이 아니다 — 둘을 합치면 수집이 멈춘 날 조용히
    안전하다고 말하게 된다.
    """
    triggers = []
    unknown = []

    if vkospi is None:
        unknown.append("VKOSPI")
    else:
        try:
            v = float(vkospi)
        except (TypeError, ValueError):
            unknown.append("VKOSPI")
            v = None
        if v is not None and v >= vkospi_high:
            triggers.append({"name": "VKOSPI", "value": v,
                             "threshold": vkospi_high,
                             "detail": f"VKOSPI {v:.1f} ≥ {vkospi_high}(상위 10%)"})

    ret = window_return_pct(kospi_closes, window)
    if ret is None:
        unknown.append(f"코스피 {window}일 수익률")
    elif ret <= drop_pct:
        triggers.append({"name": "코스피급락", "value": ret,
                         "threshold": drop_pct,
                         "detail": f"코스피 {window}일 {ret:+.1f}% ≤ {drop_pct}%(하위 1%)"})

    if len(triggers) >= 2:
        level = SEVERE
    elif triggers:
        level = ALERT
    else:
        level = NORMAL
    return {"level": level, "triggers": triggers, "unknown": unknown,
            "vkospi": vkospi, "window_return": ret, "window": window}


def blocks_new_buys(assessment: dict) -> bool:
    """경보 이상이면 신규 매수를 막는다. **파는 것은 여기서 하지 않는다.**"""
    return LEVEL_ORDER.get((assessment or {}).get("level"), 0) >= 1


def state_key(assessment: dict) -> str:
    """알림 중복을 막는 열쇠(순수). **값이 아니라 상태로 만든다.**

    VKOSPI 78.1과 78.4는 같은 상태다. 값을 열쇠에 넣으면 매일 달라져서
    "상태가 바뀔 때만"이 무너진다.
    """
    a = assessment or {}
    names = ",".join(sorted(t["name"] for t in a.get("triggers") or []))
    unk = ",".join(sorted(a.get("unknown") or []))
    return f"{a.get('level')}|{names}|미확보:{unk}"


def transition(previous: Optional[str], now: dict) -> Optional[str]:
    """`enter` / `escalate` / `deescalate` / `clear` / None(순수)."""
    lvl = (now or {}).get("level", NORMAL)
    prev = previous or NORMAL
    if prev == lvl:
        return None
    a, b = LEVEL_ORDER.get(prev, 0), LEVEL_ORDER.get(lvl, 0)
    if a == 0 and b > 0:
        return "enter"
    if b == 0 and a > 0:
        return "clear"
    return "escalate" if b > a else "deescalate"


def format_alert(assessment: dict, *, kind: str,
                 previous: Optional[str] = None) -> str:
    """사람이 읽는 알림(순수). **무엇을 자동으로 했고 무엇을 안 했는지 말한다.**"""
    a = assessment or {}
    lvl = a.get("level", NORMAL)
    if kind == "clear":
        lines = [f"✅ 긴급 상태 해제 — {previous} → {lvl}"]
        if a.get("vkospi") is not None:
            lines.append(f"   VKOSPI {float(a['vkospi']):.1f}")
        if a.get("window_return") is not None:
            lines.append(f"   코스피 {a['window']}일 {a['window_return']:+.1f}%")
        lines.append("   신규 매수 차단을 풉니다.")
        return "\n".join(lines)

    head = {"enter": "🚨 긴급", "escalate": "🚨 격상", "deescalate": "⚠️ 완화"}
    lines = [f"{head.get(kind, '🚨')} — {lvl}"]
    if previous and previous != lvl:
        lines[0] += f" (이전 {previous})"
    lines.append("")
    for t in a.get("triggers") or []:
        lines.append(f"  • {t['detail']}")
    if a.get("unknown"):
        # **미확보를 조용히 넘기지 않는다.** 수집이 멈춘 날 안전하다고
        # 말하는 것이 이 프로젝트에서 반복된 실패다.
        lines.append(f"  • ⚠️ 미확보: {', '.join(a['unknown'])} — 판정에서 빠졌습니다")
    lines.append("")
    lines.append("자동으로 한 것: **신규 매수 차단**(기존 보유는 그대로)")
    lines.append("사람이 정할 것: 매도·현금 전환 — 되돌릴 수 없어 자동으로 하지 않습니다")
    lines.append("")
    lines.append(f"_임계값은 {MEASURED_AT} 실측 분포 기준"
                 f"(VKOSPI p90 · 코스피 {DROP_WINDOW}일 p1)입니다._")
    lines.append("_같은 상태가 이어지는 동안에는 다시 보내지 않습니다._")
    return "\n".join(lines)
