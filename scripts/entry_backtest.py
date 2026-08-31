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
from storage_paths import PATHS

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


# ─── 조건 심사 결과 (2026-08-31 재측정) ──────────────
#
# 로컬 일봉 캐시 **199종목 × 291일**, 거래비용 0.3%p 차감.
#
# **2026-08-29자 표는 폐기했다.** 그때는 `price_sanity.load_series`가 최신 캐시
# 파일을 못 읽어 종목당 일봉이 대폭 잘려 있었다(탭 E 커버리지 18%가 같은 원인).
# 가격이 바뀌었으니 조건 성적도 전부 다시 쟀다.
#
#   percentile  진입 빈도만 같고 시점은 무작위인 기준선 대비 백분위(95 미만 = 우연)
#   bootstrap   종목 리샘플링 100회에서 1위를 지킨 비율
#   halves      **전·후반 각각의 초과수익**(구간별 기준선 대비)
#   half_pct    전·후반 각각의 백분위
#
# **`halves`를 원수익에서 초과수익으로 바꿨다.** 이전 표는 전후반 원수익을 비교해
# "신고가돌파 +1.24% → +1.27%, 안정적"이라고 읽었다. 그런데 같은 구간에서
# **아무 때나 사는 기준선도 +2.87% → +0.67%로 떨어졌다.** 시장이 내려간 것을
# 조건의 안정성으로 오독한 것이다. 구간별 기준선과 대조하면 이렇게 나온다.
#
#     신고가돌파  전반 −0.46%p (백분위 0)  ·  후반 +0.69%p (백분위 100)
#
# 부호가 뒤집힌다. 전체 기간 백분위 100은 **후반 하나가 만든 값**이었다.
# 음성 대조인 `베이스라인(무조건)`은 전·후반 모두 백분위 50 — 검사 자체는 정상이다.
#
# **결과: 두 구간을 모두 통과한 조건이 없다. '관찰' 등급은 비어 있다.**
# 신고가돌파와 역추세는 전체 기간만 통과했으므로 보류다.
#
# 실거래로 검증하려면 표본이 훨씬 필요하다 — 운영 DB 완결 63건·표준편차
# 15.90%p 기준, 조건 8종을 놓고 2%p 차이를 가리려면 본페로니 보정 후 **809건**,
# 현재 속도(연 216건)로 **3.7년**이다. 그때까지 백테스트는 후보를 좁히는 데만
# 쓰고, 실거래는 **반증**에 쓴다.
#
# 조건 정의나 기간이 바뀌면 다시 재고, 이 표도 같이 고친다.
CONDITION_REVIEW = {
    "신고가돌파":   {"status": "보류", "percentile": 100, "bootstrap": 59.0,
                     "actual": 0.0179, "baseline": 0.0125,
                     "halves": (-0.0046, 0.0069), "half_pct": (0, 100)},
    "역추세":       {"status": "보류", "percentile": 100, "bootstrap": 20.0,
                     "actual": 0.0154, "baseline": 0.0124,
                     "halves": (0.0166, 0.0002), "half_pct": (100, 56)},
    "정배열":       {"status": "기각", "percentile": 94, "bootstrap": 1.0,
                     "actual": 0.0139, "baseline": 0.0126,
                     "halves": (-0.0050, 0.0013), "half_pct": (0, 94)},
    "추세위+눌림":  {"status": "기각", "percentile": 90, "bootstrap": 20.0,
                     "actual": 0.0139, "baseline": 0.0125,
                     "halves": (-0.0024, -0.0099), "half_pct": (5, 0)},
    "하락나이프":   {"status": "기각", "percentile": 0, "bootstrap": 0.0,
                     "actual": 0.0091, "baseline": 0.0125,
                     "halves": (0.0021, -0.0041), "half_pct": (94, 0)},
    "과낙폭반등":   {"status": "기각", "percentile": 0, "bootstrap": 0.0,
                     "actual": -0.0089, "baseline": 0.0123,
                     "halves": (0.0012, -0.0265), "half_pct": (82, 0)},
}
REVIEWED_AT = "2026-08-31"
REVIEW_SAMPLE = "199종목 × 291일"


def condition_status(name: str) -> str:
    """조건의 심사 상태. 심사하지 않은 조건은 '미심사'."""
    return (CONDITION_REVIEW.get(name) or {}).get("status", "미심사")


_STATUS_ORDER = {"관찰": 0, "보류": 1, "미심사": 2, "기각": 3}


