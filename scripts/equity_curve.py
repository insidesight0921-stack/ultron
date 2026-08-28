"""equity_curve.py — 일간 마크투마켓 자산곡선 (탭 E 데이터 소스, v1)

**왜 필요한가**: 실전 전환 기준 6개 중 3개(샤프 1.0 / MDD −15% / 코스피 대비 알파)가
일간 수익률을 전제한다. 그런데 기존 성과 집계는 **완결 거래 순서**로 자산곡선을 만들고
있었다. 그러면 (a) 보유 중 평가손실이 통째로 빠져 MDD가 실제보다 얕게 나오고,
(b) 거래 1건을 하루로 보고 √252를 곱해 샤프가 몇 배로 부풀었다(콴텍 3.42 → 실제 −0.15).
이 모듈은 **달력 위의 하루**를 단위로 자산을 다시 센다.

날짜 정렬: 일봉 캐시에는 종가 배열만 있고 날짜가 없다. `price_sanity.load_series`가
KRX 거래일 달력(KOSPI 지수 캐시의 날짜열)로 역산해 붙이며, 거래가 대비 당일 종가
대조에서 119/120(99.2%) 일치했다. 정렬이 옳다는 것은 스테일 진입가가 과거 종가와
원 단위까지 맞아떨어진 사실이 거꾸로 증명한다.

**추정하지 않는다**: 보유 종목 중 하나라도 그날 종가를 모르면 그날 자산은 `None`이다.
직전 값으로 메우면 변동성이 줄어 샤프가 다시 부풀고, MDD는 얕아진다. 대신
**커버리지**를 함께 보고해 표본이 얼마나 성긴지 드러낸다.

**곡선은 기록된 그대로 만든다**: 데이터 품질 규칙(`data_quality`)이 집계에서 빼는
스테일 진입가 건도 곡선에는 들어 있다. 그 매수는 실제로 페이퍼 계좌의 현금을 줄였고,
없었던 일로 만들려면 없는 가격을 지어내야 하기 때문이다. 대신 제외 대상의 크기를
함께 돌려주어 화면이 그 사실을 말하게 한다.
"""
from __future__ import annotations

import logging
import math
from typing import Iterable, Optional

log = logging.getLogger("equity_curve")

TRADING_DAYS = 252
MIN_DAYS = 20          # 이 미만이면 어떤 비율 지표도 판정하지 않는다
MIN_COVERAGE = 0.7     # 가격이 붙은 날이 이 비율 미만이면 판정 보류


# ─── 일별 보유·현금 재구성 (순수) ────────────────────


def _day(value) -> str:
    return str(value or "")[:10].replace("-", "")


def replay(trades: Iterable[dict], calendar: list[str], seeds: dict) -> list[dict]:
    """거래 기록 → 거래일별 {date, slot: {cash, holdings}}(순수).

    첫 거래일부터 시작한다. 그 전 구간은 자산이 움직이지 않아 수익률 0인 날만 늘리고,
    그러면 변동성이 낮아져 샤프가 부풀어 오른다.
    """
    trades = sorted(trades, key=lambda t: (_day(t.get("executed_at")),
                                           str(t.get("executed_at") or ""),
                                           t.get("id") or 0))
    if not trades or not calendar:
        return []

    first = _day(trades[0].get("executed_at"))
    days = [d for d in calendar if d >= first]
    if not days:
        return []

    cash = {slot: float(seed) for slot, seed in seeds.items()}
    holdings: dict[str, dict[str, int]] = {slot: {} for slot in seeds}
    by_day: dict[str, list[dict]] = {}
    for t in trades:
        by_day.setdefault(_day(t.get("executed_at")), []).append(t)

    out = []
    for d in days:
        for t in by_day.get(d, []):
            slot = t.get("slot_name") or t.get("slot") or "?"
            if slot not in cash:
                cash[slot], holdings[slot] = 0.0, {}
            qty = int(t.get("quantity") or 0)
            price = float(t.get("price") or 0)
            fees = float(t.get("fees") or 0)
            if qty <= 0:
                continue
            ticker = str(t.get("ticker"))
            if t.get("side") == "buy":
                cash[slot] -= qty * price + fees
                holdings[slot][ticker] = holdings[slot].get(ticker, 0) + qty
            else:
                cash[slot] += qty * price - fees
                left = holdings[slot].get(ticker, 0) - qty
                if left > 0:
                    holdings[slot][ticker] = left
                else:
                    holdings[slot].pop(ticker, None)
        out.append({"date": d,
                    "slots": {s: {"cash": cash[s], "holdings": dict(holdings[s])}
                              for s in cash}})
    return out


