"""vkospi_calibrate.py — VKOSPI 분포 실측과 임계값 재설정 (순수 코어 + CLI)

**왜 붙이기 전에 재는가.** `kium_bot.compute_weight_recommendation`의 VKOSPI
항은 이렇게 정해져 있다.

    VKOSPI > 30 → 채권 +10%p
    VKOSPI < 15 → 주식 +10%p

이 값은 **실제 VKOSPI가 15~30 사이를 오간다는 전제**에서 나온 것이다. 그런데
이 시장은 지수 일간 변동 상위가 +17.9%/−12.1%인 곳이고, 외부 시세 기준 현재
VKOSPI가 50 근처다. 그대로 붙이면 `>30`이 **상시 참**이 되어 목표가 70%에서
60%로 내려간 뒤 다시는 안 올라온다 — 규칙이 아니라 상수가 된다.

**임계값을 분포에서 정한다.** 백분위로 잡으면 시장이 달라져도 뜻이 유지된다:
"평소보다 많이 불안하면 채권으로", "평소보다 잔잔하면 주식으로".

이 프로젝트에서 임계값을 바깥 값 그대로 가져와 쓴 적이 있고(200일선 기울기
±0.5%는 평가 구간 100일 내내 한 번도 안 바뀌었다 — 2026-08-31 실측), 같은
실수를 반복하지 않기 위한 모듈이다.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

# 기존 임계값(계획서 v3.17). 이 시장에 맞는지가 이 모듈이 답할 질문이다.
LEGACY_HIGH = 30.0
LEGACY_LOW = 15.0

# 백분위 기준 기본값. 상·하위 20%를 "평소와 다르다"로 본다.
# 더 좁히면(10%) 신호가 드물어 반응이 늦고, 넓히면(30%) 늘 어느 쪽이든 걸린다.
HIGH_PCTL = 80.0
LOW_PCTL = 20.0


def percentile(values: list[float], pct: float) -> Optional[float]:
    """선형보간 백분위(순수)."""
    vals = sorted(v for v in (values or []) if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return vals[int(k)]
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def describe(values: Iterable[float]) -> dict:
    """분포 요약(순수)."""
    vals = sorted(v for v in (values or []) if v is not None)
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals), "min": vals[0], "max": vals[-1],
        "p10": percentile(vals, 10), "p20": percentile(vals, 20),
        "median": percentile(vals, 50),
        "p80": percentile(vals, 80), "p90": percentile(vals, 90),
        "mean": sum(vals) / len(vals),
    }


def legacy_coverage(values: Iterable[float], *, high: float = LEGACY_HIGH,
                    low: float = LEGACY_LOW) -> dict:
    """기존 임계값이 이 분포에서 며칠이나 걸리는가(순수).

    **한쪽이 0일이거나 전체 일수면 그 항은 규칙이 아니라 상수다.**
    """
    vals = [v for v in (values or []) if v is not None]
    n = len(vals)
    if not n:
        return {"n": 0}
    above = sum(1 for v in vals if v > high)
    below = sum(1 for v in vals if v < low)
    return {
        "n": n, "high": high, "low": low,
        "above": above, "below": below,
        "above_pct": round(above / n * 100, 1),
        "below_pct": round(below / n * 100, 1),
        "constant_high": above == n,      # 늘 채권 +10%p
        "constant_none": above == 0 and below == 0,   # 아무 때도 안 걸림
        "dead_low": below == 0,           # 주식 +10%p 분기가 죽어 있음
    }


def suggest(values: Iterable[float], *, high_pctl: float = HIGH_PCTL,
            low_pctl: float = LOW_PCTL) -> dict:
    """분포에서 임계값 제안(순수).

    **값을 반올림해서 예쁘게 만들지 않는다.** 0.1 단위 반올림만 한다 —
    "30"처럼 떨어지는 숫자는 근거가 아니라 관습이다.
    """
    vals = [v for v in (values or []) if v is not None]
    if len(vals) < 30:
        return {"ok": False, "n": len(vals),
                "reason": f"표본 {len(vals)}일 — 30일 미만이면 백분위가 흔들린다"}
    hi = percentile(vals, high_pctl)
    lo = percentile(vals, low_pctl)
    return {"ok": True, "n": len(vals),
            "high": round(hi, 1), "low": round(lo, 1),
            "high_pctl": high_pctl, "low_pctl": low_pctl}


def format_report(values: list[float], *, dates: Optional[list] = None) -> str:
    """사람이 읽는 실측 보고(순수)."""
    d = describe(values)
    if not d.get("n"):
        return "📉 VKOSPI 표본이 없습니다."
    span = ""
    if dates:
        span = f" ({dates[0]} ~ {dates[-1]})"
    lines = [f"📉 VKOSPI 분포 실측 — {d['n']}일{span}", ""]
    lines.append(f"  최저 {d['min']:.2f} · p10 {d['p10']:.2f} · p20 {d['p20']:.2f}")
    lines.append(f"  중앙 {d['median']:.2f} · 평균 {d['mean']:.2f}")
    lines.append(f"  p80 {d['p80']:.2f} · p90 {d['p90']:.2f} · 최고 {d['max']:.2f}")
    lines.append("")

    cov = legacy_coverage(values)
    lines.append(f"■ 기존 임계값 (>{cov['high']:.0f} 채권+10%p / "
                 f"<{cov['low']:.0f} 주식+10%p)")
    lines.append(f"  상단 초과 {cov['above']}일 ({cov['above_pct']}%) · "
                 f"하단 미만 {cov['below']}일 ({cov['below_pct']}%)")
    if cov["constant_high"]:
        lines.append("  ⚠️ **전 기간 상단 초과 — 규칙이 아니라 상수다.**")
        lines.append("     붙이는 즉시 목표가 −10%p 되고 다시 안 올라온다.")
    if cov["dead_low"]:
        lines.append("  ⚠️ **하단 분기가 한 번도 걸리지 않는다 — 죽은 가지다.**")
    if cov["constant_none"]:
        lines.append("  ⚠️ 아무 때도 걸리지 않는다 — 항이 없는 것과 같다.")
    lines.append("")

    sug = suggest(values)
    if not sug.get("ok"):
        lines.append(f"■ 제안: {sug['reason']}")
    else:
        lines.append(f"■ 분포 기준 제안 (상위 {sug['high_pctl']:.0f}% / "
                     f"하위 {sug['low_pctl']:.0f}%)")
        lines.append(f"  VKOSPI > {sug['high']} → 채권 +10%p")
        lines.append(f"  VKOSPI < {sug['low']} → 주식 +10%p")
        chk = legacy_coverage(values, high=sug["high"], low=sug["low"])
        lines.append(f"  이 값이면 상단 {chk['above_pct']}% · 하단 {chk['below_pct']}%로 걸린다")
    lines.append("")
    lines.append("_백분위로 잡으면 시장이 달라져도 뜻이 유지된다 —_")
    lines.append("_'평소보다 불안하면 채권', '평소보다 잔잔하면 주식'._")
    lines.append("_임계값을 바꾸기 전에는 예산에 연결하지 않는다._")
    return "\n".join(lines)


def _cli() -> int:
    import argparse
    import json
    from pathlib import Path

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--collect", type=int, metavar="DAYS",
                    help="KRX OPEN API로 최근 N일 수집 후 실측(맥에서 실행)")
    ap.add_argument("--cache", action="store_true",
                    help="이미 수집된 캐시로만 실측")
    args = ap.parse_args()

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import env_config
        env_config.ensure_env(["KRX_OPENAPI_KEY"])
    except ImportError:
        pass

    if args.collect:
        import market_data_collector as mdc
        print(f"VKOSPI {args.collect}일 수집 중… (하루에 한 번씩 호출합니다)")
        payload = mdc.collect_market_index("VKOSPI", days=args.collect)
        closes = [float(c) for c in payload["series"]["close"]]
        dates = [str(d) for d in payload["series"]["date"]]
    else:
        import price_sanity as ps
        files = sorted((ps._cache_root() / "indices").glob("market_index_VKOSPI_*.json"))
        if not files:
            print("VKOSPI 캐시가 없습니다. --collect 365 로 먼저 수집하세요.")
            return 1
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        closes = [float(c) for c in payload["series"]["close"]]
        dates = [str(d) for d in payload["series"]["date"]]

    print()
    print(format_report(closes, dates=dates))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
