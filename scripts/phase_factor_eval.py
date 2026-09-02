"""phase_factor_eval.py — 국면 판정이 팩터 방향을 맞히는가(탭 C).

**크기가 아니라 부호만 본다.** 2026-09-02 실측: 사이즈 스프레드 199개월을
확보했지만 검정 단위는 에피소드 18개이고, 에피소드 평균의 sd가 3.72%p라
최소 검출 차이가 월 4.91~5.48%p다. 현실적인 국면-팩터 효과(월 0.3~0.5%p)의
10배다. **평균 차이는 이 표본으로 잴 수 없다.**

부호는 다르다. 「이 국면에서 소형이 대형을 이기는가」는 에피소드당 하나의
예/아니오이고, 18개면 이항검정이 선다 — 여전히 얇지만 판정선이 현실적이다.

세 가지를 지킨다:

1. **가설을 내가 쓰지 않는다.** `PHASE_FACTOR_WEIGHT`는 이 측정보다 먼저
   코드에 있었다. 봇이 국면별로 Size를 얼마나 사는지가 곧 봇의 가설이다.
   사후에 방향을 정하면 무엇이든 맞는다.

2. **우연 기대는 50%가 아니다.** 스프레드에 방향성 편향이 있으면 「늘 음수」로
   찍어도 맞는다 — nowcast에서 「늘 오른다」가 63.4%였던 것과 같다. 항상
   다수 부호로 찍는 전략의 적중률을 함께 낸다.

3. **발표 시차를 반영한다.** CLI는 참조월보다 늦게 나온다. 그 달의 국면으로
   그 달의 수익률을 설명하면 실전에서 쓸 수 없는 판정이 된다. 기본은
   `lag=2` — 봇이 실제로 쓰는 값이 그만큼 과거다.
"""
from __future__ import annotations

from typing import Optional

LAG_DEFAULT = 2          # 개월. CLI 발표 시차 — 봇이 실제로 보는 값의 나이
ALPHA = 0.05             # 단측. 검증 전에 고정한다
MIN_EPISODES = 12        # 이보다 적으면 비율을 내지 않는다


# ─── 순수: 가설 ───────────────────────────────────────

def expected_signs(weights: dict, factor: str = "Size",
                   *, tol: float = 1e-9) -> dict:
    """국면별 기대 부호(순수). **봇의 가중치에서 기계적으로 끌어낸다.**

    그 국면의 팩터 비중이 전체 평균보다 크면 그 팩터가 이길 것으로 본
    것이고(+1), 작으면 질 것으로 본 것이다(−1). 같으면 예측 없음(0).

    `tol` 없이 부동소수 비교를 하면 **완전히 평평한 가중치에서도 부호가
    생긴다**(0.1의 평균이 0.1이 아니다). 없는 예측을 만들어내는 셈이다.
    """
    values = {phase: w.get(factor, 0.0) for phase, w in (weights or {}).items()}
    if not values:
        return {}
    mean = sum(values.values()) / len(values)
    out = {}
    for phase, value in values.items():
        if abs(value - mean) <= tol:
            out[phase] = 0
        else:
            out[phase] = 1 if value > mean else -1
    return out


def hypothesis_note(weights: dict, factor: str) -> str:
    """그날 쓴 가중치를 문장으로 남긴다(순수).

    가중치는 wiki에서 오고 사람이 고칠 수 있다. 바뀐 줄 모르고 옛 가설로
    판정하면 사후 맞춤과 구분되지 않는다 — **쓴 값을 그대로 적어둔다.**
    """
    items = [f"{phase} {w.get(factor, 0.0):.3f}"
             for phase, w in sorted((weights or {}).items())]
    return f"{factor} 가중: " + " · ".join(items)


# ─── 순수: 국면 이력 ──────────────────────────────────

def phase_by_month(history: list, key: str = "consensus") -> dict:
    """{'YYYY-MM': 국면}(순수)."""
    out = {}
    for row in history or []:
        month = str(row.get("month", ""))[:7]
        if month and row.get(key):
            out[month] = row[key]
    return out


def shift(phases: dict, lag: int) -> dict:
    """국면을 lag개월 **뒤로** 민다(순수).

    M월 국면은 M+lag월에야 알 수 있다. 미루지 않으면 아직 발표되지 않은
    값으로 그 달을 설명하게 된다 — 소급 재구성이 미래를 안 보는 것과
    발표 시차를 반영하는 것은 **다른 문제**다.
    """
    out = {}
    for month, phase in phases.items():
        y, m = int(month[:4]), int(month[5:7])
        m += lag
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        out[f"{y:04d}-{m:02d}"] = phase
    return out


