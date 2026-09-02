"""weight_rule_backtest.py — 비중 규칙을 대조군과 겨룬다 (v1).

**왜 필요한가**: 키움봇은 KOSPI가 200일선 위면 주식 70%, 아래면 50%로
비중을 가른다. 이 규칙은 **방향 예측이 아니라 하락장에서 덜 잃자는 것**이라
나우캐스팅의 「다음날 적중률」로는 잴 수 없다. 그런데 지금까지 한 번도
재지 않았다.

**이 측정의 함정은 하나다.** 비중을 낮추면 MDD는 **당연히** 준다. 그러니
「200일선 규칙이 MDD를 줄였다」는 문장은 그 자체로 아무 말도 아니다.
규칙이 값을 더했는지 알려면 **평균 비중이 같은 고정 전략**과 겨뤄야 한다.
그것이 이 파일의 음성 대조다.

두 번째 함정은 표본이다. 전환 53회는 53개의 독립 사건이 아니다 —
하락 구간은 뭉쳐 있다. 그래서 유의성은 **회전 검정**으로 본다: 비중
시계열을 통째로 k일 밀어 원래 자기상관을 보존한 채 정답과의 정렬만 깬다.
날짜를 무작위로 섞으면 뭉침이 깨져 무엇이든 유의해진다.

**현금 수익률은 0으로 둔다.** 실제로는 채권 ETF를 사지만 그 수익률을
넣으면 「채권이 좋았나」와 「규칙이 좋았나」가 섞인다. 규칙만 본다.
"""
from __future__ import annotations

from typing import Iterable, Optional

ABOVE_WEIGHT = 0.70        # 키움봇 실측 규칙 (kium_bot.compute_weight_recommendation)
BELOW_WEIGHT = 0.50
MA_WINDOW = 200
ROTATIONS = 500            # 회전 검정 횟수
MIN_DAYS = 250             # 이보다 짧으면 판정하지 않는다


# ─── 순수: 계열 ──────────────────────────────────────

def moving_average(closes: list[float], window: int = MA_WINDOW) -> list[Optional[float]]:
    """단순이동평균(순수). 창이 차기 전은 None — **채워 넣지 않는다.**"""
    out: list[Optional[float]] = []
    total = 0.0
    for i, c in enumerate(closes):
        total += c
        if i >= window:
            total -= closes[i - window]
        out.append(total / window if i >= window - 1 else None)
    return out


def ma_weights(closes: list[float], *, window: int = MA_WINDOW,
               above: float = ABOVE_WEIGHT, below: float = BELOW_WEIGHT
               ) -> list[Optional[float]]:
    """200일선 위/아래 비중(순수).

    **그날 종가로 그날 비중을 정하지 않는다.** 종가를 보고 그날 하루를 다시
    사는 것은 미래 참조다. 비중은 전일 판정으로 다음날에 적용한다.
    """
    ma = moving_average(closes, window)
    out: list[Optional[float]] = [None]
    for i in range(1, len(closes)):
        prev_ma = ma[i - 1]
        out.append(None if prev_ma is None else
                   (above if closes[i - 1] >= prev_ma else below))
    return out


def daily_returns(closes: list[float]) -> list[Optional[float]]:
    """일간 수익률(순수). 첫날은 None."""
    return [None] + [(closes[i] / closes[i - 1] - 1.0) if closes[i - 1] else None
                     for i in range(1, len(closes))]


def strategy_returns(rets: list[Optional[float]],
                     weights: list[Optional[float]]) -> list[float]:
    """비중을 곱한 수익률(순수). 어느 쪽이든 None이면 그 날은 빠진다."""
    return [r * w for r, w in zip(rets, weights)
            if r is not None and w is not None]


def aligned_days(rets: list[Optional[float]], weights: list[Optional[float]],
                 days: list[str]) -> list[str]:
    return [d for d, r, w in zip(days, rets, weights)
            if r is not None and w is not None]


