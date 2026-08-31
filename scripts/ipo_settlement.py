"""ipo_settlement.py — IPO 청약 → 배정 → 상장일 정산 (순수 코어)

**왜 필요한가.** IPO 슬롯 2,000만원이 개설(2026-05-10) 이후 한 번도 자본이
움직이지 않았다. `ipo_records`는 0행이고, 설령 구독 버튼을 눌러도 지금 코드는
`ipo_upsert(subscribed=True, alloc_amount=...)`로 **기록만 남기고 매매는 하지
않는다.** 그래서 성과 집계(라운드트립)에도 잡히지 않고 투입률은 영원히 0%다.

이 모듈이 채우는 칸: **배정수량 추정**과 **상장일 매도가 결정**.

━━━ 배정수량은 우리 데이터로 계산할 수 없다 ━━━

**첫 구현에서 틀렸다.** 기관 수요예측 경쟁률로 비례배정을 계산했는데, 두 군데가
잘못이었다.

  1. 비례배정에 쓰는 것은 **일반청약 경쟁률**이지 기관 수요예측 경쟁률이 아니다.
     우리가 파싱하는 것은 후자다.
  2. 개인 배정은 사실상 전부 **균등배정**에서 나온다. 비례분은 소수점 이하다.

     청약 500만원 · 공모가 16,000원 → 312주 청약
     경쟁률 1200:1 → 비례배정 **0.13주**   (300:1이어도 0.52주)

균등배정 = 균등물량 ÷ **청약 건수**인데, 청약 건수는 청약이 끝난 뒤에야 나오고
우리는 수집하지 않는다. 일반청약 경쟁률도 마찬가지다.

**그래서 이 모듈은 배정수량을 스스로 만들어 내지 않는다.** 호출부가 가정을
명시적으로 넘겨야 하고(`assumed_qty`), 그 가정이 기록에 그대로 남는다.
가정 없이 부르면 `None`이 나온다 — 모르는 값을 그럴듯한 숫자로 채우면 그 가정이
성과가 되어 돌아온다.

━━━ 매도 시점 ━━━

`wiki/투자/IPO_매도전략.md`의 `현재 전략:` 한 줄이 기준이다(A/B/C). 파일이
없거나 값이 이상하면 **A(가장 보수적)** 로 떨어진다.

    A  상장 당일 시초가 매도  → 측정 기준: 상장 당일 종가
    B  상장 당일 고가 추적    → 측정 기준: 상장 당일 고가
    C  3~5일 모멘텀 추적      → 측정 기준: 상장 후 5일 종가
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("ipo_settlement")

# 균등:비례 = 50:50 (2021년 개편 이후 일반청약자 배정 방식)
PROPORTIONAL_SHARE = 0.50

DEFAULT_STRATEGY = "A"
VALID_STRATEGIES = ("A", "B", "C")

# 전략별 측정 기준 — wiki 표와 같은 내용을 코드에도 둔다(파일을 못 읽어도 동작).
STRATEGY_BASIS = {
    "A": ("상장 당일 시초가 매도", "close"),
    "B": ("상장 당일 고가 추적 매도", "high"),
    "C": ("3~5일 모멘텀 추적 매도", "close_d5"),
}

WIKI_RELATIVE = Path("wiki") / "투자" / "IPO_매도전략.md"


# ─── 매도 전략 (순수 파싱 + 얇은 경계) ───────────────


def parse_strategy(text: str) -> Optional[str]:
    """'현재 전략: A' 한 줄에서 전략을 읽는다(순수). 못 읽으면 None."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("현재 전략:"):
            continue
        value = line.split(":", 1)[1].strip().upper()
        # "A (보수적)" 같은 표기도 첫 글자로 받는다
        value = value[:1] if value else ""
        if value in VALID_STRATEGIES:
            return value
        log.warning("IPO 매도전략 값이 이상함: %r — 기본값으로 진행", line)
        return None
    return None


def current_strategy(vault_root=None) -> str:
    """wiki에서 현재 전략을 읽는다. 실패하면 A(가장 보수적).

    **못 읽었다고 매매를 멈추지 않는다.** 전략은 '언제 파는가'이고, 못 읽으면
    가장 보수적인 A로 파는 것이 안전하다. 다만 그 사실을 로그에 남긴다.
    """
    try:
        root = Path(vault_root) if vault_root else _default_vault()
        text = (Path(root) / WIKI_RELATIVE).read_text(encoding="utf-8")
    except (OSError, TypeError) as e:
        log.warning("IPO 매도전략 파일을 읽지 못함 — %s로 진행: %s",
                    DEFAULT_STRATEGY, e)
        return DEFAULT_STRATEGY
    picked = parse_strategy(text)
    if picked is None:
        return DEFAULT_STRATEGY
    return picked


