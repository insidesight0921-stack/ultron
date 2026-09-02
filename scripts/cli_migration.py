"""cli_migration.py — 죽은 CLI 시리즈를 살아 있는 것으로 바꿔도 되는지 잰다.

**상황(2026-09-01).** 국면 판정에 쓰던 OECD CLI `*LOLITONOSTSAM`(Normalised)이
한국·미국 모두 **2024-01에서 멈췄다.** 같은 CLI의 `*LOLITOAASTSAM`
(Amplitude adjusted)은 2026-06까지 갱신 중이다.

**그냥 바꿔 끼우면 안 된다.** `classify_phase`는 `level >= 100`을 기준선으로
쓰는데, 두 계열은 정규화 방식이 다르다.

    Normalised          평균 100, 표준편차로 정규화 — 진폭이 눌린다
    Amplitude adjusted  장기 추세를 100으로 두고 **실제 진폭을 유지**한다

둘 다 "100 근처"지만 그것만으로 같은 뜻이라고 할 수 없다. 오늘 하루 교과서
임계값 다섯 개를 재면서 배운 것이 정확히 이거다 — **값이 비슷해 보이는 것과
같은 뜻인 것은 다르다.**

**그래서 겹치는 구간에서 대조한다.** 두 계열 모두 1990~2024년이 있으므로,
같은 달에 같은 국면을 내는지 직접 셀 수 있다. VIX가 VKOSPI 대용인지 잴 때
쓴 방법(판정 일치율 + 우연 기대 + 코헨 kappa)을 그대로 쓴다.

판정 기준(사전에 정한다 — 결과를 보고 정하면 그건 검증이 아니다):

    kappa >= 0.6 이고 기준선 교차 시점이 크게 어긋나지 않으면  → 교체 가능
    그 미만                                                → 임계값 재설정 필요
"""
from __future__ import annotations

import logging


log = logging.getLogger("cli_migration")

# 사전에 정한 합격선. 결과를 보고 낮추지 않는다.
KAPPA_PASS = 0.6
# 기준선(100) 교차 시점이 이 개월 수 이상 어긋나면 뜻이 다른 것으로 본다.
CROSS_TOLERANCE_MONTHS = 2


def _aligned(a: list, b: list) -> tuple[list, list, list]:
    """같은 달만 남긴 (월, a값, b값)(순수)."""
    da = {str(m): v for m, v in (a or [])}
    db = {str(m): v for m, v in (b or [])}
    months = sorted(set(da) & set(db))
    return months, [da[m] for m in months], [db[m] for m in months]


def compare_levels(old: list, new: list, threshold: float = 100.0) -> dict:
    """기준선 위/아래 판정이 같은가(순수).

    `classify_phase`가 쓰는 것은 level 그 자체가 아니라 **기준선 대비 위치**다.
    그러니 그 판정이 일치하는지가 교체 가능성의 핵심이다.
    """
    months, va, vb = _aligned(old, new)
    n = len(months)
    if n < 24:
        return {"n": n, "usable": False,
                "reason": f"겹치는 달 {n}개 — 24개월 미만이면 판정하지 않는다"}
    sa = [v >= threshold for v in va]
    sb = [v >= threshold for v in vb]
    agree = sum(1 for x, y in zip(sa, sb) if x == y)
    # 우연 기대(각자의 주변분포가 독립일 때) + 코헨 kappa
    pa = sum(sa) / n
    pb = sum(sb) / n
    exp = pa * pb + (1 - pa) * (1 - pb)
    kappa = (agree / n - exp) / (1 - exp) if exp < 1 else None
    return {
        "n": n, "usable": True,
        "span": (months[0], months[-1]),
        "agree": agree, "agree_pct": round(agree / n * 100, 1),
        "expected_pct": round(exp * 100, 1),
        "kappa": round(kappa, 3) if kappa is not None else None,
        "above_old_pct": round(pa * 100, 1),
        "above_new_pct": round(pb * 100, 1),
    }


def crossings(series: list, threshold: float = 100.0) -> list[str]:
    """기준선을 넘나든 달(순수). 국면 전환의 뼈대다."""
    out = []
    prev = None
    for month, value in series or []:
        cur = value >= threshold
        if prev is not None and cur != prev:
            out.append(str(month))
        prev = cur
    return out


def compare_crossings(old: list, new: list, threshold: float = 100.0,
                      tolerance: int = CROSS_TOLERANCE_MONTHS) -> dict:
    """교차 시점이 비슷한 때에 일어나는가(순수).

    **일치율만 보면 안 된다.** 기준선 근처에 오래 머무는 계열이면 판정은
    자주 같아도 전환 시점이 밀릴 수 있고, 국면 판정에서 중요한 것은 그
    시점이다.
    """
    months, va, vb = _aligned(old, new)
    if len(months) < 24:
        return {"usable": False}
    ca = crossings(list(zip(months, va)), threshold)
    cb = crossings(list(zip(months, vb)), threshold)

    def _idx(m):
        return months.index(m) if m in months else None

    matched = 0
    gaps = []
    for m in ca:
        i = _idx(m)
        best = None
        for other in cb:
            j = _idx(other)
            if i is None or j is None:
                continue
            gap = abs(i - j)
            if best is None or gap < best:
                best = gap
        if best is not None and best <= tolerance:
            matched += 1
            gaps.append(best)
    return {
        "usable": True,
        "old_crossings": len(ca), "new_crossings": len(cb),
        "matched": matched,
        "match_pct": round(matched / len(ca) * 100, 1) if ca else None,
        "mean_gap": round(sum(gaps) / len(gaps), 1) if gaps else None,
    }


