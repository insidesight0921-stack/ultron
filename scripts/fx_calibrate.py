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


def same_day_relation(fx: dict, kospi: dict, *, lag: int = 0) -> dict:
    """환율 변화 vs 주가 변화(순수). `lag`만큼 환율을 **뒤로** 민다.

    부호가 음(-)이면 "원화 약세일 때 주가 하락"이다 — 교과서가 말하는 방향.

    **왜 lag을 스캔해야 하는가 — 그리고 실제로 어긋나 있었다.**
    ECOS 731Y001은 매매기준율이고, 2026-09-01 실측 결과 **T일 고시값은
    T-1일 시장을 담고 있다.**

        corr(고시변화[i], 주가변화[i-1]) = **-0.378**   ← 진짜 동행
        corr(고시변화[i], 주가변화[i])   = +0.029
        corr(고시변화[i], 주가변화[i+1]) = +0.006

    lag 0만 보고 "+0.029 → 관계 없음"이라고 결론 낼 뻔했다. **정렬이 틀리면
    진짜 -0.378이 +0.029로 보인다.** 그래서 이 함수는 항상 스캔한다.
    """
    import vix_calibrate as vx

    days, f, k = aligned(fx, kospi)
    if len(days) < 30:
        return {"n": len(days)}
    df = [b / a - 1 for a, b in zip(f, f[1:]) if a]
    dk = [b / a - 1 for a, b in zip(k, k[1:]) if a]
    if lag > 0:
        df, dk = df[:-lag], dk[lag:]
    elif lag < 0:
        df, dk = df[-lag:], dk[:lag]
    m = min(len(df), len(dk))
    if m < 30:
        return {"n": m}
    return {"n": m, "lag": lag, "corr": vx.correlation(df[:m], dk[:m])}


def lag_scan(fx: dict, kospi: dict, *, lags=(-2, -1, 0, 1, 2)) -> list[dict]:
    """여러 lag에서 동행 상관(순수). 가장 강한 곳이 정렬 규약을 드러낸다."""
    out = []
    for lg in lags:
        r = same_day_relation(fx, kospi, lag=lg)
        if r.get("corr") is not None:
            out.append(r)
    return out


def band_outcomes(fx: dict, kospi: dict, *, window: int,
                  threshold: float) -> dict:
    """밴드별 **다음 날 상승 비율**(순수).

    적중률 하나로는 무슨 일이 일어났는지 모른다. "원화 약세일 때 상승 비율이
    평소보다 낮은가"가 진짜 질문이고, 그 답은 밴드별로 갈라 봐야 나온다.
    """
    import nowcast_eval as ne

    kd = sorted(kospi)
    truth = dict(ne.direction_series(kd, [kospi[d] for d in kd]))
    preds = next_day_predictions(fx, window=window, threshold=threshold)
    buckets = {"weak_krw": [], "strong_krw": [], "neutral": [], "all": []}
    keys = sorted(fx)
    values = [fx[d] for d in keys]
    changes = dict(zip(keys, window_change_pct(values, window)))
    for d, want in truth.items():
        buckets["all"].append(want)
        c = changes.get(d)
        if c is None:
            continue
        if c >= threshold:
            buckets["weak_krw"].append(want)
        elif c <= -threshold:
            buckets["strong_krw"].append(want)
        else:
            buckets["neutral"].append(want)
    out = {}
    for name, vals in buckets.items():
        n = len(vals)
        up = sum(1 for v in vals if v == ne.UP)
        out[name] = {"n": n, "up": up,
                     "up_pct": round(up / n * 100, 1) if n else None}
    del preds
    return out


def shift_significance(band: dict, base: dict, *, trials: int = 5000,
                       seed: int = 20260901) -> Optional[float]:
    """밴드의 상승 비율이 전체와 다른가 — 같은 크기로 무작위 추출한 대조(순수).

    **비율 차이만 보고 놀라지 않기 위해서다.** 표본이 작으면 10%p 차이는
    흔하게 나온다.
    """
    import random

    n, up = band.get("n") or 0, band.get("up") or 0
    total, tot_up = base.get("n") or 0, base.get("up") or 0
    if n < 20 or total <= n:
        return None
    pool = [1] * tot_up + [0] * (total - tot_up)
    rng = random.Random(seed)
    obs = up / n
    hits = 0
    for _ in range(trials):
        pick = rng.sample(pool, n)
        if abs(sum(pick) / n - tot_up / total) >= abs(obs - tot_up / total):
            hits += 1
    return (hits + 1) / (trials + 1)