def episodes(phases: dict, *, start: Optional[str] = None) -> list:
    """연속한 같은 국면을 한 덩어리로(순수). [(국면, 시작, 끝), ...]"""
    out, prev = [], None
    for month in sorted(phases):
        if start and month < start:
            continue
        phase = phases[month]
        if phase != prev:
            out.append([phase, month, month])
            prev = phase
        else:
            out[-1][2] = month
    return [tuple(e) for e in out]


# ─── 순수: 판정 ───────────────────────────────────────

def episode_rows(values_by_month: dict, eps: list, *, min_months: int = 1) -> list:
    """에피소드마다 평균과 부호(순수). 값이 없는 에피소드는 빠진다."""
    rows = []
    for phase, first, last in eps:
        xs = [v for m, v in values_by_month.items() if first <= m <= last and v is not None]
        if len(xs) < min_months:
            continue
        mean = sum(xs) / len(xs)
        rows.append({"phase": phase, "start": first, "end": last,
                     "months": len(xs), "mean": mean,
                     "sign": 1 if mean > 0 else (-1 if mean < 0 else 0)})
    return rows


def score(rows: list, expected: dict) -> dict:
    """예측 부호와 실제 부호의 일치(순수). 예측 없음(0)과 무승부는 뺀다."""
    used, hit = 0, 0
    for row in rows:
        want = expected.get(row["phase"], 0)
        if want == 0 or row["sign"] == 0:
            continue
        used += 1
        hit += 1 if want == row["sign"] else 0
    return {"n": used, "hits": hit, "rate": (hit / used) if used else None}


def constant_baseline(rows: list) -> dict:
    """**항상 같은 부호로 찍는** 전략의 적중률(순수) — 진짜 우연 기대.

    스프레드가 대체로 음수인 시장에서 「늘 음수」는 그냥 맞는다.
    이것을 넘지 못하면 국면은 아무것도 더해주지 않은 것이다.
    """
    signs = [r["sign"] for r in rows if r["sign"] != 0]
    if not signs:
        return {"n": 0, "rate": None, "sign": 0}
    plus = sum(1 for s in signs if s > 0)
    best = max(plus, len(signs) - plus)
    return {"n": len(signs), "rate": best / len(signs),
            "sign": 1 if plus >= len(signs) - plus else -1}


def _comb(n: int, k: int) -> int:
    from math import comb
    return comb(n, k)


def binom_p_ge(hits: int, n: int, p: float) -> Optional[float]:
    """P(X >= hits) (순수, 단측). n=0이면 None."""
    if n <= 0 or not 0.0 < p < 1.0:
        return None
    return sum(_comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(hits, n + 1))


def required_hits(n: int, p: float, alpha: float = ALPHA) -> Optional[int]:
    """합격선(순수) — **결과를 보기 전에** 계산한다."""
    if n <= 0 or not 0.0 < p < 1.0:
        return None
    for k in range(0, n + 1):
        value = binom_p_ge(k, n, p)
        if value is not None and value <= alpha:
            return k
    return None


def bar(rows: list, *, alpha: float = ALPHA) -> dict:
    """넘어야 할 선(순수). 우연 기대와 0.5 중 **큰 쪽**을 기준으로."""
    base = constant_baseline(rows)
    n = base["n"]
    p0 = max(0.5, base["rate"] or 0.5)
    need = required_hits(n, p0, alpha)
    return {"n": n, "p0": p0, "need": need,
            "need_rate": (need / n) if (need is not None and n) else None,
            "baseline_sign": base["sign"]}


def verdict(rows: list, expected: dict, *, alpha: float = ALPHA,
            min_episodes: int = MIN_EPISODES) -> dict:
    got = score(rows, expected)
    line = bar(rows, alpha=alpha)
    if got["n"] < min_episodes:
        return {**got, **{"bar": line}, "verdict": "표본 부족",
                "p": None, "passed": None}
    p = binom_p_ge(got["hits"], got["n"], line["p0"])
    passed = bool(line["need"] is not None and got["hits"] >= line["need"])
    return {**got, "bar": line, "p": p, "passed": passed,
            "verdict": "우위 확인" if passed else "우위 확인 불가"}


