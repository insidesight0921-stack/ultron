"""fx_calibrate.py — 원/달러가 주가와 관계가 있는가, 그리고 임계값이 도는가.

**출발점은 안 도는 가지다.** `proxy_indicators`의 환율 항은 이렇게 되어 있다.

    FX_MOVE_PCT = 2.0     # 20일 변화가 ±2% 넘으면 방향으로 본다
    fx_change = None      # ← **변화율을 한 번도 계산하지 않는다**

`_fetch_ecos_series_raw`가 월간만 지원해 최신값 하나만 받아왔기 때문이다.
그래서 이 지표는 **줄곧 `unknown`이었다.** 죽은 가지(임계값이 안 걸림)와
다른 상태다 — 아예 판정 자체를 한 적이 없다. 지표 목록에는 이름이 올라
있으므로 밖에서 보면 4개를 보고 있는 것처럼 보인다.

**두 질문을 구분해서 잰다.**

  ① 같은 날 관계   원화가 약해진 날 주가도 빠졌는가 (동행)
  ② 다음 날 관계   오늘 환율 변화로 내일 방향을 맞히는가 (예측)

①이 강해도 ②는 없을 수 있다. 나우캐스팅이 필요한 것은 ②이고, ①만 보고
지표를 넣으면 **이미 일어난 일을 예측이라고 부르게 된다.**

임계값도 마찬가지로 분포에서 잰다 — ±2%가 20일 변화로 얼마나 자주 걸리는지.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

log = logging.getLogger("fx_calibrate")

FX_SERIES = {"stat_code": "731Y001", "item_code": "0000001", "cycle": "D"}


# ─── 순수 ────────────────────────────────────────────


def window_change_pct(series: list[float], window: int) -> list[Optional[float]]:
    """N거래일 변화율(%). 앞쪽 window개는 None(순수).

    **앞을 None으로 남긴다.** 0으로 채우면 '변화 없음'이라는 없는 사실이
    생기고, 그 위에서 임계값 발동 비율이 낮게 나온다.
    """
    out: list[Optional[float]] = [None] * min(window, len(series))
    for i in range(window, len(series)):
        base = series[i - window]
        out.append((series[i] / base - 1) * 100 if base else None)
    return out


def coverage(changes: Iterable[Optional[float]], *, threshold: float) -> dict:
    """±threshold%가 며칠이나 걸리는가(순수)."""
    vals = [c for c in (changes or []) if c is not None]
    n = len(vals)
    if not n:
        return {"n": 0}
    up = sum(1 for c in vals if c >= threshold)
    down = sum(1 for c in vals if c <= -threshold)
    return {"n": n, "threshold": threshold,
            "weak_krw": up, "strong_krw": down, "neutral": n - up - down,
            "weak_pct": round(up / n * 100, 1),
            "strong_pct": round(down / n * 100, 1),
            "dead_weak": up == 0, "dead_strong": down == 0}


def aligned(a: dict, b: dict) -> tuple[list, list, list]:
    """겹치는 날만 (날짜, a, b)로 정렬(순수). **없는 날을 채우지 않는다.**"""
    days = sorted(set(a) & set(b))
    return days, [a[d] for d in days], [b[d] for d in days]


def same_day_relation(fx: dict, kospi: dict) -> dict:
    """같은 날 환율 변화 vs 주가 변화(순수).

    부호가 음(-)이면 "원화 약세일 때 주가 하락"이다 — 교과서가 말하는 방향.
    """
    import vix_calibrate as vx

    days, f, k = aligned(fx, kospi)
    if len(days) < 30:
        return {"n": len(days)}
    df = [b / a - 1 for a, b in zip(f, f[1:]) if a]
    dk = [b / a - 1 for a, b in zip(k, k[1:]) if a]
    m = min(len(df), len(dk))
    return {"n": m, "corr": vx.correlation(df[:m], dk[:m])}


def next_day_predictions(fx: dict, *, window: int, threshold: float,
                         days: Optional[list] = None) -> list[tuple]:
    """환율 N일 변화 → 다음 날 방향 예측(순수).

    원화 약세(환율 상승)를 하락 신호로 본다 — `proxy_indicators.fx_state`가
    부호를 뒤집어 쓰는 것과 같은 방향이다.
    """
    import nowcast_eval as ne

    keys = sorted(fx)
    values = [fx[d] for d in keys]
    changes = window_change_pct(values, window)
    out = []
    for d, c in zip(keys, changes):
        if days is not None and d not in days:
            continue
        if c is None:
            out.append((d, None))
        elif c >= threshold:
            out.append((d, ne.DOWN))      # 원화 약세 → 위험회피
        elif c <= -threshold:
            out.append((d, ne.UP))
        else:
            out.append((d, None))
    return out


# ─── 보고 ────────────────────────────────────────────


def format_report(fx: dict, kospi: dict, *, window: int,
                  threshold: float) -> str:
    import nowcast_eval as ne
    import vkospi_calibrate as vc

    keys = sorted(fx)
    values = [fx[d] for d in keys]
    d = vc.describe(values)
    lines = [f"💱 원/달러 실측 — {d.get('n', 0)}일 "
             f"({keys[0]} ~ {keys[-1]})" if keys else "💱 표본 없음", ""]
    if not keys:
        return lines[0]
    lines.append(f"  최저 {d['min']:.2f} · 중앙 {d['median']:.2f} · "
                 f"최고 {d['max']:.2f}")
    lines.append("")

    changes = window_change_pct(values, window)
    cov = coverage(changes, threshold=threshold)
    absmax = max((abs(c) for c in changes if c is not None), default=0.0)
    lines.append(f"■ 기존 임계값 (±{threshold}% / {window}거래일)")
    lines.append(f"  원화 약세 {cov['weak_krw']}일 ({cov['weak_pct']}%) · "
                 f"중립 {cov['neutral']}일 · "
                 f"원화 강세 {cov['strong_krw']}일 ({cov['strong_pct']}%)")
    lines.append(f"  {window}일 변화 최대 절대값 {absmax:.2f}%")
    if cov["dead_weak"] and cov["dead_strong"]:
        lines.append("  ⚠️ **양쪽 다 한 번도 안 걸린다 — 항이 없는 것과 같다.**")
    elif cov["dead_weak"] or cov["dead_strong"]:
        side = "약세" if cov["dead_weak"] else "강세"
        lines.append(f"  ⚠️ **원화 {side} 분기가 한 번도 안 걸린다 — 죽은 가지다.**")
    sug = vc.suggest([abs(c) for c in changes if c is not None],
                     high_pctl=80.0, low_pctl=20.0)
    if sug.get("ok"):
        lines.append(f"  분포 기준 제안: ±{sug['high']}% "
                     "(변화 절대값 상위 20%)")
    lines.append("")

    same = same_day_relation(fx, kospi)
    lines.append("■ ① 같은 날 관계 (동행)")
    if same.get("corr") is None:
        lines.append(f"  표본 {same.get('n', 0)}일 — 판정 불가")
    else:
        lines.append(f"  일간 변화 상관 {same['corr']:+.3f} "
                     f"(표본 {same['n']}일)")
        lines.append("  _음(−)이면 '원화 약세일 때 주가 하락'입니다._")
    lines.append("")

    truth = dict(ne.direction_series(sorted(kospi), [kospi[k] for k in sorted(kospi)]))
    lines.append("■ ② 다음 날 관계 (예측)")
    preds = next_day_predictions(fx, window=window, threshold=threshold)
    r = ne.evaluate(preds, truth)
    if not r.get("n"):
        lines.append(f"  {r.get('verdict', '예측 없음')} — 임계값이 안 걸려 잴 것이 없습니다")
    else:
        lines.append(f"  예측 {r['n']}일 · 적중 {r['rate']}% · "
                     f"같은 날 '항상 상승' {r.get('control_rate')}% · "
                     f"백분위 {r.get('percentile')}")
        lines.append(f"  → {r['verdict']}")
    lines.append("")
    lines.append("_①이 강해도 ②는 없을 수 있습니다. 나우캐스팅이 쓰는 것은 ②입니다._")
    return "\n".join(lines)


def _cli() -> int:
    import argparse
    import json
    import sys
    from pathlib import Path

    ap = argparse.ArgumentParser(description="원/달러 임계값 실측 + 주가 관계")
    ap.add_argument("--months", type=int, default=24)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import env_config
        env_config.ensure_env(["ECOS_API_KEY"])
    except ImportError:
        pass

    import proxy_indicators as pi
    import quant_bot as qb

    print(f"원/달러 {args.months}개월 일간 수집 중… (ECOS {FX_SERIES['stat_code']})")
    try:
        payload = qb._fetch_ecos_series_raw(
            FX_SERIES["stat_code"], FX_SERIES["item_code"], "D", args.months)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ ECOS 조회 실패: {exc}")
        return 1
    series = qb._parse_ecos_series(payload)
    if not series:
        print("❌ 응답이 비어 있습니다 — 통계표 코드나 키를 확인하세요.")
        return 1
    fx = {str(d): float(v) for d, v in series}

    import price_sanity as ps

    files = sorted((ps._cache_root() / "indices").glob("market_index_KOSPI_*.json"))
    if not files:
        print("❌ KOSPI 캐시가 없습니다.")
        return 1
    kp = json.loads(files[-1].read_text(encoding="utf-8"))["series"]
    kospi = {str(d): float(c) for d, c in zip(kp["date"], kp["close"])}

    print()
    print(format_report(fx, kospi, window=20, threshold=pi.FX_MOVE_PCT))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
