"""exit_backtest.py — 트레일링 파라미터 (arm, drop) 백테스트 (순수 코어 + pykrx CLI)

exit_rules 트레일링 기본값(arm +10%, drop 7%p)은 임의값 → 과거 라운드트립의
실제 가격 경로로 그리드서치해 근거를 만든다.

- 순수 코어: simulate_exit / grid_search — 가격 경로 주입, hermetic 테스트 대상.
- CLI(네트워크 불필요 — 로컬 일봉 캐시 사용):
    python3 scripts/exit_backtest.py

**2026-08-27 두 가지를 고쳤다.**

1. **표본에서 오염분을 뺀다.** 이전에는 `compute_roundtrips`를 그대로 써서
   진입가가 며칠 전 종가였던 배치(16건, 전체의 21%)가 섞여 있었다. 그중 8건은
   가짜 급락으로 22분 만에 청산된 건이라, 그리드서치가 "빨리 자르는 게 낫다"는
   쪽으로 기울 수 있었다. 실제로 돌아가는 파라미터의 근거이므로 그냥 둘 수 없다.
2. **pykrx 대신 로컬 일봉 캐시를 쓴다.** KRX 거래일 달력으로 날짜를 복원하는
   `price_sanity.load_series`가 이미 있고, 거래가 대조에서 119/120 일치했다.
   네트워크가 없어도 돌고, 결과가 실행 환경에 따라 달라지지 않는다.

한계: 일봉 종가 기준 근사(장중 5분 폴링보다 보수적), 표본이 작다.
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


def combo_key(row: dict) -> str:
    return ("baseline" if row["arm"] is None
            else f"arm{row['arm']:.2f}/drop{row['drop']:.2f}")


def result_matrix(paths: list[tuple[list[float], float]],
                  arms=DEFAULT_ARMS, drops=DEFAULT_DROPS, **kw) -> dict:
    """조합별 경로별 수익률을 한 번만 계산해 둔다(순수).

    부트스트랩은 이 표에서 부분합만 내면 되므로, 재표집마다 다시 시뮬레이션하지 않는다.
    """
    combos: list[tuple] = [(None, None)] + [(a, d) for a in arms for d in drops]
    return {(a, d): [simulate_exit(p, e, arm=a, drop=d, **kw)["ret"] for p, e in paths]
            for a, d in combos}


def split_halves(paths: list[tuple[list[float], float]], **kw) -> dict:
    """시간 순 전/후반 각각의 1위 조합(순수).

    한쪽에서만 좋은 조합은 그 구간에 맞춘 것이다. 표본이 작을수록 이 검사가 중요하다.
    **paths는 매수일 순서로 들어와야 한다.**
    """
    mid = len(paths) // 2
    if mid < 2:
        return {"usable": False, "reason": f"표본 {len(paths)}건 — 전/후반 분할 불가"}
    first = grid_search(paths[:mid], **kw)[0]
    second = grid_search(paths[mid:], **kw)[0]
    return {"usable": True, "n_first": mid, "n_second": len(paths) - mid,
            "first": first, "second": second,
            "agree": combo_key(first) == combo_key(second)}


def bootstrap_winners(paths: list[tuple[list[float], float]],
                      arms=DEFAULT_ARMS, drops=DEFAULT_DROPS,
                      *, rounds: int = 400, seed: int = 7, **kw) -> list[dict]:
    """재표집에서 각 조합이 1위를 차지한 비율(순수, 시드 고정).

    표본을 복원추출로 다시 뽑아 순위가 얼마나 흔들리는지 본다. 1위 비율이 낮으면
    그 조합이 이긴 것은 운일 가능성이 크다 — 표만 보면 그 사실이 보이지 않는다.
    """
    import random

    matrix = result_matrix(paths, arms, drops, **kw)
    combos = list(matrix)
    n = len(paths)
    if n < 2:
        return []
    rng = random.Random(seed)
    wins: dict[tuple, int] = {c: 0 for c in combos}
    for _ in range(rounds):
        idx = [rng.randrange(n) for _ in range(n)]
        best, best_total = None, None
        for c in combos:
            rets = matrix[c]
            total = sum(rets[i] for i in idx)
            if best_total is None or total > best_total:
                best, best_total = c, total
        wins[best] += 1
    out = [{"arm": a, "drop": d, "win_rate": round(w / rounds * 100, 1)}
           for (a, d), w in wins.items() if w]
    return sorted(out, key=lambda r: -r["win_rate"])


def format_stability(halves: dict, boot: list[dict], top: int = 5) -> str:
    """안정성 검사 결과(순수)."""
    lines = ["🔍 안정성 검사"]
    if not halves.get("usable"):
        lines.append(f"  전/후반 분할: {halves.get('reason')}")
    else:
        f, sec = halves["first"], halves["second"]
        lines.append(f"  전반 {halves['n_first']}건 1위: {combo_key(f)} "
                     f"(합계 {f['total_ret']*100:+.1f}%)")
        lines.append(f"  후반 {halves['n_second']}건 1위: {combo_key(sec)} "
                     f"(합계 {sec['total_ret']*100:+.1f}%)")
        lines.append("  → " + ("전후반이 같은 조합을 고름" if halves["agree"]
                               else "**전후반이 다른 조합을 고름 — 구간에 맞춘 값일 수 있다**"))
    if boot:
        lines.append(f"  재표집 1위 비율(상위 {top}):")
        for r in boot[:top]:
            tag = ("베이스라인" if r["arm"] is None
                   else f"arm{r['arm']*100:+.0f}% drop{r['drop']*100:.0f}%p")
            lines.append(f"    {tag}: {r['win_rate']}%")
        lines.append("  → 1위 비율이 낮으면 표에서 이긴 것은 운일 가능성이 크다.")
    return "\n".join(lines)


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


def _load_real_paths(db_path=None) -> tuple[list[tuple[list[float], float]], dict]:
    """paper.db 라운드트립 → ([(보유기간 종가 경로, 매수가)], 표본 요약).

    데이터 품질 규칙에 걸린 라운드트립은 뺀다 — 가짜 가격이 만든 청산 경로로
    파라미터를 고르면 그 파라미터도 가짜다.
    """
    import paper_db
    import price_sanity as ps
    import trade_analytics as ta

    kwargs = {"db_path": db_path} if db_path else {}
    kept, dropped = ta.roundtrips_for_analysis(
        paper_db.list_trades(limit=100000, **kwargs))

    calendar = ps.trading_calendar()
    series: dict = {}
    paths, no_price = [], 0
    for r in kept:
        if not (r.get("buy_at") and r.get("sell_at") and r.get("cost")):
            continue
        ticker = r["ticker"]
        if ticker not in series:
            series[ticker] = dict(ps.load_series(ticker, calendar))
        start = r["buy_at"][:10].replace("-", "")
        end = r["sell_at"][:10].replace("-", "")
        closes = [c for d, c in sorted(series[ticker].items())
                  if start <= d <= end and c]
        if not closes:
            no_price += 1
            continue
        # 진입가 = 실매수가(수수료 포함 cost/qty). qty 없으면 첫 종가 근사 폴백.
        qty = r.get("qty") or 0
        entry = (r["cost"] / qty) if qty else closes[0]
        paths.append((closes, entry))
    return paths, {"kept": len(kept), "excluded": len(dropped),
                   "no_price": no_price, "paths": len(paths)}


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="트레일링 파라미터 그리드서치")
    ap.add_argument("--db")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    paths, info = _load_real_paths(args.db)
    print(f"표본: 라운드트립 {info['kept']}건 "
          f"(품질 제외 {info['excluded']}건, 일봉 없음 {info['no_price']}건) "
          f"→ 경로 {info['paths']}건")
    if not paths:
        print("평가할 경로가 없습니다.")
        return 0
    rows = grid_search(paths)
    print(format_grid(rows, top=args.top))
    print()
    base = next(r for r in rows if r["arm"] is None)
    best = rows[0]
    import exit_rules as _er
    cur = next((r for r in rows
                if r["arm"] == _er.TRAIL_ARM_PCT and r["drop"] == _er.TRAIL_DROP_PCT), None)
    print(f"현행 설정 arm{_er.TRAIL_ARM_PCT*100:+.0f}% drop{_er.TRAIL_DROP_PCT*100:.0f}%p: "
          + (f"합계 {cur['total_ret']*100:+.1f}%" if cur else "그리드에 없음"))
    print(f"베이스라인(트레일링 없음): 합계 {base['total_ret']*100:+.1f}%")
    if best["arm"] is not None:
        print(f"최고 조합 arm{best['arm']*100:+.0f}% drop{best['drop']*100:.0f}%p: "
              f"합계 {best['total_ret']*100:+.1f}%")
    print()
    print(format_stability(split_halves(paths), bootstrap_winners(paths)))
    print()
    print("⚠️ 표본이 작고 일봉 근사다. 전후반이 갈리거나 재표집 1위 비율이 낮으면")
    print("   합계 순위가 높아도 파라미터를 바꾸지 말 것.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
