"""slot_allocation.py — 슬롯 비중 합계 검증 (순수)

**왜 필요한가.** 2026-08-29 실측: 슬롯 비중 합계가 **110%**였다(콴텍 40 + 키움 40
+ IPO 20 + 마이퀀트 10). 마이퀀트 슬롯이 나중에 추가되면서 기존 90%에 그냥
얹혔고, 아무도 합계를 보지 않았다.

`equity_curve`와 `paper_metrics`는 슬롯 시드를 **`비중 × 포트폴리오 시드`** 로
계산한다. 합계가 110%면 **넣지 않은 1,000만원 위에서 수익률을 잰다.** 실제
자산곡선이 첫날 110,977,856원으로 시작했다(포트폴리오 시드는 1억).

영향은 표시 오류에 그치지 않았다.

  포트폴리오 수익률   −5.94%  →  실제 운용분만 −8.15%
  초과수익           +2.09%p →  **−0.12%p**   ← 전환 기준 판정이 뒤집힌다

**틀린 방향이 유리한 쪽이었다.** 이 프로젝트에서 같은 성격의 오류가 반복됐다
(샤프 √252 오연환산, 밴드 하단을 확정가로 오인). 성적이 나쁘게 나오는 오류는
눈에 띄어 금방 잡히지만, 좋게 나오는 오류는 그냥 넘어간다. 그래서 **합계를
자동으로 보는 장치**를 둔다.

기존 검증(`private_read_store`)은 `0 ≤ allocation_pct ≤ 1`로 **개별 값만** 봤다.
개별로는 전부 정상이라 통과했다.
"""
from __future__ import annotations

from typing import Iterable, Optional

# 부동소수 오차 허용치. 0.1%p까지는 같은 값으로 본다.
TOLERANCE = 0.001


def total_allocation(slots: Iterable[dict]) -> float:
    """비중 합계(순수). 값이 없거나 숫자가 아니면 0으로 센다."""
    total = 0.0
    for slot in slots or []:
        value = (slot or {}).get("allocation_pct")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        total += float(value)
    return round(total, 6)


def check(slots: Iterable[dict], *, tolerance: float = TOLERANCE) -> dict:
    """비중 합계 점검(순수).

    반환: {ok, total, gap, slots, detail}
      ok     100%와 tolerance 안에서 일치하는가
      gap    100% 대비 초과(+)/미달(−)
      detail 사람이 읽는 한 줄

    **판정만 하고 고치지 않는다.** 어떤 슬롯을 얼마로 바꿀지는 운용 결정이라
    코드가 임의로 정하면 안 된다 — 비중을 바꾸면 과거 수익률이 소급해서 달라진다.
    """
    total = total_allocation(slots)
    gap = round(total - 1.0, 6)
    names = [str((s or {}).get("name") or "?") for s in (slots or [])]
    if abs(gap) <= tolerance:
        detail = f"비중 합계 {total*100:.1f}% — 정상"
    else:
        direction = "초과" if gap > 0 else "미달"
        detail = (f"⚠️ 비중 합계 {total*100:.1f}% ({gap*100:+.1f}%p {direction}) — "
                  f"슬롯 {len(names)}개: {', '.join(names)}. "
                  f"시드가 실제 자본과 어긋나 수익률·MDD·초과수익이 왜곡됩니다")
    return {"ok": abs(gap) <= tolerance, "total": total, "gap": gap,
            "slots": names, "detail": detail}


def would_exceed(slots: Iterable[dict], new_pct: float,
                 *, tolerance: float = TOLERANCE) -> bool:
    """슬롯을 하나 더 넣으면 100%를 넘는가(순수).

    **추가 시점에 막는 것이 가장 싸다.** 이미 거래가 쌓인 뒤에 비중을 바꾸면
    과거 성과가 소급해서 달라져 되돌리기 어렵다. 마이퀀트가 정확히 그 경우였다.
    """
    if isinstance(new_pct, bool) or not isinstance(new_pct, (int, float)):
        return False
    return total_allocation(slots) + float(new_pct) > 1.0 + tolerance


def seed_of(slot: dict, seed_capital: float,
            slots: Optional[Iterable[dict]] = None) -> Optional[float]:
    """슬롯 시드. **합계가 어긋나 있으면 None을 돌려준다.**

    틀린 분모로 계산한 수익률을 내놓느니 "계산하지 않았다"가 낫다 —
    이 프로젝트에서 반복된 원칙이다(모르는 것을 채우지 않는다).
    `slots`를 주지 않으면 검증 없이 계산한다(호출부가 이미 확인한 경우).
    """
    pct = (slot or {}).get("allocation_pct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    if slots is not None and not check(slots)["ok"]:
        return None
    return float(pct) * float(seed_capital)