def curve(returns: Iterable[float], *, start: float = 1.0) -> list[float]:
    """자산곡선(순수)."""
    out, v = [start], start
    for r in returns:
        v *= (1.0 + r)
        out.append(v)
    return out


def average_weight(weights: list[Optional[float]]) -> Optional[float]:
    """실제로 실린 평균 비중(순수) — **대조군을 만드는 기준.**"""
    vals = [w for w in weights if w is not None]
    return sum(vals) / len(vals) if vals else None


def constant_weights(weights: list[Optional[float]], value: float
                     ) -> list[Optional[float]]:
    """같은 날들에 같은 비중을 싣는다(순수). 비교 구간을 어긋나게 하지 않는다."""
    return [None if w is None else value for w in weights]


def switches(weights: list[Optional[float]]) -> int:
    """비중이 바뀐 횟수(순수) — 규칙이 실제로 몇 번 움직였나."""
    vals = [w for w in weights if w is not None]
    return sum(1 for i in range(1, len(vals)) if vals[i] != vals[i - 1])


# ─── 순수: VKOSPI 오버레이(봇의 실제 규칙) ─────────────
#
# `kium_bot.compute_weight_recommendation`을 그대로 옮긴다.
#   1차 200일선: 위 70% / 아래 50%
#   2차 VKOSPI:  > HIGH → −10%p · < LOW → +10%p
#   클램프 30~90%
# **여기서도 전일 값으로 다음날 비중을 정한다.**

VKOSPI_HIGH = 60.6         # kium_bot 실측 p80 (2026-09-01 확정)
VKOSPI_LOW = 20.7          # p20
OVERLAY_STEP = 0.10
CLAMP = (0.30, 0.90)


def vkospi_adjust(base: Optional[float], vkospi: Optional[float], *,
                  high: float = VKOSPI_HIGH, low: float = VKOSPI_LOW,
                  step: float = OVERLAY_STEP, clamp=CLAMP) -> Optional[float]:
    """VKOSPI 오버레이(순수). 값이 없으면 **기본 비중을 그대로 둔다.**

    모르는 날을 중립(mid)으로 세면 「미확보」가 하나의 판정이 된다 —
    2026-09-01 경보 설계에서 정한 것과 같은 원칙이다.
    """
    if base is None:
        return None
    out = base
    if vkospi is not None:
        if vkospi > high:
            out -= step
        elif vkospi < low:
            out += step
    # **봇과 같은 자리에서 반올림한다.** `compute_weight_recommendation`이
    # `round(base_equity, 2)`로 내보내므로, 여기서 안 하면 0.7+0.1이
    # 0.7999999999999999가 되어 봇이 실제로 쓰는 값과 어긋난다.
    return round(max(clamp[0], min(clamp[1], out)), 2)


def combined_weights(closes: list[float], days: list[str],
                     vkospi_by_day: dict, *, window: int = MA_WINDOW,
                     above: float = ABOVE_WEIGHT, below: float = BELOW_WEIGHT,
                     high: float = VKOSPI_HIGH, low: float = VKOSPI_LOW
                     ) -> list[Optional[float]]:
    """200일선 + VKOSPI를 합친 비중(순수). 봇이 실제로 내는 값."""
    base = ma_weights(closes, window=window, above=above, below=below)
    out: list[Optional[float]] = []
    for i, b in enumerate(base):
        prev_day = days[i - 1] if i > 0 else None
        v = vkospi_by_day.get(prev_day) if prev_day else None
        out.append(vkospi_adjust(b, v, high=high, low=low))
    return out