def _default_vault() -> Path:
    import os
    env = os.getenv("VAULT_PATH")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "obsidian-vault"


def strategy_basis(strategy: str) -> dict:
    """전략 → {label, price_field}. 모르는 값이면 A로 떨어진다(순수)."""
    label, field = STRATEGY_BASIS.get(
        str(strategy or "").upper(), STRATEGY_BASIS[DEFAULT_STRATEGY])
    return {"strategy": str(strategy or DEFAULT_STRATEGY).upper(),
            "label": label, "price_field": field}


# ─── 배정 추정 (순수) ────────────────────────────────


def subscription_shares(amount: float, final_price: Optional[float]) -> Optional[int]:
    """청약금액으로 몇 주를 청약하는가(순수). 확정공모가가 없으면 None.

    **확정 공모가 없이는 청약 자체가 성립하지 않는다.** 밴드 상단으로 대신
    계산하면 없는 사실이 생긴다(2026-08-29에 밴드 하단을 확정가로 오인해
    등급이 뒤집힌 적이 있다).
    """
    if not final_price or final_price <= 0:
        return None
    if not amount or amount <= 0:
        return 0
    return int(float(amount) // float(final_price))


def estimate_allocation(sub_shares: Optional[int],
                        *, assumed_qty: Optional[int] = None) -> dict:
    """배정수량(순수). **가정을 받아야만 숫자가 나온다.**

    반환: {qty, assumed, basis}

    경쟁률로 계산하지 않는다 — 위 모듈 설명 참조. 비례배정에 필요한 일반청약
    경쟁률도, 균등배정에 필요한 청약 건수도 우리에게 없다.
    """
    if not sub_shares or sub_shares <= 0:
        return {"qty": None, "assumed": False,
                "basis": "청약주수를 계산할 수 없음(확정 공모가 미상)"}
    if assumed_qty is None:
        return {"qty": None, "assumed": False,
                "basis": ("배정수량을 계산할 수 없음 — 일반청약 경쟁률과 청약 "
                          "건수를 수집하지 않는다. 가정을 명시해야 한다")}
    qty = int(assumed_qty)
    if qty <= 0:
        return {"qty": 0, "assumed": True, "basis": f"배정 {qty}주로 가정"}
    return {"qty": min(qty, int(sub_shares)), "assumed": True,
            "basis": f"**가정** 배정 {qty:,}주 (청약 {sub_shares:,}주 중)"}


def plan_subscription(*, alloc_amount: float, final_price: Optional[float],
                      grade: Optional[str] = None,
                      min_grade: Optional[set] = None,
                      assumed_qty: Optional[int] = None) -> dict:
    """청약 1건의 판단(순수).

    반환: {action: subscribe|skip, sub_shares, alloc, reason}

    `assumed_qty`가 없으면 **청약 판단까지만** 하고 배정은 비워 둔다.
    성과 기록은 배정이 정해져야 가능하다.
    """
    if min_grade and (grade or "") not in min_grade:
        return {"action": "skip", "sub_shares": 0, "alloc": 0.0,
                "reason": f"등급 {grade or '?'} — 기준 미달"}
    shares = subscription_shares(alloc_amount, final_price)
    if shares is None:
        return {"action": "skip", "sub_shares": 0, "alloc": 0.0,
                "reason": "확정 공모가 미상 — 청약 금액을 주수로 바꿀 수 없음"}
    if shares <= 0:
        return {"action": "skip", "sub_shares": 0, "alloc": 0.0,
                "reason": f"배정금액 {alloc_amount:,.0f}원으로 1주도 청약 불가"}
    est = estimate_allocation(shares, assumed_qty=assumed_qty)
    return {"action": "subscribe", "sub_shares": shares,
            "alloc": float(shares) * float(final_price),
            "est_qty": est["qty"], "est_basis": est["basis"],
            "reason": f"청약 {shares:,}주 · {est['basis']}"}


def settle(*, est_qty: int, final_price: float, exit_price: Optional[float],
           strategy: str = DEFAULT_STRATEGY) -> dict:
    """상장일 정산(순수). 공모가 매수 → 전략 기준가 매도.

    반환: {ok, qty, buy_price, sell_price, ret_pct, pnl, reason}

    **상장일 시세를 모르면 정산하지 않는다.** 공모가로 청산하면 수익 0%인
    가짜 라운드트립이 기록된다(콴텍 퇴출청산에서 같은 실수를 막아 둔 적이 있다).
    """
    basis = strategy_basis(strategy)
    if not est_qty or est_qty <= 0:
        return {"ok": False, "reason": "배정수량 없음"}
    if not final_price or final_price <= 0:
        return {"ok": False, "reason": "확정 공모가 없음"}
    if not exit_price or exit_price <= 0:
        return {"ok": False,
                "reason": (f"상장일 {basis['label']} 기준가 미확보 — "
                           f"공모가로 대체하지 않는다")}
    pnl = (float(exit_price) - float(final_price)) * int(est_qty)
    return {
        "ok": True, "qty": int(est_qty),
        "buy_price": float(final_price), "sell_price": float(exit_price),
        "ret_pct": round((exit_price / final_price - 1) * 100, 2),
        "pnl": pnl, "strategy": basis["strategy"], "label": basis["label"],
        "reason": f"{basis['label']} 기준 {exit_price:,.0f}원",
    }


def format_plan(name: str, plan: dict) -> str:
    """청약 판단 한 줄(순수)."""
    mark = "📥" if plan["action"] == "subscribe" else "⏭"
    return f"{mark} {name} — {plan['reason']}"


# ─── 상장일 결과 정산 (순수) ─────────────────────────
#
# **지금까지 아무것도 `ipo_close`를 부르지 않았다.** 상장일이 지나도 수익률이
# 채워지지 않으니, 매력지수 등급이 맞았는지 영원히 알 수 없었다.
#
# **이 정산에는 배정수량이 필요 없다.** 등급 검증이 묻는 것은 "등급이 높은
# 종목이 상장일에 더 올랐는가"이고, 그건 수익률만 있으면 답이 나온다.
# 배정을 몰라도 할 수 있는 일을 배정 때문에 미룰 이유가 없다.

# 전략 C는 상장 후 5거래일 종가가 기준이다 — 그만큼 기다려야 정산할 수 있다.
STRATEGY_WAIT_DAYS = {"A": 0, "B": 0, "C": 5}


def wait_days(strategy: str) -> int:
    return STRATEGY_WAIT_DAYS.get(str(strategy or "").upper(), 0)


def due_for_close(records, today: str, strategy: str = DEFAULT_STRATEGY,
                  calendar=None) -> list[dict]:
    """정산할 때가 된 기록들(순수).

    조건: 상장일이 있고 · 이미 수익률이 채워지지 않았고 · 기준일이 지났다.
    전략 C는 상장 후 5거래일이 지나야 한다(달력을 주면 거래일로 센다).
    """
    need = wait_days(strategy)
    out = []
    for rec in records or []:
        listing = str((rec or {}).get("listing_date") or "")
        if len(listing) != 8 or not listing.isdigit():
            continue
        if (rec or {}).get("return_pct") is not None:
            continue
        if not _passed(listing, today, need, calendar):
            continue
        out.append(rec)
    return out


def _passed(listing: str, today: str, need: int, calendar) -> bool:
    if need <= 0:
        return today >= listing
    if calendar:
        days = [d for d in calendar if d >= listing]
        if len(days) <= need:
            return False
        return today >= days[need]
    # 달력이 없으면 넉넉히 잡는다 — 이르게 정산하느니 늦게 하는 편이 낫다
    return today >= _shift_days(listing, need * 2)


def _shift_days(yyyymmdd: str, days: int) -> str:
    from datetime import date, timedelta
    try:
        d = date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:]))
    except ValueError:
        return yyyymmdd
    return (d + timedelta(days=days)).strftime("%Y%m%d")