def condition_rank(name: str) -> int:
    """표시 우선순위(작을수록 앞). 기각된 조건은 맨 뒤로 민다."""
    return _STATUS_ORDER.get(condition_status(name), 2)


def half_split_holds(review: dict) -> bool:
    """전·후반이 **둘 다** 기준선을 넘었는가.

    전체 기간 백분위 하나로는 부족하다 — 신고가돌파는 전체 100인데 전반이 0이었다.
    한쪽 구간이 만든 값을 "조건이 통한다"로 읽지 않기 위한 관문이다.
    """
    h1, h2 = review.get("halves", (0.0, 0.0))
    p1, p2 = review.get("half_pct", (0, 0))
    return (h1 > 0) and (h2 > 0) and p1 >= 95 and p2 >= 95


def review_note(name: str) -> str:
    """조건 옆에 붙일 한 줄. **근거의 얇음을 숨기지 않는다.**"""
    r = CONDITION_REVIEW.get(name)
    if not r:
        return "미심사"
    if r["status"] == "기각":
        if r["actual"] < r["baseline"]:
            return (f"기각 — 무작위보다 나쁨"
                    f"({r['actual']*100:+.2f}% vs {r['baseline']*100:+.2f}%)")
        return f"기각 — 우연 범위(백분위 {r['percentile']}, 95 미만)"
    edge = (r["actual"] - r["baseline"]) * 100
    p1, p2 = r.get("half_pct", (0, 0))
    flip = "" if half_split_holds(r) else f" · 전후반 뒤집힘(백분위 {p1}/{p2})"
    return (f"{r['status']} — 무작위 대비 {edge:+.2f}%p · 백분위 {r['percentile']} · "
            f"1위유지 {r['bootstrap']:.0f}%{flip}")


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
        # 심사 상태 순으로 정렬 — 화면에서 관찰 대상이 먼저 보이게.
        matched.sort(key=lambda c: (condition_rank(c), c))
        if matched:
            out.append({
                "ticker": s.get("ticker"), "name": s.get("name") or s.get("ticker"),
                "price": closes[-1], "matched": matched,
                # 근거의 얇음을 화면까지 들고 간다. 조건 이름만 보이면
                # "검증된 전략"으로 읽힌다.
                "review": {c: review_note(c) for c in matched},
                "best_status": condition_status(matched[0]),
                "features": {k: (round(v, 4) if isinstance(v, float) else v)
                             for k, v in f.items()},
            })
    # 관찰 대상을 먼저, 그 안에서 충족 조건 수 많은 순.
    return sorted(out, key=lambda r: (condition_rank(r["matched"][0]),
                                      -len(r["matched"])))


def scan_current(market_code: str = "1028", days: int = 140,
                 cache_dir=None) -> list[dict]:
    """현재 시점 유니버스 스캔(실환경, pykrx). 당일 캐시 사용."""
    import json
    from datetime import datetime, timedelta
    from pathlib import Path
    from pykrx import stock as stk

    cdir = Path(cache_dir) if cache_dir else PATHS.private_state_dir
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


# ─── 안정성 검사 (2026-08-29) ────────────────────────
#
# **왜 필요한가.** 실거래 56건을 아무 의미 없이 무작위로 4분할해 보니, "최고 그룹"이
# 절반의 확률로 승률 50%·평균 +17.6%로 나왔다(전체는 39.3%·+4.5%). 진입 조건이
# 아무 정보를 담지 않아도 그 정도는 나온다는 뜻이다. 그래서 조건의 성적을 **혼자
# 보면 안 되고**, "같은 개수를 아무 때나 샀으면 얼마였나"와 나란히 놓아야 한다.
#
# 청산 파라미터에서 같은 검사를 먼저 했고, 1위가 우연임을 밝혀내 변경을 막았다
# (전후반 불일치 · 부트스트랩 1위 유지율 34% · 2개 경로가 차이의 100%).


def _entry_indices(closes: list[float], volumes, condition_fn,
                   horizon: int, arm, drop) -> list[int]:
    """조건이 실제로 진입한 시점들(중첩 방지 규칙 그대로, 순수)."""
    out, i = [], MIN_HISTORY
    while i < len(closes) - 1:
        f = compute_features(closes, i, volumes)
        if f is None or not condition_fn(f):
            i += 1
            continue
        out.append(i)
        window = closes[i:i + 1 + horizon]
        r = exit_backtest.simulate_exit(window, closes[i], arm=arm, drop=drop)
        i += max(r["day"], 1) + 1        # event_study와 같은 재진입 금지 규칙
    return out