def next_day_predictions(fx: dict, *, window: int, threshold: float,
                         days: Optional[list] = None) -> list[tuple]:
    """환율 N일 변화 → 다음 날 방향 예측(순수).

    원화 약세(환율 상승)를 하락 신호로 본다 — `proxy_indicators.fx_state`가
    부호를 뒤집어 쓰는 것과 같은 방향이다.

    **정렬은 일부러 보수적으로 둔다.** 고시값 d는 d-1일 시장을 담으므로 이
    예측기는 하루 묵은 정보를 쓴다. 제대로 맞춘 판(고시값 d+1 사용)은 종가
    d 시점에는 아직 없는 값이라 미래를 보는 셈이 된다. 그리고 실측에서
    **둘 다 0이다**(정렬 후 다음 날 상관 +0.029) — 보수적으로 두어도 결론이
    달라지지 않는다.
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

    lines.append("■ ① 같은 날 관계 (동행) — **lag 스캔**")
    scan = lag_scan(fx, kospi)
    if not scan:
        lines.append("  표본 부족 — 판정 불가")
    else:
        for r in scan:
            mark = " ←" if abs(r["corr"]) == max(abs(x["corr"]) for x in scan) else ""
            # **라벨이 규약을 반영해야 한다.** 고시값 T가 T-1일 시장을 담으므로
            # 진짜 '같은 날'은 lag -1이다. lag 0을 '같은 날'이라 부르면
            # 다음 사람이 정확히 같은 자리에서 다시 헛읽는다.
            label = {-1: "같은 날(정렬됨)", 0: "환율이 하루 뒤",
                     1: "환율이 이틀 뒤", -2: "주가가 하루 앞"}.get(
                r["lag"], f"lag {r['lag']:+d}")
            lines.append(f"  lag {r['lag']:+d} ({label:<12}) 상관 {r['corr']:+.3f} "
                         f"· 표본 {r['n']}일{mark}")
        lines.append("  _음(−)이면 '원화 약세일 때 주가 하락'입니다._")
        lines.append("  _매매기준율 T일 고시값은 T−1일 시장을 담습니다"
                     "(2026-09-01 실측) — 그래서 lag −1이 '같은 날'입니다._")
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

    lines.append("■ ③ 밴드별 다음 날 상승 비율")
    bo = band_outcomes(fx, kospi, window=window, threshold=threshold)
    base = bo["all"]
    lines.append(f"  전체 {base['n']}일 · 상승 {base['up_pct']}%  ← 이겨야 할 기준")
    for key, label in (("weak_krw", "원화 약세"), ("neutral", "중립"),
                       ("strong_krw", "원화 강세")):
        b = bo[key]
        if not b["n"]:
            lines.append(f"  {label} — 해당 없음")
            continue
        pv = shift_significance(b, base)
        tail = "" if pv is None else f" · 무작위 대조 p={pv:.3f}"
        lines.append(f"  {label} {b['n']:>3}일 · 상승 {b['up_pct']}%"
                     f" ({b['up_pct'] - base['up_pct']:+.1f}%p){tail}")
    lines.append("  _적중률 하나로는 무슨 일이 일어났는지 모릅니다._")
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

    # **받은 원자료를 남긴다.** 매번 다시 받아야 하면 재측정이 네트워크에
    # 묶이고, 네트워크가 막힌 곳에서는 아무도 다시 재지 못한다.
    try:
        out_dir = ps._cache_root() / "fx"
        out_dir.mkdir(parents=True, exist_ok=True)
        last = max(fx)
        (out_dir / f"usdkrw_{last}.json").write_text(
            json.dumps({"pair": "USD/KRW", "source": "ECOS 731Y001",
                        "as_of": last,
                        "series": {"date": sorted(fx),
                                   "close": [fx[d] for d in sorted(fx)]}},
                       ensure_ascii=False), encoding="utf-8")
        print(f"  (원자료 {len(fx)}일 캐시에 저장: cache/fx/usdkrw_{last}.json)")
    except OSError as exc:
        print(f"  (캐시 저장 실패 — 측정은 계속합니다: {exc})")

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
