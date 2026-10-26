"""vol_target_backtest.py — 변동성 역수 비중 규칙을 대조군과 겨룬다 (v1, 2026-10-26).

**왜 필요한가.** 2026-10-26 위험 검정에서 방향은 못 맞히는 지표들이 **그 뒤의
흔들림**은 앞서 말했다(VKOSPI·외국인 순매수). 그런데 그 정보를 지금 비중 규칙
(200일선 70/50 + VKOSPI ±10%p)에 넣는 방식은 낙폭을 못 줄였다 — 60.6→45로
바꿔도 회전 백분위 52~61. 정보가 있는데 규칙이 못 쓰는 것이다.

변동성은 뭉친다(오늘 흔들리면 내일도 흔들린다). 그렇다면 문턱으로 계단을 두는
대신 **최근 실현 변동성에 반비례해 비중을 연속으로** 싣는 것이 정보를 덜 버린다
(Moreira & Muir 2017 「Volatility-Managed Portfolios」). 실현 변동성은 KOSPI
종가만으로 계산되므로 9년(2017~)을 쓸 수 있다 — VKOSPI 442일의 한계가 없다.

━━━ 사전 등록 (결과를 보기 전에 고정했다 — 바꾸면 새 버전으로 다시 잰다) ━━━

규칙:
  실현 변동성 σ_t  = t일 **전날까지** 20거래일 일간수익률의 표준편차 × √252
  목표 변동성 σ*_t = t일 전날까지 **모든** σ의 중앙값(확장창, 250일 이상 쌓인 뒤부터)
  비중 w_t        = clamp(기준 × σ*_t / σ_t, 30%, 90%)
  → 고를 파라미터가 없다. 20일·중앙값·30~90%는 문헌 관행과 봇의 기존 클램프를 그대로 쓴다.

겨루는 것(이 넷뿐):
  ① 변동성 규칙        기준 70%
  ② 200일선×변동성     기준 = 200일선 위 70% / 아래 50%
  ③ 200일선만          현행 1차 규칙(대조)
  ④ 고정(같은 평균 비중) 각 규칙과 평균 비중이 같은 고정 전략 — 음성 대조

판정(①·② 각각):
  「우위」 = MDD 회전 백분위 ≥ 95  **그리고**  비용 차감 샤프 ≥ 같은 평균 비중 고정의 샤프
  그 외는 「우위 없음」. 기간을 3등분한 구간별 결과는 **안정성 확인용**이지 판정이 아니다.

비용: 비중 변경량 합 × 0.1%(ETF 수수료+스프레드 추정). 연속 비중은 매일 조금씩
움직이므로 비용을 빼지 않으면 계단 규칙과 공정한 비교가 아니다.
현금 수익률은 0 — `weight_rule_backtest`와 같은 이유(채권 성과와 규칙 성과를 섞지 않는다).
"""
from __future__ import annotations

import math
from typing import Optional

import weight_rule_backtest as wb

VOL_WINDOW = 20            # 실현 변동성 창(거래일)
TARGET_MIN_HISTORY = 250   # 목표 변동성(확장창 중앙값)을 내기 전 최소 이력
BASE_WEIGHT = wb.ABOVE_WEIGHT
CLAMP = wb.CLAMP           # (0.30, 0.90) — 봇과 같은 클램프
COST_PER_TURNOVER = 0.001  # 비중 1.0(100%p) 바꿀 때 0.1%
MDD_BAR = 95.0             # 사전 등록


# ─── 순수: 변동성과 비중 ───────────────────────────────

def realized_vol(rets: list[Optional[float]], window: int = VOL_WINDOW) -> list[Optional[float]]:
    """t일 값 = t일 **전날까지** window개 수익률의 연율 표준편차(순수).

    그날 수익률을 넣으면 그날 비중이 그날 결과를 안다 — 미래 참조.
    """
    out: list[Optional[float]] = []
    for t in range(len(rets)):
        past = [r for r in rets[max(0, t - window):t] if r is not None]
        if t < window or len(past) < window:
            out.append(None)
            continue
        mean = sum(past) / window
        var = sum((r - mean) ** 2 for r in past) / (window - 1)
        out.append(math.sqrt(var) * math.sqrt(252))
    return out