def verdict(levels: dict, cross: dict) -> dict:
    """교체해도 되는가(순수). **기준을 미리 정해두고 그대로 적용한다.**"""
    if not levels.get("usable"):
        return {"ok": False, "reason": levels.get("reason", "표본 부족")}
    kappa = levels.get("kappa")
    if kappa is None:
        return {"ok": False, "reason": "kappa를 계산할 수 없다(한쪽이 상수)"}
    if kappa < KAPPA_PASS:
        return {"ok": False, "kappa": kappa,
                "reason": f"기준선 판정 일치가 약하다(kappa {kappa} < {KAPPA_PASS}) "
                          "— 임계값을 새 계열의 분포에서 다시 정해야 한다"}
    if cross.get("usable") and (cross.get("match_pct") or 0) < 60:
        return {"ok": False, "kappa": kappa,
                "reason": f"전환 시점이 어긋난다(일치 {cross.get('match_pct')}%) "
                          "— 국면 판정에서 중요한 것은 그 시점이다"}
    return {"ok": True, "kappa": kappa,
            "reason": "기준선 판정과 전환 시점이 모두 맞는다 — 같은 뜻으로 쓸 수 있다"}


def format_report(levels: dict, cross: dict, *, old_name: str,
                  new_name: str) -> str:
    lines = [f"🔄 CLI 시리즈 교체 검증 — {old_name} → {new_name}", ""]
    if not levels.get("usable"):
        lines.append(f"  {levels.get('reason')}")
        return "\n".join(lines)
    lines.append(f"  겹치는 구간 {levels['n']}개월 "
                 f"({levels['span'][0]} ~ {levels['span'][1]})")
    lines.append("")
    lines.append("■ 기준선(100) 위/아래 판정")
    lines.append(f"  같은 판정 {levels['agree']}개월 ({levels['agree_pct']}%) · "
                 f"무관할 때 기대 {levels['expected_pct']}%")
    lines.append(f"  코헨 kappa {levels['kappa']:+.3f}  (합격선 {KAPPA_PASS})")
    lines.append(f"  기준선 위 비율: 기존 {levels['above_old_pct']}% · "
                 f"신규 {levels['above_new_pct']}%")
    lines.append("")
    if cross.get("usable"):
        lines.append("■ 기준선 교차 시점")
        lines.append(f"  기존 {cross['old_crossings']}회 · 신규 {cross['new_crossings']}회")
        lines.append(f"  ±{CROSS_TOLERANCE_MONTHS}개월 안에서 맞은 것 "
                     f"{cross['matched']}회 ({cross['match_pct']}%)"
                     + (f" · 평균 {cross['mean_gap']}개월 차이"
                        if cross.get("mean_gap") is not None else ""))
        lines.append("")
    v = verdict(levels, cross)
    lines.append(("✅ 교체 가능 — " if v["ok"] else "⛔ 그대로 교체 불가 — ") + v["reason"])
    lines.append("")
    lines.append("_합격선은 검증 전에 정했습니다(kappa 0.6 · 전환 일치 60%)._")
    lines.append("_결과를 보고 기준을 낮추면 그건 검증이 아니라 승인입니다._")
    return "\n".join(lines)


def _cli() -> int:
    import argparse
    import sys
    from pathlib import Path

    ap = argparse.ArgumentParser(description="CLI 시리즈 교체 검증")
    ap.add_argument("--months", type=int, default=420,
                    help="겹치는 구간을 최대한 확보한다(기본 420 = 35년)")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import env_config
        env_config.ensure_env(["FRED_API_KEY"])
    except ImportError:
        pass

    import quant_bot as qb

    pairs = [("CLI_KR", "CLI_KR_AA"), ("CLI_US", "CLI_US_AA")]
    for old_key, new_key in pairs:
        print(f"{old_key} · {new_key} 수집 중… ({args.months}개월)")
        try:
            old = qb.fetch_series(old_key, months=args.months)
            new = qb.fetch_series(new_key, months=args.months)
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 수집 실패: {exc}\n")
            continue
        levels = compare_levels(old, new)
        cross = compare_crossings(old, new)
        print()
        print(format_report(levels, cross, old_name=old_key, new_name=new_key))
        print()
        # 새 계열의 기준일도 알린다 — 이번 사고의 핵심이 그것이었다.
        f_old, f_new = qb.freshness(old, old_key), qb.freshness(new, new_key)
        print(f"  기준일: {old_key} {f_old['as_of']}"
              f"({f_old['note'] or '정상'}) · "
              f"{new_key} {f_new['as_of']}({f_new['note'] or '정상'})")
        print("─" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
