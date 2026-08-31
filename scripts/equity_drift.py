"""equity_drift.py — 슬롯 주식 비중이 목표에서 얼마나 벗어났는가 (순수 코어)

`kium_bot.compute_weight_recommendation`(KOSPI 200일선 위 → 주식 70%, 아래 →
50%)을 **계속 따라가게** 하는 판정부. v3.59에서 매수 예산에 목표를 연결했지만,
그건 **리밸런싱 시점에만** 적용된다. 그 사이 시세가 움직이거나 자동청산이 나가면
비중은 목표에서 벗어나고, 아무도 모른다.

**하는 일과 하지 않는 일.**

  한다  — 현재 비중과 목표를 비교해 상태를 낸다
  한다  — 상태가 **바뀔 때만** 알린다(매일 같은 말을 하면 안 보게 된다)
  안 한다 — **강제 매도.** 되돌릴 수 없는 조치는 사람 판단에 남긴다.
            슬롯 일일 손실 한도에서 이미 같은 선을 그었다(신규 진입만 차단).
  안 한다 — 정기 일정 밖의 추가 매수. 스캔 순위가 지난 종목을 지금 더 사지 않는다.

**초과 상태는 이미 사실상 차단돼 있다.** `slot_budget.budget_for_new`가 보유
평가액을 목표에서 빼므로, 목표를 넘으면 예산이 0이 되고 수량도 0이 된다.
문제는 그 결과가 "배정금액 부족"으로 표시된다는 것이다 — **원인이 아닌 증상**이다.
이 모듈이 그 상태에 이름을 붙인다.

**전제의 한계를 적어 둔다.**
- VKOSPI ±10%p 항은 수집 소스가 없어 죽어 있다. 지금 도는 것은 200일선 규칙
  하나뿐이라 목표는 **70% 아니면 50%** 둘뿐이다.
- 200일선 규칙은 완만한 지수를 상정한다. 이 시장의 지수 일간 변동은 상위가
  +17.9%/−12.1%다(2026-08-31 실측, 캐시 319일). 하향 신호가 뜰 때는 이미
  크게 빠진 뒤일 수 있다.
"""
from __future__ import annotations

# 목표 ±이 폭 안이면 '정상'으로 본다. 없으면 시세가 조금만 움직여도 상태가 바뀐다.
NORMAL_BAND = 0.05

NORMAL = "정상"
OVER = "초과"
UNDER = "미달"


def assess(cash: float, holdings_value: float, target_weight: float,
           *, band: float = NORMAL_BAND) -> dict:
    """슬롯 하나의 비중 판정(순수).

    반환: {total, current, target, gap, state, action, detail}
      state   정상 | 초과 | 미달
      action  사람이 할 일(또는 '없음'). **여기서 매매하지 않는다.**
    """
    cash = max(0.0, float(cash or 0))
    held = max(0.0, float(holdings_value or 0))
    total = cash + held
    if total <= 0:
        return {"total": 0.0, "current": None, "target": target_weight,
                "gap": None, "state": NORMAL, "action": "없음",
                "detail": "슬롯 자본이 없습니다"}
    current = held / total
    gap = current - float(target_weight)
    if abs(gap) <= band:
        state, action = NORMAL, "없음"
    elif gap > 0:
        state = OVER
        action = "신규 매수 차단(기존 보유는 그대로 — 강제 청산하지 않습니다)"
    else:
        state = UNDER
        action = "다음 정기 리밸런싱에서 보충"
    need = float(target_weight) * total - held
    return {
        "total": total, "current": current, "target": float(target_weight),
        "gap": gap, "state": state, "action": action, "need": need,
        "detail": (f"주식 {current*100:.1f}% / 목표 {float(target_weight)*100:.0f}% "
                   f"({gap*100:+.1f}%p) — {state}"),
    }


def blocks_new_buys(result: dict) -> bool:
    """이 상태에서 신규 매수를 막아야 하는가(순수)."""
    return (result or {}).get("state") == OVER


def state_key(results: dict, weight: float) -> str:
    """알림 중복 판정용 키(순수).

    **상태와 목표 비중이 그대로면 다시 알리지 않는다.** 매일 같은 말을 보내면
    사람은 그 알림을 안 보게 되고, 정작 바뀌었을 때도 놓친다.
    """
    parts = [f"w={float(weight):.2f}"]
    for name in sorted(results or {}):
        parts.append(f"{name}={results[name].get('state')}")
    return "|".join(parts)


def format_report(results: dict, weight: float, reason: str = "") -> str:
    """상태가 바뀌었을 때 보내는 문구(순수)."""
    head = f"⚖️ 주식 목표 비중 {float(weight)*100:.0f}%"
    if reason:
        head += f"\n_{reason}_"
    lines = [head, ""]
    for name in sorted(results or {}):
        r = results[name]
        if r.get("current") is None:
            lines.append(f"• {name}: {r['detail']}")
            continue
        mark = {NORMAL: "✅", OVER: "🔺", UNDER: "🔻"}.get(r["state"], "•")
        lines.append(f"{mark} *{name}* {r['detail']}")
        if r["state"] != NORMAL:
            lines.append(f"    └ {r['action']} · 차이 {abs(r['need']):,.0f}원")
    lines.append("")
    lines.append("_강제 매도는 하지 않습니다. 보유 종목 청산은 되돌릴 수 없어_")
    lines.append("_사람 판단에 남깁니다(슬롯 일일 손실 한도와 같은 원칙)._")
    return "\n".join(lines)