def format_verdict(result: dict, expected: dict, rows: list, *,
                   label: str = "", lag: int = LAG_DEFAULT) -> str:
    lines = [f"🎯 국면 → 팩터 방향 검정{(' — ' + label) if label else ''}", ""]
    lines.append(f"  발표 시차 {lag}개월 반영 · 에피소드 {len(rows)}개")
    lines.append("")
    lines.append("  사전 등록된 예측(봇의 PHASE_FACTOR_WEIGHT에서 기계적으로 유도):")
    for phase, want in sorted(expected.items()):
        word = {1: "소형 우위", -1: "대형 우위", 0: "예측 없음"}[want]
        lines.append(f"   · {phase}: {word}")
    lines.append("")
    for row in rows:
        want = expected.get(row["phase"], 0)
        mark = "—" if want == 0 or row["sign"] == 0 else ("○" if want == row["sign"] else "✗")
        lines.append(f"   {mark} {row['phase']:12s} {row['start']}~{row['end']} "
                     f"({row['months']:2d}개월) 평균 {row['mean'] * 100:+6.2f}%p")
    line = result.get("bar", {})
    lines.append("")
    if result.get("rate") is None:
        lines.append("  판정할 에피소드가 없습니다.")
        return chr(10).join(lines)
    lines.append(f"  적중 {result['hits']}/{result['n']} = {result['rate'] * 100:.1f}%")
    base_sign = {1: "늘 소형 우위", -1: "늘 대형 우위", 0: "-"}[line.get("baseline_sign", 0)]
    lines.append(f"  우연 기대: {line['p0'] * 100:.1f}% ({base_sign}로 찍었을 때)")
    if line.get("need") is not None:
        lines.append(f"  합격선(α={ALPHA}, 단측, 검증 전 고정): "
                     f"{line['need']}/{line['n']} = {line['need_rate'] * 100:.1f}%")
    if result.get("p") is not None:
        lines.append(f"  p = {result['p']:.3f}")
    lines.append("")
    lines.append(f"  판정: **{result['verdict']}**")
    if result.get("verdict") == "우위 확인 불가":
        lines.append("  국면이 팩터 방향을 예측한다는 근거가 이 표본에서는 없습니다.")
        lines.append("  (없다는 증명이 아닙니다 — 이 크기로는 가릴 수 없다는 뜻입니다.)")
    return chr(10).join(lines)


def _cli() -> int:
    import argparse
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import factor_series as fs
    import quant_bot as qb

    ap = argparse.ArgumentParser(description="국면 → 팩터 방향 검정(탭 C)")
    ap.add_argument("--long", default="코스피 소형주")
    ap.add_argument("--short", default="코스피 대형주")
    ap.add_argument("--service", default="KOSPI 시리즈")
    ap.add_argument("--factor", default="Size")
    ap.add_argument("--lag", type=int, default=LAG_DEFAULT)
    ap.add_argument("--from", dest="start", default="2010-01")
    ap.add_argument("--key", default="consensus")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1] / "data" / "cache"
    cache = fs.load_cache(fs.cache_path(args.service, root))
    for name in (args.long, args.short):
        if not cache.get(name):
            print(f"❌ 캐시에 「{name}」이 없습니다. factor_series.py로 먼저 받으세요.")
            return 1
    sp = fs.spread(fs.monthly_returns(cache[args.long]),
                   fs.monthly_returns(cache[args.short]))

    hist_path = Path(__file__).resolve().parents[1] / "data" / "private" / "state" / "phase_history.json"
    history = json.loads(hist_path.read_text(encoding="utf-8"))
    phases = shift(phase_by_month(history, args.key), args.lag)
    eps = episodes(phases, start=args.start)
    rows = episode_rows(sp, eps)
    weights = qb.parse_phase_weights_from_wiki()
    source = ("wiki" if weights != qb._normalize_phase_weights(
        qb.PHASE_FACTOR_WEIGHTS_FALLBACK) else "코드 fallback")
    expected = expected_signs(weights, args.factor)
    result = verdict(rows, expected)
    print(format_verdict(result, expected, rows,
                         label=f"{args.long} − {args.short} ({args.factor})",
                         lag=args.lag))
    print()
    print(f"  가설 출처: {source} · {hypothesis_note(weights, args.factor)}")
    print(f"  스프레드 {len(sp)}개월 · 국면 키 {args.key} · 시작 {args.start}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