# ─── 평가 (순수) ─────────────────────────────────────


def mark(state: dict, closes: dict) -> tuple[Optional[float], list[str]]:
    """한 슬롯의 하루 자산 = 현금 + 보유 평가액.

    가격을 모르는 종목이 하나라도 있으면 (None, [모르는 종목])을 돌려준다.
    직전 값으로 메우지 않는다 — 그러면 변동성이 줄어 샤프가 부풀고 MDD가 얕아진다.
    """
    missing = [t for t, q in state["holdings"].items() if q and not closes.get(t)]
    if missing:
        return None, missing
    value = state["cash"]
    for ticker, qty in state["holdings"].items():
        value += qty * float(closes.get(ticker) or 0)
    return value, []


def build(trades: Iterable[dict], calendar: list[str], seeds: dict,
          series_by_ticker: dict) -> dict:
    """일간 자산곡선(순수).

    series_by_ticker: {ticker: {YYYYMMDD: close}}
    반환: {days: [{date, total, slots:{...}}], coverage, missing_days, missing_tickers}
    """
    frames = replay(trades, calendar, seeds)
    days, missing_days = [], []
    missing_tickers: dict[str, int] = {}
    for frame in frames:
        closes = {}
        for state in frame["slots"].values():
            for ticker in state["holdings"]:
                if ticker not in closes:
                    closes[ticker] = (series_by_ticker.get(ticker) or {}).get(frame["date"])
        per_slot, ok = {}, True
        for slot, state in frame["slots"].items():
            value, missing = mark(state, closes)
            per_slot[slot] = value
            if missing:
                ok = False
                for t in missing:
                    missing_tickers[t] = missing_tickers.get(t, 0) + 1
        if ok:
            days.append({"date": frame["date"], "slots": per_slot,
                         "total": sum(v for v in per_slot.values() if v is not None)})
        else:
            missing_days.append(frame["date"])
    total_days = len(frames)
    return {
        "days": days,
        "n_days": len(days),
        "coverage": round(len(days) / total_days, 3) if total_days else None,
        "missing_days": missing_days,
        "missing_tickers": dict(sorted(missing_tickers.items(),
                                       key=lambda kv: -kv[1])[:10]),
    }


# ─── 지표 (순수) ─────────────────────────────────────


def daily_returns(values: list[float]) -> list[float]:
    out = []
    for prev, cur in zip(values, values[1:]):
        if prev and prev > 0:
            out.append(cur / prev - 1)
    return out


def max_drawdown(values: list[float]) -> Optional[float]:
    """일간 자산 기준 MDD(%). 보유 중 평가손실이 여기서는 반영된다."""
    if len(values) < 2:
        return None
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return round(worst * 100, 2)


def sharpe(returns: list[float], *, rf_annual: float = 0.0) -> Optional[float]:
    """연환산 샤프. **일간 수익률 기준이라야 √252가 의미를 가진다.**

    rf_annual을 0으로 두면 무위험수익률을 빼지 않은 값이다(그만큼 후하게 나온다).
    기본을 0으로 두되 값을 넘기면 차감한다 — 어느 쪽인지 화면에 밝힌다.
    """
    if len(returns) < 2:
        return None
    rf_daily = rf_annual / TRADING_DAYS
    excess = [r - rf_daily for r in returns]
    mean = sum(excess) / len(excess)
    var = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return None
    return round(mean / sd * math.sqrt(TRADING_DAYS), 2)


def total_return(values: list[float]) -> Optional[float]:
    if len(values) < 2 or not values[0]:
        return None
    return round((values[-1] / values[0] - 1) * 100, 2)