def _returns_at(closes: list[float], entries: list[int], horizon: int,
                arm, drop, cost: float) -> list[float]:
    """주어진 진입 시점들의 수익률(순수)."""
    rets = []
    for i in entries:
        window = closes[i:i + 1 + horizon]
        r = exit_backtest.simulate_exit(window, closes[i], arm=arm, drop=drop)
        rets.append(r["ret"] - cost)
    return rets


def permuted_baseline(series_list: list[dict], condition_fn,
                      horizon: int = DEFAULT_HORIZON,
                      arm=DEFAULT_ARM, drop=DEFAULT_DROP,
                      cost: float = 0.003, trials: int = 200,
                      seed: int = 20260829) -> dict:
    """**진입 빈도만 같고 시점은 무작위인** 기준선(순수).

    조건 대신 "확률 p로 진입"하는 함수를 같은 `event_study`에 태운다. 그래야
    **재진입 금지 규칙이 양쪽에 똑같이** 적용된다.

    첫 구현은 무작위 시점 k개를 뽑아 독립적으로 시뮬레이션했는데, 그건 공정한
    비교가 아니었다. 실제 조건은 청산할 때까지 다시 사지 않는 반면 무작위 쪽은
    상승 구간의 여러 시점을 겹쳐 담을 수 있어, **기준선이 실제보다 유리해진다.**
    실데이터에서 `베이스라인(무조건)` 조건이 백분위 0으로 나와 들켰다 — 정의상
    시점을 고르지 않는 조건이므로 기준선과 같아야 하는데 그렇지 않았다.
    음성 대조가 검사 자체의 결함을 잡아낸 것이다.

    반환: {trials, avg_of_avgs, p95, percentile_of_actual, actual, n}
    """
    import random as _random

    actual = event_study(series_list, condition_fn, horizon,
                         arm=arm, drop=drop, cost=cost)
    if not actual["n"]:
        return {"trials": 0, "avg_of_avgs": None, "p95": None,
                "percentile_of_actual": None, "actual": None, "n": 0}

    # 무조건 진입했을 때의 건수 → 조건의 진입 빈도(=확률)를 추정한다.
    full = event_study(series_list, lambda f: True, horizon,
                       arm=arm, drop=drop, cost=cost)
    rate = min(1.0, actual["n"] / full["n"]) if full["n"] else 1.0

    sims = []
    for t in range(trials):
        rng = _random.Random(seed + t)
        stat = event_study(series_list, lambda f: rng.random() < rate,
                           horizon, arm=arm, drop=drop, cost=cost)
        if stat["n"] and stat["avg_ret"] is not None:
            sims.append(stat["avg_ret"])
    if not sims:
        return {"trials": 0, "avg_of_avgs": None, "p95": None,
                "percentile_of_actual": None, "actual": actual["avg_ret"],
                "n": actual["n"]}

    sims.sort()
    # 동률은 절반으로 센다(mid-p). 안 그러면 기준선과 **정확히 같은** 조건
    # (`베이스라인(무조건)`, 진입확률 1.0)이 백분위 0으로 나와 "우연보다 나쁨"으로
    # 읽힌다. 실제로 그렇게 나와서 알아챘다.
    below = sum(1 for x in sims if x < actual["avg_ret"])
    equal = sum(1 for x in sims if x == actual["avg_ret"])
    below += equal / 2
    return {
        "trials": len(sims),
        "avg_of_avgs": round(sum(sims) / len(sims), 4),
        "p95": round(sims[int(len(sims) * 0.95) - 1], 4),
        "percentile_of_actual": round(below / len(sims) * 100, 1),
        "actual": actual["avg_ret"],
        "n": actual["n"],
        "rate": round(rate, 4),
    }


def bootstrap_conditions(series_list: list[dict],
                         conditions: dict = None,
                         horizon: int = DEFAULT_HORIZON,
                         cost: float = 0.003, trials: int = 200,
                         seed: int = 20260829) -> list[dict]:
    """**종목을 리샘플링**해 1위가 유지되는지 본다(순수).

    종목 몇 개가 결과를 떠받치고 있으면 1위가 표본에 따라 흔들린다. 청산
    파라미터에서 실제로 그랬다 — 62경로 중 **2개**가 차이의 100%를 만들었다.
    """
    import random as _random
    rng = _random.Random(seed)
    conds = conditions or CONDITIONS
    wins = {name: 0 for name in conds}
    valid = 0
    for _ in range(trials):
        sample = [series_list[rng.randrange(len(series_list))]
                  for _ in range(len(series_list))]
        best, best_ret = None, None
        for name, fn in conds.items():
            stat = event_study(sample, fn, horizon, cost=cost)
            if not stat["n"] or stat["avg_ret"] is None:
                continue
            if best_ret is None or stat["avg_ret"] > best_ret:
                best, best_ret = name, stat["avg_ret"]
        if best:
            wins[best] += 1
            valid += 1
    return sorted(
        ({"name": k, "win_rate": round(v / valid * 100, 1) if valid else 0.0,
          "wins": v, "trials": valid} for k, v in wins.items()),
        key=lambda r: r["win_rate"], reverse=True)


