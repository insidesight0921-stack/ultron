"""slot_budget.py — 매수 예산 배분 (순수 코어)

**왜 필요한가.** 2026-08-31 실측: 슬롯 투입률이 목표(주식 70%)에 못 미쳤다.

    콴텍 11.5% · 키움 61.4% · IPO 0% · 마이퀀트 0% → 전체 25.4%

키움 08-31 09:23 배치를 원 단위까지 복원했다.

    가용 현금        25,036,937원   보유 평가 9,849,600원(3종목)
    스캔 8종목 중 신규 5종목
    alloc_per = 25,036,937 / 8 = 3,129,617원
    집행 13,426,550원 → 배치 후 주식 비중 66.7%

**처음엔 "보유 3종목 몫 9.4M이 이중 배정됐다"고 봤는데 그건 틀린 진단이었다.**
그 3종목은 이미 현금을 써서 산 것이라 다시 쓸 몫이 없다. 목표(70%) 대비 실제
차이는 46%가 아니라 **3.3%p**다. 잘못된 크기로 문제를 키우면 엉뚱한 데를 고친다.

진짜 문제는 둘이다.

**① 기존식에는 목표가 없다.** `현금 ÷ 전체 스캔 종목수`는 70%를 겨냥한 식이
아니다. 보유/현금 구성에 따라 결과가 떠다닌다(정수 내림 없다고 가정, 스캔 8종목):

    보유 0원(0종목)  현금 3,500만 → 의도 비중 100.0%
    보유 500만(2종목) 현금 3,000만 →         78.6%
    보유 985만(3종목) 현금 2,504만 →         73.1%   ← 이번 배치
    보유 1,500만(4종목) 현금 2,000만 →        71.4%

이번에 70% 근처에 떨어진 것은 **우연**이다. 보유가 없으면 현금을 다 털어 넣는다.
그리고 `compute_weight_recommendation`의 주식 70% 권고는 **매수 경로 어디에서도
읽지 않았다** — 출력 문구에만 쓰였다. 이 모듈이 그 값을 실제 예산으로 옮긴다.

**② 정수 주식수 내림 손실이 크다.** SK하이닉스는 1주가 예산의 51.7%라 나머지가
그냥 남는다. 이번 배치에서 2,592,350원이 그렇게 남았다.

설계
  1) **슬롯 총액**(현금 + 보유 평가)에 목표 주식 비중을 곱해 주식 목표액을 낸다.
  2) 이미 보유한 종목의 평가액은 목표에서 **뺀다**(이미 주식이 된 몫이다).
  3) 남은 목표액을 신규 종목 수로 나눈다.
  4) 정수 내림으로 남은 돈은 **싼 종목부터 2차 배분**한다. 비싼 종목부터 담으면
     한 종목이 남은 돈을 다 먹어 비중이 틀어진다.
  5) 현금이 목표보다 적으면 현금이 상한이다(빚내지 않는다).
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

log = logging.getLogger("slot_budget")

# 주식 목표 비중을 알 수 없을 때 쓰는 값. `compute_weight_recommendation`의
# 디폴트와 같다(계획서 §키움봇 v2: KOSPI 200일선 위 → 주식 70%).
DEFAULT_EQUITY_WEIGHT = 0.70

# 현금을 이만큼은 남긴다(수수료·슬리피지·정산 지연). 0이면 딱 맞춰 쓰다 실패한다.
CASH_BUFFER = 0.01


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def slot_total(cash: float, holdings_value: float) -> float:
    """슬롯 총액 = 현금 + 보유 평가. **분모는 이것이다.**"""
    return max(0.0, float(cash or 0)) + max(0.0, float(holdings_value or 0))


def equity_target(cash: float, holdings_value: float,
                  equity_weight: float = DEFAULT_EQUITY_WEIGHT) -> float:
    """이 슬롯이 주식에 두어야 할 금액."""
    w = _num(equity_weight)
    if w is None or not (0.0 <= w <= 1.0):
        w = DEFAULT_EQUITY_WEIGHT
    return slot_total(cash, holdings_value) * w


def budget_for_new(cash: float, holdings_value: float, n_new: int,
                   *, equity_weight: float = DEFAULT_EQUITY_WEIGHT,
                   cash_buffer: float = CASH_BUFFER) -> dict:
    """신규 종목 1개당 예산(순수).

    반환: {per_name, spendable, target, already, capped_by}

    `capped_by`가 `"cash"`면 목표만큼 살 현금이 없다는 뜻이다 — 목표 미달이
    **배분 실패가 아니라 현금 부족**임을 호출부가 구분할 수 있어야 한다.
    """
    if n_new <= 0:
        return {"per_name": 0.0, "spendable": 0.0, "target": 0.0,
                "already": float(holdings_value or 0), "capped_by": "no_names"}
    target = equity_target(cash, holdings_value, equity_weight)
    already = max(0.0, float(holdings_value or 0))
    # 이미 보유한 몫은 목표에서 뺀다 — 그 돈은 이미 주식이 됐다.
    remaining = max(0.0, target - already)
    usable_cash = max(0.0, float(cash or 0)) * (1.0 - max(0.0, cash_buffer))
    spendable = min(remaining, usable_cash)
    return {
        "per_name": spendable / n_new,
        "spendable": spendable,
        "target": target,
        "already": already,
        "capped_by": "cash" if usable_cash < remaining else "target",
    }


def allocate(prices: Iterable[tuple], cash: float, holdings_value: float,
             *, equity_weight: float = DEFAULT_EQUITY_WEIGHT,
             cash_buffer: float = CASH_BUFFER,
             second_pass: bool = True) -> dict:
    """종목별 매수 수량(순수).

    `prices`: [(ticker, price), ...] — 신규 진입 대상만.

    **2차 배분이 왜 필요한가.** 1차는 예산을 종목 수로 나눠 정수 주식수로 내린다.
    비싼 종목일수록 버림이 크다 — SK하이닉스는 1주가 예산의 51.7%라 나머지 48%가
    그냥 남았다. 남은 돈은 `second_pass_topup`이 **한 바퀴에 한 주씩** 돌려 담는다
    (한 종목에 몰아 담으면 그 종목만 예산의 배가 되어 비중이 틀어진다).
    """
    rows = [(str(t), _num(p)) for t, p in (prices or [])]
    valid = [(t, p) for t, p in rows if p and p > 0]
    skipped = [t for t, p in rows if not p or p <= 0]

    plan = budget_for_new(cash, holdings_value, len(valid),
                          equity_weight=equity_weight, cash_buffer=cash_buffer)
    per = plan["per_name"]
    qty = {t: int(per // p) for t, p in valid}
    spent = sum(qty[t] * p for t, p in valid)

    if second_pass and valid:
        top = second_pass_topup(qty, dict(valid), plan["spendable"] - spent)
        qty = top["quantities"]
        spent += top["spent"]

    return {
        "quantities": qty,
        "per_name": per,
        "spendable": plan["spendable"],
        "spent": spent,
        "leftover": plan["spendable"] - spent,
        "target": plan["target"],
        "capped_by": plan["capped_by"],
        "skipped_no_price": skipped,
    }


def format_plan(result: dict, *, equity_weight: float = DEFAULT_EQUITY_WEIGHT) -> str:
    """한 줄 요약. **목표 대비 얼마나 집행했는지**를 숨기지 않는다."""
    spendable = result.get("spendable") or 0.0
    spent = result.get("spent") or 0.0
    rate = (spent / spendable * 100) if spendable else 0.0
    parts = [f"예산 {spendable:,.0f}원 중 {spent:,.0f}원 집행({rate:.1f}%)",
             f"주식 목표 비중 {equity_weight*100:.0f}%"]
    if result.get("capped_by") == "cash":
        parts.append("현금 부족으로 목표 미달")
    if result.get("skipped_no_price"):
        parts.append(f"시세 미확보 {len(result['skipped_no_price'])}종목 제외")
    return " · ".join(parts)


def holdings_value(positions: Iterable[dict],
                   prices: Optional[dict] = None) -> dict:
    """보유 평가액(순수). 반환: {value, priced, unpriced, basis}

    현재가를 모르는 종목은 **취득원가로 대체하고 그 사실을 돌려준다.** 0으로
    치면 슬롯 총액이 작아져 목표 주식액이 줄고, 결국 덜 사게 된다 —
    모르는 값을 0으로 채우는 것도 '지어내기'다.
    """
    prices = prices or {}
    value = 0.0
    priced = unpriced = 0
    for pos in positions or []:
        try:
            qty = int((pos or {}).get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        px = _num(prices.get(str((pos or {}).get("ticker"))))
        if px and px > 0:
            priced += 1
        else:
            px = _num((pos or {}).get("avg_price")) or 0.0
            if px > 0:
                unpriced += 1
        value += px * qty
    return {"value": value, "priced": priced, "unpriced": unpriced,
            "basis": "시가" if not unpriced else f"시가 {priced} / 취득원가 {unpriced}"}


def second_pass_topup(quantities: dict, prices: dict, leftover: float) -> dict:
    """1차 배분 뒤 남은 돈을 **한 바퀴씩 돌며** 한 주씩 더 담는다(순수).

    반환: {quantities, added, spent, leftover}

    **싼 종목부터 몰아 담으면 안 된다.** 2026-08-31 배치에서 잔액 2,592,350원을
    최저가 종목(대우건설 18,650원)에 다 넣으면 139주가 추가돼 그 종목만 예산의
    1.9배가 된다. 비싼 종목부터 담으면 한 종목이 남은 돈을 다 먹는다.
    그래서 **싼 순서로 정렬한 뒤 한 바퀴에 한 주씩** 돌린다 — 잔액이 여러 종목에
    고르게 퍼지고, 어떤 종목도 크게 튀지 않는다.
    """
    out = dict(quantities or {})
    added: dict = {}
    left = float(leftover or 0)
    ordered = [(t, _num(prices.get(t))) for t in out]
    ordered = sorted([(t, p) for t, p in ordered if p and p > 0],
                     key=lambda x: x[1])
    spent = 0.0
    progressed = True
    while ordered and progressed:
        progressed = False
        for ticker, price in ordered:
            if price > left:
                continue
            out[ticker] = out.get(ticker, 0) + 1
            added[ticker] = added.get(ticker, 0) + 1
            left -= price
            spent += price
            progressed = True
    return {"quantities": out, "added": added, "spent": spent, "leftover": left}