def expanding_median(values: list[Optional[float]],
                     min_history: int = TARGET_MIN_HISTORY) -> list[Optional[float]]:
    """t일 값 = t일 **전날까지** 모인 값들의 중앙값(순수). 이력이 모자라면 None."""
    import bisect

    seen: list[float] = []
    out: list[Optional[float]] = []
    for v in values:
        if len(seen) >= min_history:
            n = len(seen)
            out.append(seen[n // 2] if n % 2 else (seen[n // 2 - 1] + seen[n // 2]) / 2)
        else:
            out.append(None)
        if v is not None:
            bisect.insort(seen, v)
    return out


def vol_target_weights(rets: list[Optional[float]], base: list[Optional[float]], *,
                       window: int = VOL_WINDOW, min_history: int = TARGET_MIN_HISTORY,
                       clamp=CLAMP) -> list[Optional[float]]:
    """w_t = clamp(base_t × σ*_t / σ_t)(순수). 어느 하나라도 모르면 None — 채우지 않는다."""
    vol = realized_vol(rets, window)
    target = expanding_median(vol, min_history)
    out: list[Optional[float]] = []
    for b, s, tgt in zip(base, vol, target):
        if b is None or s is None or tgt is None or s <= 0:
            out.append(None)
            continue
        out.append(round(max(clamp[0], min(clamp[1], b * tgt / s)), 4))
    return out


def turnover(weights: list[Optional[float]]) -> float:
    """비중 변경량 합(순수). None 구간은 건너뛴다."""
    vals = [w for w in weights if w is not None]
    return sum(abs(vals[i] - vals[i - 1]) for i in range(1, len(vals)))


def net_returns(rets: list[Optional[float]], weights: list[Optional[float]], *,
                cost: float = COST_PER_TURNOVER) -> list[float]:
    """비용 차감 일간 수익률(순수). 비중을 바꾼 날 그 변경량만큼 비용을 뺀다."""
    out, prev = [], None
    for r, w in zip(rets, weights):
        if r is None or w is None:
            continue
        c = abs(w - prev) * cost if prev is not None else 0.0
        out.append(r * w - c)
        prev = w
    return out


def evaluate(rets: list[Optional[float]], weights: list[Optional[float]], *,
             cost: float = COST_PER_TURNOVER) -> dict:
    """성과 + 비용 차감 샤프(순수)."""
    perf = wb.performance(rets, weights)
    net = net_returns(rets, weights, cost=cost)
    perf["turnover"] = round(turnover(weights), 2)
    perf["net_sharpe"] = wb.sharpe(net)
    perf["net_return"] = wb.total_return(wb.curve(net))
    return perf


def verdict(rule: dict, fixed: dict, mdd_test: dict, *, bar: float = MDD_BAR) -> str:
    """사전 등록 판정(순수)."""
    pct = (mdd_test or {}).get("percentile")
    if pct is None or rule.get("net_sharpe") is None or fixed.get("sharpe") is None:
        return "판정 불가"
    mdd_ok = pct >= bar
    sharpe_ok = rule["net_sharpe"] >= fixed["sharpe"]
    if mdd_ok and sharpe_ok:
        return f"우위(MDD 회전 백분위 {pct} · 비용 차감 샤프 {rule['net_sharpe']} ≥ 고정 {fixed['sharpe']})"
    why = []
    if not mdd_ok:
        why.append(f"MDD 회전 백분위 {pct} < {bar:g}")
    if not sharpe_ok:
        why.append(f"비용 차감 샤프 {rule['net_sharpe']} < 고정 {fixed['sharpe']}")
    return "우위 없음(" + " · ".join(why) + ")"


def common_days(*series: list[Optional[float]]) -> list[int]:
    """모든 계열이 값이 있는 날의 인덱스(순수) — 비교 구간을 어긋나게 하지 않는다."""
    n = len(series[0])
    return [i for i in range(n) if all(s[i] is not None for s in series)]


# ─── CLI ─────────────────────────────────────────────

def _fmt(name: str, p: dict) -> str:
    return (f"  {name:24s} MDD {p['mdd']:6.2f}%  수익 {p['total_return']:7.2f}%  "
            f"샤프 {p['sharpe']!s:>5}  비용차감 샤프 {p['net_sharpe']!s:>5}  "
            f"평균비중 {p['avg_weight']:.3f}  회전량 {p['turnover']:6.2f}")


def _cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="변동성 역수 비중 규칙 백테스트(사전 등록 v1)")
    ap.parse_args()

    days, closes = wb.load_kospi()
    if not closes:
        print("❌ KOSPI 일봉 캐시가 없습니다.")
        return 1
    rets = wb.daily_returns(closes)
    ma = wb.ma_weights(closes)
    vt = vol_target_weights(rets, [BASE_WEIGHT] * len(rets))
    combo = vol_target_weights(rets, ma)
    idx = [i for i in common_days(rets, ma, vt, combo)]
    cut = lambda s: [s[i] for i in idx]                       # noqa: E731
    r, legs = cut(rets), {"① 변동성 규칙": cut(vt), "② 200일선×변동성": cut(combo),
                          "③ 200일선만": cut(ma)}
    print(f"구간 {days[idx[0]]}~{days[idx[-1]]} · {len(idx)}일 · 비용 {COST_PER_TURNOVER:.1%}/회전량 1.0")
    print()
    results = {}
    for name, w in legs.items():
        p = evaluate(r, w)
        fixed_w = wb.constant_weights(w, p["avg_weight"])
        f = evaluate(r, fixed_w)
        t = wb.rotation_test(r, w, wb._mdd_metric, larger_is_better=False)
        results[name] = (p, f, t)
        print(_fmt(name, p))
        print(_fmt(f"   고정 {p['avg_weight']:.1%}(같은 평균)", f))
        if not name.startswith("③"):
            print(f"     → {verdict(p, f, t)}")
        else:
            print(f"     (대조 — MDD 회전 백분위 {t.get('percentile')})")
    print()
    print("구간별 안정성(판정 아님) — 비용 차감 샤프 / MDD, 같은 평균 비중 고정과 나란히")
    thirds = [idx[k * len(idx) // 3:(k + 1) * len(idx) // 3] for k in range(3)]
    for part in thirds:
        line = f"  {days[part[0]]}~{days[part[-1]]}"
        rr = [rets[i] for i in part]
        for name, w_all in (("①", vt), ("②", combo), ("③", ma)):
            w = [w_all[i] for i in part]
            p = evaluate(rr, w)
            f = evaluate(rr, wb.constant_weights(w, p["avg_weight"]))
            line += (f" | {name} {p['net_sharpe']!s:>5}/{p['mdd']:5.1f}%"
                     f" 고정 {f['net_sharpe']!s:>5}/{f['mdd']:5.1f}%")
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
