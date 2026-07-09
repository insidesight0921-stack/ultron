"""exit_backtest.py — 트레일링 파라미터 (arm, drop) 백테스트 (순수 코어 + pykrx CLI)

exit_rules 트레일링 기본값(arm +10%, drop 7%p)은 임의값 → 과거 라운드트립의
실제 가격 경로로 그리드서치해 근거를 만든다.

- 순수 코어: simulate_exit / grid_search — 가격 경로 주입, hermetic 테스트 대상.
- CLI(네트워크 필요, 실환경 전용):
    python3 scripts/exit_backtest.py            # paper.db 라운드트립 + pykrx 일봉
  샌드박스에서는 pykrx 차단으로 CLI 불가(테스트는 합성 경로로 수행).

한계: 일봉 종가 기준 근사(장중 5분 폴링보다 보수적), 과거 42건 표본.
"""
from __future__ import annotations

from typing import Optional

import exit_rules

DEFAULT_ARMS = (0.06, 0.08, 0.10, 0.12, 0.15)
DEFAULT_DROPS = (0.03, 0.05, 0.07, 0.10)


def simulate_exit(prices: list[float], entry: float, *,
                  arm: Optional[float], drop: Optional[float],
                  stop: float = exit_rules.STOP_PCT,
                  take: float = exit_rules.TAKE_PCT,
                  hard_stop: float = exit_rules.HARD_STOP_PCT) -> dict:
    """일별 종가 경로에 exit_rules 판정 적용(순수).

    arm/drop=None → 트레일링 비활성(현행 손절·익절만).
    반환: {ret, reason, day}  — 미청산 시 마지막 종가 수익률(reason="만기").
    """
    if not prices or entry <= 0:
        return {"ret": 0.0, "reason": "만기", "day": 0}
    peak = None
    for i, px in enumerate(prices):
        pnl = (px - entry) / entry
        peak = pnl if peak is None else max(peak, pnl)
        use_peak = peak if (arm is not None and drop is not None) else None
        verdict = exit_rules.should_exit(
            pnl, use_peak, stop=stop, take=take, hard_stop=hard_stop,
            trail_arm=(arm if arm is not None else 9e9),
            trail_drop=(drop if drop is not None else 9e9))
        if verdict:
            return {"ret": pnl, "reason": verdict[1], "day": i}
    return {"ret": (prices[-1] - entry) / entry, "reason": "만기",
            "day": len(prices) - 1}


def grid_search(paths: list[tuple[list[float], float]],
                arms=DEFAULT_ARMS, drops=DEFAULT_DROPS, **kw) -> list[dict]:
    """(가격경로, 진입가) 리스트 × (arm, drop) 그리드 → 합계 수익률 내림차순(순수).

    첫 행에 베이스라인(트레일링 없음) 포함. 각 행:
      {arm, drop, total_ret, avg_ret, n_trail, n_stop, n_take}
    """
    combos: list[tuple] = [(None, None)] + [(a, d) for a in arms for d in drops]
    out: list[dict] = []
    for arm, drop in combos:
        results = [simulate_exit(p, e, arm=arm, drop=drop, **kw)
                   for p, e in paths]
        n = len(results) or 1
        out.append({
            "arm": arm, "drop": drop,
            "total_ret": round(sum(r["ret"] for r in results), 4),
            "avg_ret": round(sum(r["ret"] for r in results) / n, 4),
            "n_trail": sum(1 for r in results if r["reason"] == "트레일링"),
            "n_stop": sum(1 for r in results
                          if r["reason"] in ("손절선", "급락")),
            "n_take": sum(1 for r in results if r["reason"] == "익절선"),
        })
    return sorted(out, key=lambda r: r["total_ret"], reverse=True)


def format_grid(rows: list[dict], top: int = 10) -> str:
    lines = ["📐 트레일링 그리드서치 (합계 수익률순, 일봉 근사)"]
    for r in rows[:top]:
        tag = ("베이스라인(현행)" if r["arm"] is None
               else f"arm{r['arm']*100:+.0f}% drop{r['drop']*100:.0f}%p")
        lines.append(
            f"  {tag}: 합계 {r['total_ret']*100:+.1f}% · 평균 {r['avg_ret']*100:+.2f}% "
            f"(트레일링 {r['n_trail']} · 손절 {r['n_stop']} · 익절 {r['n_take']})")
    return "\n".join(lines)


# ─── CLI (네트워크 필요 — 실환경 전용) ───────────────


def _load_real_paths() -> list[tuple[list[float], float]]:
    """paper.db 라운드트립 → (보유기간 pykrx 종가 경로, 매수가)."""
    import paper_db
    import trade_analytics as ta
    from pykrx import stock as stk

    rts = ta.compute_roundtrips(paper_db.list_trades(limit=100000))
    paths = []
    for r in rts:
        if not (r.get("buy_at") and r.get("sell_at") and r.get("cost")):
            continue
        start = r["buy_at"][:10].replace("-", "")
        end = r["sell_at"][:10].replace("-", "")
        try:
            df = stk.get_market_ohlcv(start, end, r["ticker"])
            closes = [float(x) for x in df["종가"].tolist()]
        except Exception:
            continue
        if closes:
            # 진입가 = 실매수가(수수료 포함 cost/qty). qty 없으면 첫 종가 근사 폴백.
            qty = r.get("qty") or 0
            entry = (r["cost"] / qty) if qty else closes[0]
            paths.append((closes, entry))
    return paths


if __name__ == "__main__":
    paths = _load_real_paths()
    print(f"경로 {len(paths)}건 로드")
    print(format_grid(grid_search(paths), top=12))