def beta_alpha(port: list[float], bench: list[float]) -> tuple[Optional[float], Optional[float]]:
    """일간 수익률 회귀로 베타와 젠센 알파(연환산 %). 길이가 다르면 짧은 쪽에 맞춘다."""
    n = min(len(port), len(bench))
    if n < MIN_DAYS:
        return None, None
    p, b = port[:n], bench[:n]
    mp, mb = sum(p) / n, sum(b) / n
    var_b = sum((x - mb) ** 2 for x in b) / (n - 1)
    if var_b <= 0:
        return None, None
    cov = sum((x - mp) * (y - mb) for x, y in zip(p, b)) / (n - 1)
    beta = cov / var_b
    alpha_daily = mp - beta * mb
    return round(beta, 2), round(alpha_daily * TRADING_DAYS * 100, 2)


def align(days: list[dict], bench: dict) -> tuple[list[float], list[float], list[str]]:
    """자산곡선과 벤치마크를 **같은 날짜**로 맞춘다(순수).

    날짜를 맞추지 않고 각자 수익률을 내면 휴장·결측 구간에서 서로 다른 날을 비교하게 된다.
    """
    dates = [d["date"] for d in days if bench.get(d["date"])]
    port = [d["total"] for d in days if bench.get(d["date"])]
    marks = [float(bench[d]) for d in dates]
    return port, marks, dates


def evaluate(curve: dict, bench: dict, *, rf_annual: float = 0.0,
             min_days: int = MIN_DAYS,
             min_coverage: float = MIN_COVERAGE) -> dict:
    """자산곡선 + 벤치마크 → 탭 E 지표(순수)."""
    port, marks, dates = align(curve["days"], bench)
    pr, br = daily_returns(port), daily_returns(marks)
    beta, alpha = beta_alpha(pr, br)
    coverage = curve.get("coverage")

    reasons = []
    if len(port) < min_days:
        reasons.append(f"가격이 붙은 거래일 {len(port)}일 — {min_days}일 미만")
    if coverage is not None and coverage < min_coverage:
        reasons.append(f"커버리지 {coverage:.0%} — {min_coverage:.0%} 미만")

    port_ret = total_return(port)
    bench_ret = total_return(marks)
    return {
        "n_days": len(port),
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "coverage": coverage,
        "missing_days": len(curve.get("missing_days") or []),
        "missing_tickers": curve.get("missing_tickers") or {},
        "port_return": port_ret,
        "bench_return": bench_ret,
        "excess_return": (None if (port_ret is None or bench_ret is None)
                          else round(port_ret - bench_ret, 2)),
        "mdd": max_drawdown(port),
        "bench_mdd": max_drawdown(marks),
        "sharpe": sharpe(pr, rf_annual=rf_annual),
        "bench_sharpe": sharpe(br, rf_annual=rf_annual),
        "beta": beta,
        "alpha": alpha,
        "rf_annual": rf_annual,
        "usable": not reasons,
        "reasons": reasons,
        "series": [{"date": d, "port": p, "bench": m}
                   for d, p, m in zip(dates, port, marks)],
    }


def verdict(ev: dict) -> list[dict]:
    """실전 전환 기준 대조(순수). 판정 불가면 그렇다고 말한다.

    계획서의 "코스피 대비 알파 양수 + 3%p 이상"은 두 조건으로 나눠 **둘 다** 본다.
      - 단순 초과수익 ≥ +3%p — 총액으로 시장을 앞섰는가
      - 젠센 알파 > 0 — 감수한 위험 대비로도 앞섰는가
    둘은 반대 방향을 가리킬 수 있다. 현금 비중이 높으면 하락장에서 초과수익은
    저절로 양수가 되지만(베타가 낮아서), 위험 대비로는 뒤질 수 있다.
    한쪽만 보면 그 착시를 통과시키게 된다.
    """
    checks = [
        ("샤프 비율", ev.get("sharpe"), 1.0, "이상"),
        ("MDD", ev.get("mdd"), 15.0, "이내"),
        ("코스피 대비 초과수익", ev.get("excess_return"), 3.0, "이상"),
        ("젠센 알파(연환산)", ev.get("alpha"), 0.0, "초과"),
    ]
    out = []
    for name, value, threshold, direction in checks:
        if not ev.get("usable") or value is None:
            passed = None
        elif direction == "이내":
            passed = value <= threshold
        elif direction == "초과":
            passed = value > threshold
        else:
            passed = value >= threshold
        out.append({"name": name, "value": value, "threshold": threshold,
                    "direction": direction, "passed": passed})
    return out