def format_stability(baselines: dict, boot: list[dict]) -> str:
    """조건별 기준선 대비 위치 + 1위 유지율."""
    lines = ["🧪 안정성 검사 — 조건이 '아무 때나 사기'보다 나은가",
             "   ※ 시점을 섞은 기준선(종목·진입 빈도 동일, 위치만 무작위)과 대조합니다.",
             "   ※ 백분위 95 미만이면 **우연으로 설명되는 범위**입니다."]
    for name, b in baselines.items():
        if not b or b.get("percentile_of_actual") is None:
            lines.append(f"• {name}: 표본 부족 — 판정 불가")
            continue
        verdict = "✅ 기준선 초과" if b["percentile_of_actual"] >= 95 else "❌ 우연 범위"
        lines.append(
            f"• {name}: 실제 {b['actual']*100:+.2f}% (n={b['n']}) vs "
            f"기준선 평균 {b['avg_of_avgs']*100:+.2f}% · 상위 5% 경계 {b['p95']*100:+.2f}% "
            f"→ 백분위 {b['percentile_of_actual']:.0f} {verdict}")
    if boot:
        lines.append("")
        lines.append("종목 리샘플링에서 1위를 지킨 비율 (낮으면 표본 몇 개가 떠받친 것)")
        for r in boot:
            lines.append(f"  {r['name']}: {r['win_rate']}% ({r['wins']}/{r['trials']})")
    return "\n".join(lines)


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
        cdir = PATHS.shareable_cache_dir
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
    """실거래 라운드트립 진입 시점 피처 태깅 → 승/패 교차(가설 후보 추출용).

    2026-08-27: 데이터 품질 규칙에 걸린 라운드트립은 뺀다. 진입가가 며칠 전 종가였던
    건은 '그 시점 피처로 그 결과가 났다'는 관계 자체가 성립하지 않으므로, 승패 교차에
    넣으면 없는 상관을 만들어 낸다. 시세는 로컬 일봉 캐시를 쓴다(pykrx 불필요).
    캐시에 거래량이 없어 `vol_z`는 계산하지 않는다 — 없는 값을 0으로 채우면
    '거래량이 평범했다'는 없는 사실이 만들어진다.
    """
    import paper_db
    import price_sanity as ps
    import trade_analytics as ta

    kept, dropped = ta.roundtrips_for_analysis(paper_db.list_trades(limit=100000))
    calendar = ps.trading_calendar()
    series: dict = {}

    lines = [f"🏷️ 실거래 진입 시점 태깅 (승패 교차용 원자료) — "
             f"{len(kept)}건 (품질 제외 {len(dropped)}건)"]
    for r in kept:
        buy_day = str(r.get("buy_at") or "")[:10].replace("-", "")
        ticker = r.get("ticker")
        if not buy_day or not ticker:
            continue
        if ticker not in series:
            series[ticker] = ps.load_series(ticker, calendar)
        hist = [c for d, c in series[ticker] if d <= buy_day and c]
        if len(hist) < 60:
            lines.append(f"{r['buy_at'][:10]} {r.get('name')}: 일봉 {len(hist)}일 — 건너뜀")
            continue
        f = compute_features(hist, len(hist) - 1, None)
        if not f:
            continue
        lines.append(
            f"{r['buy_at'][:10]} {r['name']} [{'승' if r['pnl'] > 0 else '패'} "
            f"{r['ret']*100:+.1f}%] ma20 {f['ma20_gap']*100:+.1f}% · "
            f"ma60 {f['ma60_gap']*100:+.1f}% · dd20 {f['dd20']*100:+.1f}% · "
            f"5일 {f['ret5']*100:+.1f}%")
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
        if "--stability" in sys.argv:
            print()
            base = {name: permuted_baseline(series, fn)
                    for name, fn in CONDITIONS.items()}
            boot = bootstrap_conditions(series, trials=100)
            print(format_stability(base, boot))