def overlay_only_weights(closes: list[float], days: list[str],
                         vkospi_by_day: dict, *, window: int = MA_WINDOW,
                         base: float = ABOVE_WEIGHT, **kw
                         ) -> list[Optional[float]]:
    """**VKOSPI 다리만** — 200일선을 끄고 고정 기준에 오버레이만 얹는다.

    합친 규칙이 나아졌다면, 그것이 어느 다리 덕인지 갈라야 한다.
    """
    ma = ma_weights(closes, window=window)          # 판정 가능한 날만 맞춘다
    out: list[Optional[float]] = []
    for i, b in enumerate(ma):
        if b is None:
            out.append(None)
            continue
        prev_day = days[i - 1] if i > 0 else None
        out.append(vkospi_adjust(base, vkospi_by_day.get(prev_day) if prev_day else None,
                                 **kw))
    return out


def restrict(days: list[str], *series, keep) -> tuple:
    """`keep(day)`가 참인 날만 남긴다(순수). 계열들의 길이를 맞춰 자른다."""
    idx = [i for i, d in enumerate(days) if keep(d)]
    return ([days[i] for i in idx],
            *[[s[i] for i in idx] for s in series])


# ─── 순수: 성과 ──────────────────────────────────────

def max_drawdown(values: list[float]) -> Optional[float]:
    """최대 낙폭(순수, 양수 %)."""
    if len(values) < 2:
        return None
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return round(worst * 100, 2)


def total_return(values: list[float]) -> Optional[float]:
    if len(values) < 2 or not values[0]:
        return None
    return round((values[-1] / values[0] - 1.0) * 100, 2)


def sharpe(returns: list[float]) -> Optional[float]:
    """연율 샤프(순수, 무위험 0). 표본 2개 미만이면 None."""
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sd = var ** 0.5
    if sd <= 0:
        return None
    return round(mean / sd * (252 ** 0.5), 2)


def performance(rets: list[Optional[float]], weights: list[Optional[float]]) -> dict:
    """한 전략의 성과(순수)."""
    srets = strategy_returns(rets, weights)
    values = curve(srets)
    return {"n": len(srets), "avg_weight": average_weight(weights),
            "switches": switches(weights),
            "total_return": total_return(values),
            "mdd": max_drawdown(values), "sharpe": sharpe(srets)}


# ─── 순수: 회전 검정 ──────────────────────────────────

def rotate(seq: list, k: int) -> list:
    """k칸 회전(순수). **뭉침을 보존한 채 정렬만 깬다.**"""
    if not seq:
        return []
    k %= len(seq)
    return seq[k:] + seq[:k]


