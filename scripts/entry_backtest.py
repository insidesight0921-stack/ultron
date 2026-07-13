"""entry_backtest.py — 진입 조건 발굴: 이벤트 스터디 (순수 코어 + pykrx CLI)

'나만의 퀀트' = 당신의 청산 구조(-7% 손절 · +20% 익절 · -12% 하드스톱 · 트레일링,
5분 기계 집행)를 **고정**한 채, 그 위에서만 통하는 진입 조건을 찾는다.
같은 신호도 청산 구조가 다르면 기대값이 다르다 — 이 검증 루프 자체가 비대중적 엣지.

- 순수 코어: compute_features / CONDITIONS / event_study / split_halves — hermetic 테스트.
- CLI(실환경, pykrx 필요):
    python3 scripts/entry_backtest.py            # KOSPI200 × 3년 조건별 이벤트 스터디
    python3 scripts/entry_backtest.py --tag      # 실거래 42건 진입 시점 피처 태깅
    python3 scripts/entry_backtest.py --years 5 --market 1028
- 과최적화 방지: 기간 전/후반 분할 성과를 나란히 표시(한쪽만 좋으면 의심),
  이벤트 중첩 방지(청산 전 재진입 금지), 조건은 사전 정의된 해석 가능 세트만.

한계: 일봉 종가 근사(장중 5분 집행보다 보수적), 거래비용 미반영(왕복 ~0.3%p 차감해 볼 것).
※ 통계 관찰 도구이며 투자 권유가 아님.
"""
from __future__ import annotations

from typing import Callable, Optional

import exit_backtest
import exit_rules

MIN_HISTORY = 60          # 피처 계산에 필요한 최소 봉 수
DEFAULT_HORIZON = 20      # 진입 후 최대 관찰 봉 수(≈ 1개월) — 만기 시 종가 청산
DEFAULT_ARM = exit_rules.TRAIL_ARM_PCT
DEFAULT_DROP = exit_rules.TRAIL_DROP_PCT


# ─── 피처 (순수) ─────────────────────────────────────


def compute_features(closes: list[float], i: int,
                     volumes: Optional[list[float]] = None) -> Optional[dict]:
    """i번째 봉 종가 기준 기술적 상태. 이력 부족(i<MIN_HISTORY)이면 None."""
    if i < MIN_HISTORY or i >= len(closes) or closes[i] <= 0:
        return None
    px = closes[i]
    ma20 = sum(closes[i - 19:i + 1]) / 20
    ma60 = sum(closes[i - 59:i + 1]) / 60
    hi20 = max(closes[i - 19:i + 1])
    f = {
        "ma20_gap": px / ma20 - 1,          # 20일선 대비 이격
        "ma60_gap": px / ma60 - 1,          # 60일선 대비 이격(장기 추세)
        "dd20": px / hi20 - 1,              # 직전 20일 고점 대비 낙폭
        "ret5": px / closes[i - 5] - 1,     # 최근 5일 수익률
        "new_high20": px >= hi20,           # 20일 신고가
    }
    if volumes and len(volumes) == len(closes):
        v20 = volumes[i - 19:i + 1]
        mean = sum(v20) / 20
        var = sum((x - mean) ** 2 for x in v20) / 20
        f["vol_z"] = ((volumes[i] - mean) / (var ** 0.5)) if var > 0 else 0.0
    else:
        f["vol_z"] = None
    return f


# ─── 조건 세트 (사전 정의 — 사후 조작 금지) ──────────

CONDITIONS: dict[str, Callable[[dict], bool]] = {
    # 베이스라인: 아무 때나 진입(비교 기준)
    "베이스라인(무조건)": lambda f: True,
    # 장기 추세 위 + 단기 눌림(20일선 아래로 살짝)
    "추세위+눌림": lambda f: f["ma60_gap"] > 0 and -0.05 <= f["ma20_gap"] < 0,
    # 정배열 순추세(둘 다 위)
    "정배열": lambda f: f["ma20_gap"] > 0 and f["ma60_gap"] > 0,
    # 20일 신고가 돌파
    "신고가돌파": lambda f: bool(f["new_high20"]),
    # 과낙폭(-10%+) 후 5일 반등 시작
    "과낙폭반등": lambda f: f["dd20"] <= -0.10 and f["ret5"] > 0,
    # 떨어지는 칼(5일 -7%+) — 나쁠 것으로 예상되는 대조군
    "하락나이프": lambda f: f["ret5"] <= -0.07,
    # 추세 아래(60일선 밑) — 대조군
    "역추세": lambda f: f["ma60_gap"] < 0,
}