def exit_price_from_ohlcv(payload: dict, listing_date: str,
                          strategy: str = DEFAULT_STRATEGY,
                          *, wait: Optional[int] = None) -> dict:
    """일봉 캐시에서 전략별 기준가(순수).

    반환: {price, basis, day}

    **없으면 None을 돌려준다.** 상장일 시세를 모르는데 공모가로 대체하면
    수익률 0%인 가짜 기록이 남는다.
    """
    basis = strategy_basis(strategy)
    field = basis["price_field"]
    dates = (payload or {}).get("date") or []
    if not dates:
        return {"price": None, "basis": basis["label"], "day": None}
    idx = next((i for i, d in enumerate(dates) if str(d) >= str(listing_date)), None)
    if idx is None:
        return {"price": None, "basis": basis["label"], "day": None}
    offset = wait if wait is not None else wait_days(strategy)
    target = idx + offset
    if target >= len(dates):
        return {"price": None, "basis": basis["label"], "day": None}
    series = (payload.get("high") if field == "high" else payload.get("close")) or []
    if target >= len(series):
        return {"price": None, "basis": basis["label"], "day": None}
    value = series[target]
    if not value or float(value) <= 0:
        return {"price": None, "basis": basis["label"], "day": str(dates[target])}
    return {"price": float(value), "basis": basis["label"],
            "day": str(dates[target])}


def format_close(name: str, result: dict) -> str:
    """정산 한 줄(순수)."""
    if not result.get("ok"):
        return f"⏳ {name} — {result.get('reason')}"
    return (f"🏁 {name} {result['ret_pct']:+.1f}% "
            f"(공모 {result['buy_price']:,.0f} → {result['sell_price']:,.0f}, "
            f"{result['label']})")
