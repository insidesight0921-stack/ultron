"""vix_calibrate.py — VIX 임계값 실측과 **대용 지표였는지의 검증** (순수 코어 + CLI).

**왜 이걸 또 재는가.** `proxy_indicators`의 VIX 판정은 이렇게 정해져 있다.

    VIX ≤ 18 → risk_on · VIX ≥ 28 → risk_off · 그 사이 neutral

VKOSPI의 `>30 / <15`와 **같은 출처의 값이다** — 교과서에 적힌 "평상시 VIX는
15~20, 20 넘으면 불안, 30 넘으면 공포"에서 왔다. 그 값은 2026-09-01 실측에서
한국 시장에 대해 틀렸고(하단이 407일 중 0일), 같은 실수가 여기 남아 있을 수
있다. 확인하지 않은 채 "미국 지표니까 괜찮겠지"라고 두는 것이 정확히 그 실패다.

**두 번째 질문이 더 중요하다.** 계획서는 VIX를 "VKOSPI 대용"으로 적어두었다.
이제 진짜 VKOSPI가 있으므로 **대용이었는지 직접 잴 수 있다.** 2026-08-27에
VIX는 14.51(=risk_on)인데 같은 무렵 VKOSPI는 46(≈상단 근처)이었다. 한 지표는
"잔잔하다", 다른 지표는 "불안하다"고 말하고 있었다. 한 점만으로는 아무것도
아니지만, 이력을 대면 답이 나온다.

**이 모듈은 판정하지 않는다.** 분포와 일치율을 내놓을 뿐, 임계값 교체는
`vkospi_threshold_review`와 같은 승인 경로로 간다.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

log = logging.getLogger("vix_calibrate")

# proxy_indicators가 실제로 쓰는 값. 여기서 다시 적지 않고 읽어온다 —
# 두 곳에 같은 숫자가 있으면 한쪽만 바뀌는 날이 온다.
FRED_VIX_SERIES = "VIXCLS"

RISK_ON = "risk_on"
RISK_OFF = "risk_off"
NEUTRAL = "neutral"


def _thresholds() -> tuple[float, float]:
    import proxy_indicators as pi

    return float(pi.VIX_CALM), float(pi.VIX_STRESS)


# ─── 분포 (순수) ─────────────────────────────────────


def coverage(values: Iterable[float], *, calm: float, stress: float) -> dict:
    """지금 임계값이 이 분포에서 며칠이나 걸리는가(순수).

    **`proxy_indicators.vix_state`와 같은 부등호를 쓴다**(≤ / ≥). 재는 쪽이
    `<`를 쓰고 도는 쪽이 `≤`를 쓰면, 경계에 값이 몰릴 때 둘이 갈린다.
    """
    vals = [float(v) for v in (values or []) if v is not None]
    n = len(vals)
    if not n:
        return {"n": 0}
    on = sum(1 for v in vals if v <= calm)
    off = sum(1 for v in vals if v >= stress)
    return {
        "n": n, "calm": calm, "stress": stress,
        "risk_on": on, "risk_off": off, "neutral": n - on - off,
        "risk_on_pct": round(on / n * 100, 1),
        "risk_off_pct": round(off / n * 100, 1),
        "dead_on": on == 0,
        "dead_off": off == 0,
        "constant_on": on == n,
        "constant_off": off == n,
    }


def state_of(value: Optional[float], *, calm: float, stress: float) -> str:
    """`proxy_indicators.vix_state`와 같은 판정(순수, 키 없이 쓰기 위한 사본)."""
    if value is None:
        return "unknown"
    if value <= calm:
        return RISK_ON
    if value >= stress:
        return RISK_OFF
    return NEUTRAL


# ─── 대용 검증 (순수) ────────────────────────────────


def proxy_agreement(vix: dict, vkospi: dict, *,
                    vix_calm: float, vix_stress: float,
                    vk_low: float, vk_high: float) -> dict:
    """두 지표가 **같은 날 같은 말을 하는가**(순수).

    상관계수만 보면 안 된다 — 방향이 같아도 판정이 갈리면 봇은 다르게 움직인다.
    실제로 쓰이는 것은 `risk_on/neutral/risk_off` 세 글자이므로 그걸 대조한다.

    반환의 `opposite`는 **한쪽이 risk_on인데 다른 쪽이 risk_off인 날**이다.
    대용 지표라면 이 값이 0에 가까워야 한다.

    **일치율은 혼자서는 아무 뜻이 없다.** 두 지표가 서로 무관해도, 각자
    한쪽으로 치우쳐 있으면 우연히 상당한 비율로 겹친다. 그래서 같은 주변분포를
    가진 두 무관한 지표의 **기대 일치율**과 코헨 kappa를 함께 낸다 —
    kappa가 0 근처면 일치율이 몇 %든 "관계 없음"이다.
    (2026-09-01 초판은 이 대조 없이 36.6%만 내놓았다. 기대값은 35.5%였다.)
    """
    days = sorted(set(vix) & set(vkospi))
    if not days:
        return {"n": 0}
    agree = opposite = 0
    pairs = []
    for d in days:
        a = state_of(vix[d], calm=vix_calm, stress=vix_stress)
        b = (RISK_OFF if vkospi[d] > vk_high
             else (RISK_ON if vkospi[d] < vk_low else NEUTRAL))
        pairs.append((d, a, b))
        if a == b:
            agree += 1
        elif {a, b} == {RISK_ON, RISK_OFF}:
            opposite += 1
    n = len(days)
    exp_agree, exp_opp = _chance(pairs)
    obs = agree / n
    kappa = ((obs - exp_agree) / (1 - exp_agree)) if exp_agree < 1 else None
    return {"n": n, "agree": agree, "opposite": opposite,
            "agree_pct": round(agree / n * 100, 1),
            "opposite_pct": round(opposite / n * 100, 1),
            "expected_agree_pct": round(exp_agree * 100, 1),
            "expected_opposite_pct": round(exp_opp * 100, 1),
            "kappa": round(kappa, 3) if kappa is not None else None,
            "pairs": pairs}


def _chance(pairs: list) -> tuple[float, float]:
    """두 판정이 **서로 무관할 때**의 기대 일치율·기대 정반대율(순수).

    각자의 주변분포는 그대로 두고 독립이라고 가정한다 — 순열검정에서 라벨
    구성을 보존하는 것과 같은 이유다. 치우친 지표는 무관해도 자주 겹친다.
    """
    n = len(pairs)
    if not n:
        return 0.0, 0.0
    states = (RISK_ON, NEUTRAL, RISK_OFF, "unknown")
    pa = {s: sum(1 for _, a, _ in pairs if a == s) / n for s in states}
    pb = {s: sum(1 for _, _, b in pairs if b == s) / n for s in states}
    exp_agree = sum(pa[s] * pb[s] for s in states)
    exp_opp = pa[RISK_ON] * pb[RISK_OFF] + pa[RISK_OFF] * pb[RISK_ON]
    return exp_agree, exp_opp


def correlation(a: list[float], b: list[float]) -> Optional[float]:
    """피어슨 상관(순수). 표본이 모자라면 None."""
    if len(a) != len(b) or len(a) < 3:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return num / (va ** 0.5 * vb ** 0.5)


def changes(series: list[float]) -> list[float]:
    """전일 대비 변화(순수). **수준이 아니라 변화로도 봐야 한다** — 둘 다
    추세적으로 오르면 수준 상관은 높게 나오지만 같이 움직인다는 뜻은 아니다."""
    return [b - a for a, b in zip(series, series[1:])]


# ─── 보고 (순수) ─────────────────────────────────────


def format_report(values: list[float], *, dates: Optional[list] = None,
                  calm: float, stress: float) -> str:
    import vkospi_calibrate as vc

    d = vc.describe(values)
    if not d.get("n"):
        return "📉 VIX 표본이 없습니다."
    span = f" ({dates[0]} ~ {dates[-1]})" if dates else ""
    lines = [f"📉 VIX 분포 실측 — {d['n']}일{span}", ""]
    lines.append(f"  최저 {d['min']:.2f} · p10 {d['p10']:.2f} · p20 {d['p20']:.2f}")
    lines.append(f"  중앙 {d['median']:.2f} · 평균 {d['mean']:.2f}")
    lines.append(f"  p80 {d['p80']:.2f} · p90 {d['p90']:.2f} · 최고 {d['max']:.2f}")
    lines.append("")

    cov = coverage(values, calm=calm, stress=stress)
    lines.append(f"■ 기존 임계값 (≤{calm:.0f} risk_on / ≥{stress:.0f} risk_off)")
    lines.append(f"  risk_on {cov['risk_on']}일 ({cov['risk_on_pct']}%) · "
                 f"neutral {cov['neutral']}일 · "
                 f"risk_off {cov['risk_off']}일 ({cov['risk_off_pct']}%)")
    if cov["dead_on"]:
        lines.append("  ⚠️ **risk_on이 한 번도 안 걸린다 — 죽은 가지다.**")
    if cov["dead_off"]:
        lines.append("  ⚠️ **risk_off가 한 번도 안 걸린다 — 죽은 가지다.**")
    if cov["constant_on"] or cov["constant_off"]:
        lines.append("  ⚠️ **전 기간 한쪽 — 규칙이 아니라 상수다.**")
    lines.append("")

    sug = vc.suggest(values)
    if not sug.get("ok"):
        lines.append(f"■ 제안: {sug['reason']}")
    else:
        lines.append(f"■ 분포 기준 (상위 {sug['high_pctl']:.0f}% / "
                     f"하위 {sug['low_pctl']:.0f}%)")
        lines.append(f"  risk_off ≥ {sug['high']} · risk_on ≤ {sug['low']}")
        chk = coverage(values, calm=sug["low"], stress=sug["high"])
        lines.append(f"  이 값이면 risk_on {chk['risk_on_pct']}% · "
                     f"risk_off {chk['risk_off_pct']}%")
    return "\n".join(lines)


def format_proxy(agr: dict, *, level_corr: Optional[float],
                 change_corr: Optional[float]) -> str:
    """대용이었는지에 대한 답(순수)."""
    if not agr.get("n"):
        return "🔍 VIX·VKOSPI 겹치는 날이 없어 대용 여부를 잴 수 없습니다."
    lines = ["🔍 VIX가 VKOSPI 대용이었는가", ""]
    lines.append(f"  겹치는 날 {agr['n']}일")
    lines.append(f"  같은 판정 {agr['agree']}일 ({agr['agree_pct']}%) · "
                 f"**무관할 때 기대 {agr['expected_agree_pct']}%**")
    lines.append(f"  정반대 판정 {agr['opposite']}일 ({agr['opposite_pct']}%) · "
                 f"기대 {agr['expected_opposite_pct']}%")
    if agr.get("kappa") is not None:
        verdict = ("우연과 구분되지 않는다" if abs(agr["kappa"]) < 0.2
                   else ("약한 일치" if agr["kappa"] < 0.4 else "일치"))
        lines.append(f"  **코헨 kappa {agr['kappa']:+.3f} — {verdict}** "
                     "(0=무관, 1=완전일치)")
    if level_corr is not None:
        lines.append(f"  수준 상관 {level_corr:+.3f}")
    if change_corr is not None:
        lines.append(f"  **일간 변화 상관 {change_corr:+.3f}** "
                     "— 같이 움직이는지는 이쪽이 답한다")
    lines.append("")
    lines.append("_수준 상관이 높아도 둘 다 추세적으로 올랐을 뿐일 수 있습니다._")
    lines.append("_일치율은 혼자서는 뜻이 없습니다 — 기대값과의 차이가 결론입니다._")
    return "\n".join(lines)


def _cli() -> int:
    import argparse
    import json
    import sys
    from pathlib import Path

    ap = argparse.ArgumentParser(description="VIX 임계값 실측 + VKOSPI 대용 검증")
    ap.add_argument("--days", type=int, default=500,
                    help="FRED에서 가져올 최근 거래일 수 (기본 500)")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import env_config
        env_config.ensure_env(["FRED_API_KEY"])
    except ImportError:
        pass

    import quant_bot as qb

    print(f"VIX {args.days}거래일 수집 중… (FRED {FRED_VIX_SERIES})")
    try:
        payload = qb._fetch_fred_series_raw(FRED_VIX_SERIES, months=0,
                                            limit=args.days)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ FRED 조회 실패: {exc}")
        return 1
    series = qb._parse_fred_series(payload)
    if not series:
        print("❌ VIX 응답이 비어 있습니다 — 시리즈 ID나 키를 확인하세요.")
        return 1

    dates = [d.replace("-", "") for d, _ in series]
    closes = [v for _, v in series]
    calm, stress = _thresholds()
    print()
    print(format_report(closes, dates=dates, calm=calm, stress=stress))

    # ── 대용 검증: 로컬 VKOSPI 캐시와 대조 ──
    import price_sanity as ps

    files = sorted((ps._cache_root() / "indices").glob("market_index_VKOSPI_*.json"))
    if not files:
        print()
        print("(VKOSPI 캐시가 없어 대용 여부는 건너뜁니다)")
        return 0
    vk_payload = json.loads(files[-1].read_text(encoding="utf-8"))
    vk = {str(d): float(c) for d, c in zip(vk_payload["series"]["date"],
                                           vk_payload["series"]["close"])}
    vix = dict(zip(dates, closes))

    import kium_bot as kb

    vk_high, vk_low, _src = kb.active_thresholds()
    agr = proxy_agreement(vix, vk, vix_calm=calm, vix_stress=stress,
                          vk_low=vk_low, vk_high=vk_high)
    common = sorted(set(vix) & set(vk))
    a = [vix[d] for d in common]
    b = [vk[d] for d in common]
    print()
    print(format_proxy(agr, level_corr=correlation(a, b),
                       change_corr=correlation(changes(a), changes(b))))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