# 스캔용 진입 후보 조건(대조군·베이스라인 제외) — 마이퀀트 탭에서 사용
SCAN_CONDITIONS = ("추세위+눌림", "정배열", "신고가돌파", "과낙폭반등")


def scan_signals(series_list: list[dict],
                 conditions: tuple = SCAN_CONDITIONS) -> list[dict]:
    """각 종목의 최신 봉에서 진입 조건 평가(순수).

    series_list 항목: {"ticker", "name"?, "closes", "volumes"?}
    반환: 조건 1개 이상 충족 종목 [{ticker, name, price, matched, features}].
    """
    out = []
    for s in series_list:
        closes = s["closes"]
        f = compute_features(closes, len(closes) - 1, s.get("volumes"))
        if not f:
            continue
        matched = [c for c in conditions if CONDITIONS[c](f)]
        if matched:
            out.append({
                "ticker": s.get("ticker"), "name": s.get("name") or s.get("ticker"),
                "price": closes[-1], "matched": matched,
                "features": {k: (round(v, 4) if isinstance(v, float) else v)
                             for k, v in f.items()},
            })
    return sorted(out, key=lambda r: -len(r["matched"]))


def scan_current(market_code: str = "1028", days: int = 140,
                 cache_dir=None) -> list[dict]:
    """현재 시점 유니버스 스캔(실환경, pykrx). 당일 캐시 사용."""
    import json
    from datetime import datetime, timedelta
    from pathlib import Path
    from pykrx import stock as stk

    cdir = Path(cache_dir) if cache_dir else \
        Path(__file__).resolve().parent.parent / "data" / "cache"
    cpath = cdir / f"myquant_scan_{datetime.now().strftime('%Y%m%d')}.json"
    try:
        return json.loads(cpath.read_text(encoding="utf-8"))
    except Exception:
        pass

    end = datetime.now()
    s = (end - timedelta(days=days + 60)).strftime("%Y%m%d")
    e = end.strftime("%Y%m%d")
    series = []
    for t, name in _universe_tickers(market_code):
        try:
            df = stk.get_market_ohlcv(s, e, t)
            if df is None or len(df) < MIN_HISTORY + 1:
                continue
            series.append({
                "ticker": t, "name": name,
                "closes": [float(x) for x in df["종가"]],
                "volumes": [float(x) for x in df["거래량"]],
            })
        except Exception:
            continue
    result = scan_signals(series)
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        cpath.write_text(json.dumps(result, ensure_ascii=False),
                         encoding="utf-8")
    except Exception:
        pass
    return result


# ─── 이벤트 스터디 (순수) ────────────────────────────


