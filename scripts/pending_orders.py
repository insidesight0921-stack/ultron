"""pending_orders.py — 장외 신호 대기 큐 (v1, 순수 + 얇은 경계)

**문제**: 전체 매수 기록 91건 중 35건(38%)이 장 시간(09:00~15:30) 밖에 기록됐다
(21시 17건, 20시 9건, 17시 8건, 06시 1건). 밤 21시에 기록한 "현재가"는 당일 종가이고,
실전에서는 그 가격에 살 수 없다. 2026-08-25에는 갭 하락으로 손실이 드러났지만,
방향이 반대면 **성과를 부풀린다.** 페이퍼의 목적이 실전 전환 판단이므로 이 편향은
판단 자체를 무효로 만든다. 설계도가 경계하는 look-ahead bias의 한 형태다.

**선택한 해법(B안 + 재평가)**: 장외 신호는 체결하지 않고 큐에 넣는다. 다음 개장 후
첫 모니터 사이클에서 **그때의 실제 가격으로** 조건을 다시 보고 체결하거나 버린다.

재평가 규칙(모두 가설 — 표본이 쌓이면 재조정):
  - 갭 하락이 손절선(-7%) 이하면 **취소**. 사자마자 손절될 자리에 들어가지 않는다.
  - 갭 상승이 +5% 이상이면 **취소**. 신호는 그 가격에서 나온 것이 아니다(추격 매수).
  - 그 사이면 개장가로 체결한다.
  - 유효기간은 **1거래일**. 다음 개장에 처리하지 못하면 만료된다 — 며칠 지난 판단으로
    들어가면 그게 다시 look-ahead다.

체결분에는 `대기체결` 태그와 갭 구간 태그를 붙인다. 그래야 탭 A에서
**즉시 체결분과 대기 체결분의 성과를 나눠 볼 수 있다.** 이 장치가 실제로 도움이 됐는지는
그 비교로만 알 수 있고, 지금은 모른다.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("pending_orders")

MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)

# 재평가 임계 (가설). 환경변수로 덮어쓸 수 있다.
GAP_DOWN_CANCEL = float(os.getenv("PENDING_GAP_DOWN_CANCEL_PCT", "-7.0"))
GAP_UP_CANCEL = float(os.getenv("PENDING_GAP_UP_CANCEL_PCT", "5.0"))
MAX_AGE_DAYS = int(os.getenv("PENDING_MAX_AGE_DAYS", "4"))   # 연휴를 감안한 달력일 상한

STATE_NAME = "pending_orders.json"
TAG_FILLED = "대기체결"


# ─── 시간 판정 (순수) ────────────────────────────────


def is_market_hours(dt: datetime) -> bool:
    """평일 09:00~15:30 인가. 임시 공휴일은 알 수 없다 — 그날은 시세가 없어
    재평가에서 걸러지므로 여기서 추측하지 않는다."""
    if dt.weekday() >= 5:
        return False
    return MARKET_OPEN <= dt.time() <= MARKET_CLOSE


# ─── 주문 만들기 (순수) ──────────────────────────────


def make_order(*, slot: str, ticker: str, name: str, signal_price: float,
               alloc: float, signal_at: datetime, source: str,
               note: str = "", tags: Optional[list] = None) -> dict:
    """대기 주문 1건(순수).

    수량이 아니라 **배정 금액**을 들고 간다. 개장가가 달라지면 살 수 있는 수량도
    달라지므로, 수량을 미리 고정하면 슬롯 배분이 어긋난다.
    """
    return {
        "slot": slot, "ticker": str(ticker), "name": name,
        "signal_price": float(signal_price), "alloc": float(alloc),
        "signal_at": signal_at.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source, "note": note, "tags": list(tags or []),
    }


def _signal_date(order: dict) -> Optional[date]:
    try:
        return datetime.strptime(order["signal_at"][:10], "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return None


def is_expired(order: dict, today: date, max_age_days: int = MAX_AGE_DAYS) -> bool:
    """며칠 지난 판단으로 들어가지 않는다. 날짜를 모르면 만료로 본다."""
    d = _signal_date(order)
    if d is None:
        return True
    return (today - d).days > max_age_days


# ─── 재평가 (순수) ───────────────────────────────────


def gap_pct(signal_price: float, open_price: float) -> Optional[float]:
    if not signal_price or signal_price <= 0 or not open_price or open_price <= 0:
        return None
    return round((open_price / signal_price - 1) * 100, 2)


def gap_bucket(gap: Optional[float]) -> Optional[str]:
    """갭 구간 태그. 체결분을 갭 방향별로 나눠 볼 수 있게 한다."""
    if gap is None:
        return None
    if gap <= -3:
        return "갭하락"
    if gap >= 3:
        return "갭상승"
    return "갭보합"


def revalidate(order: dict, price: Optional[float], today: date, *,
               gap_down_cancel: float = GAP_DOWN_CANCEL,
               gap_up_cancel: float = GAP_UP_CANCEL,
               max_age_days: int = MAX_AGE_DAYS) -> dict:
    """대기 주문 1건 재평가(순수).

    반환: {order, verdict: fill|cancel, reason, gap, price, qty, tags}
    """
    def out(verdict, reason, gap=None, qty=0):
        return {"order": order, "verdict": verdict, "reason": reason,
                "gap": gap, "price": price, "qty": qty,
                "ticker": order.get("ticker"), "name": order.get("name"),
                "slot": order.get("slot"),
                "tags": order.get("tags", []) + [TAG_FILLED] + (
                    [gap_bucket(gap)] if (verdict == "fill" and gap_bucket(gap)) else [])}

    if is_expired(order, today, max_age_days):
        return out("cancel", f"신호가 {max_age_days}일을 넘겨 만료")
    if not price or price <= 0:
        return out("cancel", "개장 시세를 얻지 못함")

    gap = gap_pct(order.get("signal_price", 0), price)
    if gap is None:
        return out("cancel", "신호가를 알 수 없어 괴리를 판정 못 함")
    if gap <= -abs(gap_down_cancel):
        return out("cancel", f"갭 하락 {gap}% — 손절선({-abs(gap_down_cancel)}%) 이하", gap)
    if gap >= abs(gap_up_cancel):
        return out("cancel", f"갭 상승 {gap}% — 추격 매수 금지선({abs(gap_up_cancel)}%) 이상", gap)

    qty = int(float(order.get("alloc", 0)) // price)
    if qty < 1:
        return out("cancel", f"개장가 {price:,.0f}원 기준 배정금액 부족", gap)
    return out("fill", f"갭 {gap:+.2f}% — 체결", gap, qty)


def revalidate_all(orders: Iterable[dict], prices: dict, today: date,
                   **kw) -> list[dict]:
    """prices: {ticker: 현재가}. 없는 종목은 시세 실패로 취소된다."""
    return [revalidate(o, prices.get(o.get("ticker")), today, **kw) for o in orders]


# ─── 표시 (순수) ─────────────────────────────────────


def format_queued(orders: list[dict]) -> str:
    if not orders:
        return ""
    lines = [f"⏳ *장외 신호 {len(orders)}건 — 대기 큐에 넣었습니다*"]
    for o in orders:
        lines.append(f"• {o['name']}({o['ticker']}) 신호가 {o['signal_price']:,.0f}원")
    lines.append("")
    lines.append("_지금 기록한 가격으로는 실제로 살 수 없습니다. "
                 "다음 개장 후 첫 감시 사이클에서 그때 가격으로 다시 판단해 체결합니다._")
    return "\n".join(lines)


def format_results(results: list[dict]) -> str:
    if not results:
        return ""
    filled = [r for r in results if r["verdict"] == "fill"]
    cancelled = [r for r in results if r["verdict"] != "fill"]
    lines = ["📥 *대기 주문 처리*"]
    for r in filled:
        lines.append(f"• ✅ {r['name']} {r['qty']}주 @{r['price']:,.0f}원 — {r['reason']}")
    for r in cancelled:
        lines.append(f"• ⛔ {r['name']} 취소 — {r['reason']}")
    return "\n".join(lines)


# ─── 경계 (I-O) ──────────────────────────────────────


def default_state_path() -> Path:
    try:
        from storage_paths import PATHS
        return Path(PATHS.private_state_file(STATE_NAME))
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent / "data" / "private" / "state" / STATE_NAME


def load(path=None) -> list[dict]:
    p = Path(path) if path else default_state_path()
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    except Exception:  # noqa: BLE001
        return []


def save(orders: list[dict], path=None) -> None:
    p = Path(path) if path else default_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f".tmp_{p.name}"
        tmp.write_text(json.dumps(orders, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)                       # 원자적 교체 — 중간 상태가 남지 않는다
    except Exception as e:  # noqa: BLE001
        log.warning("대기 큐 저장 실패: %s", e)


def enqueue(orders: list[dict], path=None) -> list[dict]:
    """큐에 추가. 같은 (슬롯, 종목)이 이미 있으면 새 신호로 교체한다 —
    같은 종목을 두 번 사는 것보다 최신 판단 하나가 낫다."""
    if not orders:
        return load(path)
    existing = load(path)
    keyed = {(o.get("slot"), o.get("ticker")): o for o in existing}
    for o in orders:
        keyed[(o.get("slot"), o.get("ticker"))] = o
    merged = list(keyed.values())
    save(merged, path)
    return merged


def clear(path=None) -> None:
    save([], path)
