"""paper_view.py — 화면에 내보낼 평가·주석 계산 (순수, v1 2026-10-27).

**왜 필요한가.** 2026-10-27 화면 점검에서 숫자가 잘못 읽히고 있었다.
  - 슬롯 카드의 `current_capital`은 **현금**이다(매수 때 빠지고 매도 때 들어온다).
    키움이 8종목 1,634만원어치를 들고 있는데 카드엔 현금 1,655만원만 보였고,
    배분 막대는 현금 비중을 목표와 비교해 "키움 19.3% (목표 35%)"로 모자라 보였다.
  - 보유 포지션 평가금액이 매입가 그대로라 "지금 얼마 벌고 있나"가 없었다.
  - 매도 행에 종목명이 비어 코드만 남고, 그 거래의 손익이 보이지 않았다.

여기 함수는 시세 조회를 하지 않는다 — 가격은 호출하는 쪽(paper_ui)이 넘긴다.
청산선은 봇(`telegram_bot.intraday_monitor_job`)과 같은 `exit_rules` 상수·피크 키를 쓴다.
화면이 봇과 다른 손절선을 보여주면 어느 쪽을 믿어야 할지 알 수 없다.
"""
from __future__ import annotations

from typing import Optional

import exit_rules


def exit_line(peak: Optional[float]) -> tuple[float, str]:
    """지금 아래쪽으로 걸리는 청산선(수익률)과 이름(순수).

    트레일링이 켜졌으면(피크 ≥ trail_arm) 피크 − trail_drop과 손절선 중 높은 쪽이다.
    """
    stop = exit_rules.STOP_PCT
    if peak is not None and peak >= exit_rules.TRAIL_ARM_PCT:
        trail = peak - exit_rules.TRAIL_DROP_PCT
        if trail > stop:
            return trail, "트레일링"
    return stop, "손절선"


def position_view(pos: dict, price: Optional[float], peak: Optional[float] = None) -> dict:
    """포지션 1개 → 평가 행(순수). 가격을 모르면 장부가로 두고 그렇다고 밝힌다."""
    qty = int(pos.get("quantity") or 0)
    avg = float(pos.get("avg_price") or 0)
    book = qty * avg
    out = {**pos, "book": round(book), "price": None, "value": round(book),
           "pnl": None, "pnl_pct": None, "exit_pct": None, "exit_kind": None, "room": None}
    if not price or avg <= 0:
        return out
    pnl_pct = (price - avg) / avg
    # 봇의 update_peak와 같게 — 기록이 없으면 지금이 피크, 있으면 둘 중 큰 쪽
    level, kind = exit_line(pnl_pct if peak is None else max(peak, pnl_pct))
    out.update({
        "price": price, "value": round(qty * price), "pnl": round(qty * (price - avg)),
        "pnl_pct": round(pnl_pct * 100, 2),
        "exit_pct": round(level * 100, 2), "exit_kind": kind,
        "room": round((pnl_pct - level) * 100, 2),        # 청산선까지 남은 %p
        "take_pct": round(exit_rules.TAKE_PCT * 100, 2),
    })
    return out


def slot_equity(slots: list[dict], rows: list[dict]) -> list[dict]:
    """슬롯별 현금·주식 평가·합계(순수). `rows`는 position_view 결과."""
    by_slot: dict[str, float] = {}
    for r in rows:
        by_slot[r.get("slot_name")] = by_slot.get(r.get("slot_name"), 0.0) + float(r["value"])
    out = []
    for s in slots:
        cash = float(s.get("current_capital") or 0)
        stock = by_slot.get(s.get("name"), 0.0)
        out.append({**s, "cash": round(cash), "stock": round(stock), "equity": round(cash + stock)})
    return out


def trade_key(slot, ticker, at) -> str:
    return f"{slot}|{ticker}|{at}"


def annotate_trades(trades: list[dict], roundtrips: list[dict], excluded: list[dict],
                    names: dict[str, str]) -> list[dict]:
    """거래 행에 종목명·매도 손익·품질 제외 사유를 붙인다(순수).

    매도 1건은 `compute_roundtrips`에서 라운드트립 1개가 된다(여러 매수 로트를 합친다).
    그래서 (슬롯, 종목, 매도 시각)으로 맞붙일 수 있다.
    """
    rt_by = {trade_key(r.get("slot"), r.get("ticker"), r.get("sell_at")): r for r in roundtrips}
    exc_by = {trade_key(r.get("slot"), r.get("ticker"), r.get("sell_at")): r for r in excluded}
    out = []
    for t in trades:
        row = dict(t)
        if not row.get("name"):
            row["name"] = names.get(row.get("ticker"), "")
        if row.get("side") == "sell":
            k = trade_key(row.get("slot_name"), row.get("ticker"), row.get("executed_at"))
            rt = rt_by.get(k)
            if rt is not None:
                row["rt_pnl"] = round(float(rt["pnl"]))
                row["rt_ret"] = round(float(rt["ret"]) * 100, 2)
            ex = exc_by.get(k)
            if ex is not None:
                row["excluded_reason"] = ex.get("excluded_reason")
        out.append(row)
    return out


def name_map(trades: list[dict]) -> dict[str, str]:
    """종목코드 → 이름(순수). 매도 기록엔 이름이 비어 있는 경우가 많아 매수 기록에서 찾는다."""
    out: dict[str, str] = {}
    for t in trades:
        if t.get("name") and t.get("ticker") not in out:
            out[t["ticker"]] = t["name"]
    return out


def win_rate(perf: list[dict]) -> Optional[float]:
    """슬롯별 승률·건수 → 전체 승률(순수). 슬롯마다 반올림된 승률을 다시 가중하면
    건수 표와 0.1%p 어긋난다(38.1% vs 38.2%) — 승 건수를 정수로 되돌려 합친다."""
    n = sum(int(s.get("n_closed") or 0) for s in perf)
    if not n:
        return None
    wins = sum(round(float(s["win_rate"]) / 100 * int(s.get("n_closed") or 0))
               for s in perf if s.get("win_rate") is not None)
    return round(wins / n * 100, 1)