def event_study(series_list: list[dict],
                condition_fn: Callable[[dict], bool],
                horizon: int = DEFAULT_HORIZON,
                arm: Optional[float] = DEFAULT_ARM,
                drop: Optional[float] = DEFAULT_DROP,
                cost: float = 0.0) -> dict:
    """조건 충족 시 진입 + 당신의 청산 구조 적용, 청산까지 재진입 금지(중첩 방지).

    series_list: [{"closes": [...], "volumes": [...] or None}, ...] (종목별)
    cost: 왕복 거래비용(수익률 차감, 예: 0.003)
    반환: {n, avg_ret, win_rate, total_ret, med_hold}
    """
    rets: list[float] = []
    holds: list[int] = []
    for s in series_list:
        closes = s["closes"]
        volumes = s.get("volumes")
        i = MIN_HISTORY
        while i < len(closes) - 1:
            f = compute_features(closes, i, volumes)
            if f is None or not condition_fn(f):
                i += 1
                continue
            window = closes[i:i + 1 + horizon]
            r = exit_backtest.simulate_exit(window, closes[i],
                                            arm=arm, drop=drop)
            rets.append(r["ret"] - cost)
            holds.append(r["day"])
            i += max(r["day"], 1) + 1  # 청산일까지 재진입 금지
    n = len(rets)
    if not n:
        return {"n": 0, "avg_ret": None, "win_rate": None,
                "total_ret": 0.0, "med_hold": None}
    srt = sorted(holds)
    return {
        "n": n,
        "avg_ret": round(sum(rets) / n, 4),
        "win_rate": round(sum(1 for x in rets if x > 0) / n * 100, 1),
        "total_ret": round(sum(rets), 4),
        "med_hold": srt[n // 2],
    }


def split_halves(series_list: list[dict]) -> tuple[list[dict], list[dict]]:
    """각 종목 시계열을 시간 전/후반으로 분할(안정성 점검용, 순수)."""
    first, second = [], []
    for s in series_list:
        closes, volumes = s["closes"], s.get("volumes")
        mid = len(closes) // 2
        # 후반부는 피처 계산용 이력(MIN_HISTORY)을 겹쳐서 제공
        first.append({"closes": closes[:mid],
                      "volumes": volumes[:mid] if volumes else None})
        start = max(0, mid - MIN_HISTORY)
        second.append({"closes": closes[start:],
                       "volumes": volumes[start:] if volumes else None})
    return first, second


def run_conditions(series_list: list[dict], horizon: int = DEFAULT_HORIZON,
                   cost: float = 0.003) -> list[dict]:
    """전 조건 × (전체/전반/후반) 성과표(순수). avg_ret 내림차순."""
    fh, sh = split_halves(series_list)
    out = []
    for name, fn in CONDITIONS.items():
        row = {"name": name,
               "all": event_study(series_list, fn, horizon, cost=cost),
               "h1": event_study(fh, fn, horizon, cost=cost),
               "h2": event_study(sh, fn, horizon, cost=cost)}
        out.append(row)
    return sorted(out, key=lambda r: (r["all"]["avg_ret"] is not None,
                                      r["all"]["avg_ret"] or -9e9), reverse=True)


def format_conditions(rows: list[dict]) -> str:
    lines = ["🎯 진입 조건 이벤트 스터디 (청산 구조 고정 · 거래비용 0.3%p 차감)",
             "   ※ 전/후반이 같은 방향일 때만 신뢰. 표본 적은 칸(†<30)은 참고용."]
    for r in rows:
        def cell(a):
            if not a["n"]:
                return "표본 0"
            mark = "†" if a["n"] < 30 else ""
            return (f"n={a['n']}{mark} 평균 {a['avg_ret']*100:+.2f}% "
                    f"승률 {a['win_rate']}%")
        lines.append(f"• {r['name']}: {cell(r['all'])}")
        lines.append(f"    전반 {cell(r['h1'])} / 후반 {cell(r['h2'])}")
    return "\n".join(lines)


# ─── CLI (실환경 전용 — pykrx 네트워크 필요) ─────────


def _universe_tickers(market_code: str = "1028") -> list[tuple[str, str]]:
    """지수 구성종목 [(ticker, name)] — 3단 폴백.

    1) kium_bot.fetch_universe (캐시)
    2) 지수 PDF 직접 조회 — 최신 pykrx에서 KRX 로그인(KRX_ID/PW) 요구 시 실패
    3) 시가총액 상위 200 근사 (로그인 불필요 엔드포인트, KOSPI200≈시총 Top200)
    """
    name_map = {"1028": "KOSPI200", "2203": "KOSDAQ150"}
    m_name = name_map.get(market_code, "KOSPI200")
    try:
        import kium_bot
        lst = kium_bot.fetch_universe(m_name)
        if lst:
            return [(str(t), str(n)) for t, n in lst]
    except Exception:
        pass

    # 최신 디스크 캐시(오늘자 아니어도 OK — 구성종목은 분기 단위 변동이라 근사로 충분)
    try:
        import glob
        import json
        from pathlib import Path
        cdir = Path(__file__).resolve().parent.parent / "data" / "cache"
        files = sorted(glob.glob(str(cdir / f"universe_{m_name}_*.json")))
        if files:
            lst = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
            if lst:
                print(f"⚠️ 유니버스: 최신 캐시 사용 ({Path(files[-1]).name})")
                return [(str(t), str(n)) for t, n in lst]
    except Exception:
        pass

    from datetime import datetime, timedelta
    from pykrx import stock as stk
    today = datetime.now()

    try:
        tickers = stk.get_index_portfolio_deposit_file(
            market_code, today.strftime("%Y%m%d"))
        if tickers:
            return [(str(t), str(t)) for t in tickers]
    except Exception:
        pass

    # 시총 Top200 근사 — 휴장일 대비 최근 7일 역탐색
    market = "KOSDAQ" if market_code == "2203" else "KOSPI"
    cap_fn = getattr(stk, "get_market_cap", None) or \
        getattr(stk, "get_market_cap_by_ticker")
    for back in range(7):
        d = (today - timedelta(days=back)).strftime("%Y%m%d")
        try:
            df = cap_fn(d, market=market)
            if df is None or len(df) < 50:
                continue
            top = df.sort_values("시가총액", ascending=False).head(200)
            out = []
            for t in top.index:
                try:
                    n = stk.get_market_ticker_name(t)
                except Exception:
                    n = str(t)
                out.append((str(t), str(n)))
            print(f"⚠️ 지수 PDF 조회 불가(KRX 로그인 필요) — "
                  f"{market} 시총 Top200 근사 유니버스 사용 (기준일 {d})")
            return out
        except Exception:
            continue
    raise RuntimeError(
        "유니버스 조회 실패 — KRX 데이터 계정(.env에 KRX_ID/KRX_PW) 설정 "
        "또는 네트워크 확인 필요")


def _fetch_universe_series(market_code: str = "1028",
                           years: int = 3) -> list[dict]:
    """KOSPI200(기본) 구성 종목 일봉 closes/volumes."""
    from datetime import datetime, timedelta
    from pykrx import stock as stk
    end = datetime.now()
    start = end - timedelta(days=365 * years + 90)
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    out = []
    for t, name in _universe_tickers(market_code):
        try:
            df = stk.get_market_ohlcv(s, e, t)
            if df is None or len(df) < MIN_HISTORY * 2:
                continue
            out.append({"ticker": t, "name": name,
                        "closes": [float(x) for x in df["종가"]],
                        "volumes": [float(x) for x in df["거래량"]]})
        except Exception:
            continue
    return out


def _tag_real_entries() -> str:
    """실거래 라운드트립 진입 시점 피처 태깅 → 승/패 교차(가설 후보 추출용)."""
    from datetime import datetime, timedelta
    from pykrx import stock as stk
    import paper_db
    import trade_analytics as ta

    rts = ta.compute_roundtrips(paper_db.list_trades(limit=100000))
    lines = ["🏷️ 실거래 진입 시점 태깅 (승패 교차용 원자료)"]
    for r in rts:
        try:
            buy = datetime.strptime(r["buy_at"][:10], "%Y-%m-%d")
            s = (buy - timedelta(days=140)).strftime("%Y%m%d")
            df = stk.get_market_ohlcv(s, buy.strftime("%Y%m%d"), r["ticker"])
            closes = [float(x) for x in df["종가"]]
            vols = [float(x) for x in df["거래량"]]
            f = compute_features(closes, len(closes) - 1, vols)
            if not f:
                continue
            lines.append(
                f"{r['buy_at'][:10]} {r['name']} [{'승' if r['pnl'] > 0 else '패'} "
                f"{r['ret']*100:+.1f}%] ma20 {f['ma20_gap']*100:+.1f}% · "
                f"ma60 {f['ma60_gap']*100:+.1f}% · dd20 {f['dd20']*100:+.1f}% · "
                f"5일 {f['ret5']*100:+.1f}% · volz {f['vol_z']:.1f}")
        except Exception as e:
            lines.append(f"{r.get('buy_at', '?')[:10]} {r.get('name')}: 조회 실패 {e}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if "--tag" in sys.argv:
        print(_tag_real_entries())
    else:
        years = int(sys.argv[sys.argv.index("--years") + 1]) \
            if "--years" in sys.argv else 3
        market = sys.argv[sys.argv.index("--market") + 1] \
            if "--market" in sys.argv else "1028"
        series = _fetch_universe_series(market, years)
        print(f"유니버스 {len(series)}종목 × {years}년 로드")
        if not series:
            sys.exit("❌ 유니버스 0종목 — 조회 실패. 위 오류 로그 확인.")
        print(format_conditions(run_conditions(series)))