def format_report(ev: dict) -> str:
    if not ev["n_days"]:
        return "📈 일간 자산곡선: 평가할 거래일이 없습니다."
    lines = [f"📈 일간 자산곡선 {ev['start']}~{ev['end']} · {ev['n_days']}거래일 "
             f"(커버리지 {ev['coverage']:.0%})"]
    if not ev["usable"]:
        lines.append("⚠️ 판정 보류: " + " / ".join(ev["reasons"]))
    fmt = lambda v, u="%": "—" if v is None else f"{v}{u}"
    lines += [
        f"  수익률   {fmt(ev['port_return'])}  (코스피 {fmt(ev['bench_return'])}, "
        f"초과 {fmt(ev['excess_return'], '%p')})",
        f"  MDD      {fmt(ev['mdd'])}  (코스피 {fmt(ev['bench_mdd'])})",
        f"  샤프     {fmt(ev['sharpe'], '')}  (코스피 {fmt(ev['bench_sharpe'], '')})"
        f"{'' if ev['rf_annual'] else ' · 무위험수익률 미차감'}",
        f"  베타     {fmt(ev['beta'], '')}   알파(연환산) {fmt(ev['alpha'])}",
    ]
    if ev["missing_days"]:
        lines.append(f"  ※ 가격을 모르는 종목이 있어 건너뛴 날 {ev['missing_days']}일 "
                     f"— 직전 값으로 메우지 않았습니다.")
    for name, v in list((ev.get("missing_tickers") or {}).items())[:3]:
        lines.append(f"     {name}: {v}일 결측")
    return "\n".join(lines)


# ─── 경계 (I-O) ──────────────────────────────────────


def load_inputs(db_path=None) -> tuple[list[dict], list[str], dict, dict, dict]:
    """(거래, 거래일 달력, 슬롯 시드, 종목 일봉, 벤치마크 일봉)."""
    import json

    import paper_db
    import price_sanity as ps

    kwargs = {"db_path": db_path} if db_path else {}
    trades = paper_db.list_trades(limit=100000, **kwargs)
    slots = paper_db.list_slots(**kwargs)
    calendar = ps.trading_calendar()

    seed_total = 0.0
    try:
        import sqlite3
        con = sqlite3.connect(str(db_path or paper_db.DEFAULT_DB_PATH))
        row = con.execute("SELECT seed_capital FROM portfolios LIMIT 1").fetchone()
        seed_total = float(row[0]) if row else 0.0
    except Exception as e:  # noqa: BLE001
        log.warning("시드 자본 조회 실패: %s", e)
    seeds = {s["name"]: float(s.get("allocation_pct") or 0) * seed_total for s in slots}

    tickers = {str(t.get("ticker")) for t in trades}
    series = {t: dict(ps.load_series(t, calendar)) for t in tickers}

    bench: dict = {}
    try:
        files = sorted((ps._cache_root() / "indices").glob("market_index_KOSPI_*.json"))
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        bench = dict(zip(payload["series"]["date"], payload["series"]["close"]))
    except Exception as e:  # noqa: BLE001
        log.warning("벤치마크 로드 실패: %s", e)
    return trades, calendar, seeds, series, bench


def report(db_path=None, *, rf_annual: float = 0.0) -> dict:
    trades, calendar, seeds, series, bench = load_inputs(db_path)
    curve = build(trades, calendar, seeds, series)
    return evaluate(curve, bench, rf_annual=rf_annual)


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="일간 자산곡선 · 벤치마크 비교 (탭 E)")
    ap.add_argument("--db")
    ap.add_argument("--rf", type=float, default=0.0, help="무위험수익률(연, 0.03 = 3%%)")
    args = ap.parse_args()

    ev = report(args.db, rf_annual=args.rf)
    print(format_report(ev))
    print()
    print("실전 전환 기준 대조")
    for c in verdict(ev):
        mark_ = "판정불가" if c["passed"] is None else ("✅ 충족" if c["passed"] else "❌ 미충족")
        value = "—" if c["value"] is None else c["value"]
        print(f"  {c['name']:16s} {value!s:>8}  (기준 {c['threshold']} {c['direction']})  {mark_}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