def rotation_test(rets: list[Optional[float]], weights: list[Optional[float]],
                  metric, *, rotations: int = ROTATIONS,
                  larger_is_better: bool = True) -> dict:
    """비중 시계열을 회전시켜 만든 분포에서 실제 값의 백분위(순수).

    **무작위 셔플이 아니라 회전이다.** 하락 구간은 뭉쳐 있어서, 날짜를
    섞으면 그 뭉침이 깨지고 무엇이든 유의해진다(탭 D의 `time` 축 교훈).
    """
    pairs = [(r, w) for r, w in zip(rets, weights) if r is not None and w is not None]
    if len(pairs) < MIN_DAYS:
        return {"n": len(pairs), "percentile": None, "reason": "표본 부족"}
    real_r = [r for r, _ in pairs]
    real_w = [w for _, w in pairs]
    actual = metric(real_r, real_w)
    if actual is None:
        return {"n": len(pairs), "percentile": None, "reason": "지표 계산 불가"}
    step = max(1, len(pairs) // rotations)
    sims = []
    for k in range(step, len(pairs), step):
        got = metric(real_r, rotate(real_w, k))
        if got is not None:
            sims.append(got)
    if not sims:
        return {"n": len(pairs), "percentile": None, "reason": "회전 표본 없음"}
    if larger_is_better:
        better = sum(1 for s in sims if s < actual)
    else:
        better = sum(1 for s in sims if s > actual)
    ties = sum(1 for s in sims if s == actual)
    return {"n": len(pairs), "actual": actual, "trials": len(sims),
            "sim_mean": round(sum(sims) / len(sims), 3),
            "percentile": round((better + ties / 2) / len(sims) * 100, 1)}


def _mdd_metric(rets, weights):
    return max_drawdown(curve(strategy_returns(rets, weights)))


def _return_metric(rets, weights):
    return total_return(curve(strategy_returns(rets, weights)))


# ─── 순수: 보고 ──────────────────────────────────────

def compare(rets: list[Optional[float]], weights: list[Optional[float]],
            *, fixed: float = ABOVE_WEIGHT) -> dict:
    """규칙 · 고정 70% · **평균 비중이 같은 고정**을 나란히(순수)."""
    avg = average_weight(weights)
    out = {
        "규칙(200일선 70/50)": performance(rets, weights),
        f"고정 {fixed:.0%}": performance(rets, constant_weights(weights, fixed)),
    }
    if avg is not None:
        out[f"고정 {avg:.1%}(같은 평균 비중)"] = performance(
            rets, constant_weights(weights, avg))
    return out


def format_compare(table: dict, *, mdd_test: dict = None,
                   ret_test: dict = None) -> str:
    lines = ["📉 비중 규칙 백테스트 — 200일선 위/아래", ""]
    lines.append(f"{'전략':26}{'일수':>6}{'평균비중':>8}{'전환':>6}"
                 f"{'누적':>10}{'MDD':>8}{'샤프':>7}")
    for name, p in table.items():
        lines.append(f"{name:26}{p['n']:>6}"
                     f"{(p['avg_weight'] or 0) * 100:>7.1f}%{p['switches']:>6}"
                     f"{p['total_return']:>9.1f}%{p['mdd']:>7.2f}%"
                     f"{(p['sharpe'] if p['sharpe'] is not None else 0):>7.2f}")
    lines.append("")
    lines.append("  ⚠️ **비중을 낮추면 MDD는 당연히 준다.** 규칙이 값을 더했는지는")
    lines.append("     「고정 70%」가 아니라 **「같은 평균 비중의 고정」**과 견줘야 안다.")
    if mdd_test and mdd_test.get("percentile") is not None:
        lines.append("")
        lines.append(f"  회전 검정(MDD): 실제 {mdd_test['actual']}% · "
                     f"회전 평균 {mdd_test['sim_mean']}% · 백분위 {mdd_test['percentile']}")
    if ret_test and ret_test.get("percentile") is not None:
        lines.append(f"  회전 검정(누적): 실제 {ret_test['actual']}% · "
                     f"회전 평균 {ret_test['sim_mean']}% · 백분위 {ret_test['percentile']}")
    if mdd_test or ret_test:
        lines.append("     _비중 시계열을 통째로 밀어 뭉침은 보존하고 정렬만 깬 분포입니다._")
        lines.append("     _백분위 95 미만은 「이 시점 정렬이 특별하지 않다」는 뜻입니다._")
    return "\n".join(lines)


# ─── I/O ─────────────────────────────────────────────

def load_vkospi(path=None) -> dict:
    """VKOSPI 일별 캐시 → {날짜: 값}. 없으면 빈 dict."""
    import json
    from pathlib import Path as _P

    if path is None:
        import price_sanity as ps
        files = sorted((ps._cache_root() / "indices").glob("market_index_VKOSPI_*.json"))
        if not files:
            return {}
        path = files[-1]
    payload = json.loads(_P(path).read_text(encoding="utf-8"))
    series = payload.get("series") or {}
    return {str(d): float(c)
            for d, c in zip(series.get("date") or [], series.get("close") or [])}


def load_kospi(path=None) -> tuple[list[str], list[float]]:
    """로컬 KOSPI 일봉 캐시 → (날짜, 종가). 네트워크를 쓰지 않는다."""
    import json
    from pathlib import Path

    if path is None:
        import price_sanity as ps
        files = sorted((ps._cache_root() / "indices").glob("market_index_KOSPI_*.json"))
        if not files:
            return [], []
        path = files[-1]
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    series = payload.get("series") or {}
    return list(series.get("date") or []), [float(c) for c in series.get("close") or []]


def _cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="비중 규칙 백테스트(200일선 위/아래)")
    ap.add_argument("--above", type=float, default=ABOVE_WEIGHT)
    ap.add_argument("--below", type=float, default=BELOW_WEIGHT)
    ap.add_argument("--window", type=int, default=MA_WINDOW)
    ap.add_argument("--from", dest="start", default=None, help="YYYYMMDD")
    ap.add_argument("--combined", action="store_true",
                    help="VKOSPI 오버레이까지 합친 봇의 실제 규칙을 잰다")
    args = ap.parse_args()

    days, closes = load_kospi()
    if not closes:
        print("❌ KOSPI 일봉 캐시가 없습니다 — collect_history.py --kospi 로 먼저 받으세요.")
        return 1
    if args.start:
        keep = [i for i, d in enumerate(days) if d >= args.start]
        days = [days[i] for i in keep]
        closes = [closes[i] for i in keep]
    rets = daily_returns(closes)
    if args.combined:
        vk = load_vkospi()
        if not vk:
            print("❌ VKOSPI 캐시가 없습니다 — collect_history.py --vkospi 로 먼저 받으세요.")
            return 1
        # **VKOSPI가 있는 구간으로만 자른다.** 없는 날을 중립으로 채우면
        # 「미확보」가 하나의 판정이 되고, 두 다리의 비교 구간도 어긋난다.
        span = sorted(vk)
        first, last = span[0], span[-1]
        keep = set(d for d in days if first <= d <= last)
        idx = [i for i, d in enumerate(days) if d in keep]
        if len(idx) < MIN_DAYS:
            print(f"❌ 겹치는 구간이 {len(idx)}일뿐입니다 — {MIN_DAYS}일 미만이면 판정하지 않습니다.")
            return 1
        combined = combined_weights(closes, days, vk, window=args.window,
                                    above=args.above, below=args.below)
        ma_only = ma_weights(closes, window=args.window,
                             above=args.above, below=args.below)
        vk_only = overlay_only_weights(closes, days, vk, window=args.window,
                                       base=args.above)
        cut = lambda seq: [seq[i] for i in idx]           # noqa: E731
        rets_c, comb_c = cut(rets), cut(combined)
        table = {"합친 규칙(200일선+VKOSPI)": performance(rets_c, comb_c),
                 "200일선만": performance(rets_c, cut(ma_only)),
                 "VKOSPI만": performance(rets_c, cut(vk_only))}
        avg = average_weight(comb_c)
        table[f"고정 {args.above:.0%}"] = performance(
            rets_c, constant_weights(comb_c, args.above))
        if avg is not None:
            table[f"고정 {avg:.1%}(같은 평균 비중)"] = performance(
                rets_c, constant_weights(comb_c, avg))
        print(f"  겹치는 구간 {len(idx)}일 ({days[idx[0]]} ~ {days[idx[-1]]}) · "
              f"VKOSPI {len(vk)}일 · 창 {args.window}일")
        print(f"  ⚠️ VKOSPI 이력이 {len(vk)}일뿐이라 이 판정의 구간은 그만큼 짧습니다.")
        print()
        print(format_compare(
            table,
            mdd_test=rotation_test(rets_c, comb_c, _mdd_metric, larger_is_better=False),
            ret_test=rotation_test(rets_c, comb_c, _return_metric)))
        return 0
    weights = ma_weights(closes, window=args.window,
                         above=args.above, below=args.below)
    print(f"  KOSPI {len(closes)}일 ({days[0]} ~ {days[-1]}) · "
          f"창 {args.window}일 · 비중 {args.above:.0%}/{args.below:.0%}")
    print()
    print(format_compare(
        compare(rets, weights, fixed=args.above),
        mdd_test=rotation_test(rets, weights, _mdd_metric, larger_is_better=False),
        ret_test=rotation_test(rets, weights, _return_metric)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
