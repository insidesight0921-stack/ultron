"""execution_price.py — 체결가 확정 (v1, 순수)

**막으려는 것**: 자동 매수가 **일봉 종가**를 진입가로 기록하고 있었다.
`kium_bot`의 스캔 결과 `current_price`는 `close.iloc[-1]`, 즉 일봉의 마지막 종가다.
장중에 사도 전 거래일 종가가 진입가로 남고, 캐시가 밀리면 며칠 전 값이 들어간다.
2026-06-08에는 4거래일 전 종가가 들어가 8종목이 22분 만에 전량 손절됐다
(−7.9% ~ −25.6%, 합계 −6,786,077원).

**원칙**: 종목을 고르는 데는 일봉을 써도, **체결가는 실행 시점의 실제 시세**여야 한다.
그 둘을 섞으면 존재하지 않았던 가격으로 진입한 기록이 남는다.

  - 실시간 시세를 얻으면 → 그 가격으로 체결하고, **수량도 그 가격으로 다시 계산**한다.
    스캔가로 계산한 수량을 그대로 쓰면 배정 금액이 어긋난다.
  - 실시간 시세를 못 얻으면 → **일봉 종가로 대체하지 않는다.** 대기 큐로 보내
    다음 개장 때 다시 판단한다. 모르는 가격으로 사느니 안 사는 편이 낫다.

`price_sanity`가 이미 기록된 스테일을 *탐지*한다면, 이 모듈은 스테일이 *생기지 않게* 한다.
탐지는 놓치는 게 있지만(부드러운 임계값으로는 정상 배치와 구분되지 않는다),
소스를 바꾸면 애초에 생기지 않는다.
"""
from __future__ import annotations

from typing import Iterable, Optional

# 스캔가와 실시간가의 괴리가 이보다 크면 알린다(차단하지는 않는다).
DRIFT_NOTE_PCT = 10.0


def drift_pct(signal_price: Optional[float], live_price: Optional[float]) -> Optional[float]:
    """스캔 시점 가격 대비 실행 시점 가격의 괴리(%)."""
    if not signal_price or signal_price <= 0 or not live_price or live_price <= 0:
        return None
    return round((live_price / signal_price - 1) * 100, 2)


def resolve(item: dict, live_price: Optional[float], alloc: float) -> dict:
    """한 종목의 체결가·수량 확정(순수).

    item: {ticker, name, price(스캔가)}
    반환: {verdict: fill|defer, price, qty, drift, reason, ticker, name}
    """
    base = {"ticker": item.get("ticker"), "name": item.get("name"),
            "signal_price": float(item.get("price") or 0)}
    if not live_price or live_price <= 0:
        return {**base, "verdict": "defer", "price": None, "qty": 0, "drift": None,
                "reason": "실시간 시세를 얻지 못함 — 일봉 종가로 대체하지 않고 대기 큐로 보냄"}
    qty = int(float(alloc) // live_price)
    if qty < 1:
        return {**base, "verdict": "defer", "price": live_price, "qty": 0,
                "drift": drift_pct(base["signal_price"], live_price),
                "reason": f"실시간가 {live_price:,.0f}원 기준 배정금액 부족"}
    d = drift_pct(base["signal_price"], live_price)
    return {**base, "verdict": "fill", "price": live_price, "qty": qty, "drift": d,
            "reason": (f"실시간가로 체결 (스캔가 대비 {d:+.2f}%)" if d is not None
                       else "실시간가로 체결")}


def resolve_batch(items: Iterable[dict], live_prices: dict, alloc: float) -> list[dict]:
    """배치 전체의 체결가 확정(순수). live_prices: {ticker: 현재가 or None}."""
    return [resolve(i, live_prices.get(str(i.get("ticker"))), alloc) for i in items]


def large_drifts(results: Iterable[dict], threshold: float = DRIFT_NOTE_PCT) -> list[dict]:
    """스캔가와 실행가가 크게 벌어진 건들. 차단하지 않고 알리기만 한다 —
    스캔 후 실제로 움직인 것일 수도 있고, 그 자체가 정보다."""
    return [r for r in results
            if r.get("drift") is not None and abs(r["drift"]) >= threshold]


def format_resolution(results: list[dict]) -> str:
    """체결가 확정 결과(순수). 알릴 것이 없으면 빈 문자열."""
    deferred = [r for r in results if r["verdict"] == "defer"]
    drifted = large_drifts([r for r in results if r["verdict"] == "fill"])
    if not deferred and not drifted:
        return ""
    lines = []
    if deferred:
        lines.append(f"⏳ *실시간 시세 없음 {len(deferred)}건 — 대기 큐로 보냅니다*")
        for r in deferred:
            lines.append(f"• {r['name'] or r['ticker']}: {r['reason']}")
        lines.append("_일봉 종가로 대체하면 실제로는 살 수 없었던 가격에 산 것이 됩니다._")
    if drifted:
        if lines:
            lines.append("")
        lines.append(f"ℹ️ 스캔가 대비 {DRIFT_NOTE_PCT:.0f}% 이상 움직인 종목 {len(drifted)}건")
        for r in drifted:
            lines.append(f"• {r['name'] or r['ticker']}: "
                         f"{r['signal_price']:,.0f} → {r['price']:,.0f}원 ({r['drift']:+.1f}%)")
    return "\n".join(lines)
