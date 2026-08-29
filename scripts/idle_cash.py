"""idle_cash.py — 유휴 슬롯 자본의 단기 파킹 (순수 코어)

계획서 5단계 "IPO 없는 기간 TIGER 단기통안채 자동 운용".

**왜 필요한가.** 2026-08-29 실측: IPO 슬롯 2,000만원이 개설(2026-05-10) 이후
**3.5개월간 거래 0건**이었다. 슬롯 자본은 성과 집계에 그대로 잡히는데 수익은
0이라, 유휴 자본이 전체 수익률을 구조적으로 끌어내린다. IPO는 본질적으로
간헐적이므로 이 상태가 기본값이 된다.

설계에서 조심한 것 세 가지.

1. **채터링 방지.** 매도 임계(청약 D-5)보다 매수 임계(D-10)를 크게 둔다. 같은
   임계값을 쓰면 청약이 하나 잡힐 때마다 사고팔기를 반복한다. 수수료만 나간다.

2. **결제 지연을 모사한다.** paper_db는 매도 즉시 현금을 올려 주지만, 실제로는
   ETF 매도 대금이 T+2에 들어온다. paper에서 D-1에 팔아도 되게 두면 **실전에서
   증거금을 못 내는 전략이 paper에서는 멀쩡해 보인다.** 측정이 현실과 어긋나면
   결론이 뒤집힌다는 것을 이 프로젝트에서 여러 번 겪었다.

3. **모르면 움직이지 않는다.** 시세를 모르거나 청약 일정을 못 읽었으면 `hold`다.
   "일정이 없다"와 "일정을 확인하지 못했다"는 다르다 — 후자에서 매수하면
   청약이 코앞인데 자본을 묶어 버린다.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

# TIGER 단기통안채. 계획서가 지정한 종목이다.
PARK_TICKER = "157450"
PARK_NAME = "TIGER 단기통안채"

# 청약 D-5에 현금화한다. 실제 ETF 매도 대금은 T+2에 들어오고, 증거금은 청약일에
# 필요하다. 영업일 기준 3일 + 여유 2일. paper_db는 즉시 반영하지만 **실전 제약을
# 그대로 모사한다** — 그래야 paper 성적이 실전과 같은 조건에서 나온다.
SELL_LEAD_DAYS = 5

# 매수는 더 멀리 본다(채터링 방지). D-10 안에 청약이 없어야 산다.
BUY_CLEAR_DAYS = 10

# 이보다 적은 현금은 파킹하지 않는다. 수수료·호가 단위 대비 실익이 없다.
MIN_PARK_AMOUNT = 1_000_000


def _to_date(value) -> Optional[date]:
    """YYYYMMDD 문자열 → date. 못 읽으면 None(오늘로 대체하지 않는다)."""
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def next_subscription(subs: Iterable, today: date) -> Optional[date]:
    """앞으로 다가올 청약 시작일 중 가장 이른 것. 없으면 None(순수).

    지난 청약은 무시한다. 읽을 수 없는 날짜도 무시하되, 호출부가 그 사실을
    알 수 있도록 `unreadable_subscriptions()`를 따로 둔다.
    """
    upcoming = [d for d in (_to_date(s) for s in subs) if d is not None and d >= today]
    return min(upcoming) if upcoming else None


def unreadable_subscriptions(subs: Iterable) -> int:
    """날짜를 읽지 못한 건수. **0이 아니면 매수하지 않는다.**

    "청약이 없다"와 "청약 일정을 확인하지 못했다"는 다르다. 후자에서 자본을
    묶으면 청약이 코앞인데 현금이 없는 상태가 된다.
    """
    return sum(1 for s in subs if _to_date(s) is None)


def plan_idle_action(
    *,
    parked_qty: int,
    cash: float,
    subscriptions: Iterable,
    today: Optional[date] = None,
    price: Optional[float] = None,
    sell_lead_days: int = SELL_LEAD_DAYS,
    buy_clear_days: int = BUY_CLEAR_DAYS,
    min_park_amount: float = MIN_PARK_AMOUNT,
) -> dict:
    """유휴 자본을 어떻게 할지 결정한다(순수).

    반환: {"action": "buy"|"sell"|"hold", "qty": int, "reason": str}
    """
    today = today or date.today()
    unreadable = unreadable_subscriptions(subscriptions)
    nearest = next_subscription(subscriptions, today)
    days_to_sub = (nearest - today).days if nearest else None

    # ── 보유 중 ────────────────────────────────────────
    if parked_qty > 0:
        if days_to_sub is not None and days_to_sub <= sell_lead_days:
            return {"action": "sell", "qty": parked_qty,
                    "reason": f"청약 D-{days_to_sub} — 증거금 확보를 위해 현금화"}
        if unreadable:
            # 일정을 못 읽었으면 **보수적으로 현금화**한다. 보유 중일 때의
            # 미확인은 "혹시 청약이 코앞일 수도"를 뜻하므로 위험이 한쪽이다.
            return {"action": "sell", "qty": parked_qty,
                    "reason": f"청약 일정 {unreadable}건을 읽지 못함 — 보수적으로 현금화"}
        return {"action": "hold", "qty": 0,
                "reason": (f"청약 D-{days_to_sub}까지 여유" if days_to_sub is not None
                           else "예정된 청약 없음 — 파킹 유지")}

    # ── 미보유 ────────────────────────────────────────
    if unreadable:
        return {"action": "hold", "qty": 0,
                "reason": f"청약 일정 {unreadable}건을 읽지 못함 — 매수하지 않음"}
    if days_to_sub is not None and days_to_sub <= buy_clear_days:
        return {"action": "hold", "qty": 0,
                "reason": f"청약 D-{days_to_sub} — 곧 필요한 자금"}
    if cash < min_park_amount:
        return {"action": "hold", "qty": 0,
                "reason": f"현금 {cash:,.0f}원 < 최소 {min_park_amount:,.0f}원"}
    if not price or price <= 0:
        # 시세를 모르면 사지 않는다(매수는 fail-closed — 이 프로젝트의 기존 원칙).
        return {"action": "hold", "qty": 0, "reason": "시세 미확보 — 매수 보류"}

    qty = int(cash // price)
    if qty <= 0:
        return {"action": "hold", "qty": 0,
                "reason": f"현금 {cash:,.0f}원으로 1주도 살 수 없음"}
    return {"action": "buy", "qty": qty,
            "reason": (f"예정 청약 없음(D-{days_to_sub} 밖)" if days_to_sub is not None
                       else "예정된 청약 없음") + f" — {qty:,}주 파킹"}


def format_plan(plan: dict, *, slot: str = "IPO") -> str:
    """결정 한 줄 요약."""
    mark = {"buy": "🟢 매수", "sell": "🔴 매도", "hold": "⚪ 유지"}
    head = mark.get(plan["action"], plan["action"])
    qty = f" {plan['qty']:,}주" if plan.get("qty") else ""
    return f"{head}{qty} · {slot} 슬롯 · {PARK_NAME} — {plan['reason']}"
