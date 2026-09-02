"""phase_history.py — 거시 국면을 **과거 시점마다 되감아** 재구성한다.

**막혀 있던 것.** 계획서 탭 C(국면 판단 정확도)는 이렇게 보류돼 있었다.

    ⛔ 미구현 · 국면 캐시가 1개월치(2026-08 Expansion)뿐이라 측정 대상이 없음

그런데 국면은 **순수 함수로 계산된다**(`quant_bot.classify_phase`,
`compute_momentum`, `compute_level`). 원자료인 CLI_KR·CLI_US·BSI_KR은 월간
시계열이고 ECOS·FRED에서 **과거 조회가 된다.** 즉 캐시가 쌓이길 기다릴
이유가 없었다 — 시계열을 길게 받아 각 시점으로 되감으면 국면 이력이 나온다.
(2026-09-01: VIX가 494일 캐시를 두고 '적재 대기'로 뜨던 것과 같은 유형.)

**미래를 보지 않는 것이 이 파일의 전부다.**

t월의 국면은 **t월까지 발표된 값으로만** 계산해야 한다. 전체 시계열로 한 번에
계산한 뒤 잘라 쓰면, 과거 판정에 미래 정보가 섞여 정확도가 가짜로 올라간다.
`snapshot()`이 항상 "최신 시점"을 보도록 짜여 있으므로, 여기서는 시계열을
`series[:i+1]`로 잘라 같은 순수 함수에 넣는다.

**한계도 명시한다.** 이건 "그때 실제로 봇이 그렇게 판단했다"가 아니라
"지금 규칙으로 그때를 판정하면 이렇다"이다. 봇의 실제 판단 기록은 소급할 수
없다(`collect_history` 상단의 경계와 같다). 규칙이 바뀌면 이 이력도 바뀐다.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("phase_history")

# compute_momentum 기본값(recent 6 + baseline 12). 이보다 짧으면 국면이 없다.
MIN_MONTHS = 18
# 정답 없이 판정만 쌓는 것을 막기 위한 최소 이력.
MIN_HISTORY = 24


def phase_at(cli_kr: list, cli_us: list, bsi_kr: list, upto: int) -> dict:
    """`upto`번째 월까지의 값만으로 그 시점 국면을 계산한다(순수).

    `upto`는 인덱스가 아니라 **길이**다 — `series[:upto]`를 쓴다.
    """
    import quant_bot as qb

    kr = list(cli_kr or [])[:upto]
    us = list(cli_us or [])[:upto]
    bsi = list(bsi_kr or [])[:upto]

    kr_level, kr_mom = qb.compute_level(kr), qb.compute_momentum(kr)
    us_level, us_mom = qb.compute_level(us), qb.compute_momentum(us)
    bsi_trend = qb.compute_momentum(bsi, recent_n=3, baseline_n=6)

    phase_kr = qb.classify_phase(kr_level, kr_mom)
    phase_us = qb.classify_phase(us_level, us_mom)
    consensus, confidence = qb.compute_confidence(phase_kr, phase_us, bsi_trend)
    return {
        "phase_kr": phase_kr, "phase_us": phase_us,
        "consensus": consensus, "confidence": round(confidence, 3),
        "cli_kr_level": kr_level, "cli_kr_momentum": kr_mom,
        "bsi_trend": bsi_trend,
    }


def rebuild(cli_kr: list, cli_us: list, bsi_kr: list) -> list[dict]:
    """월별 국면 이력(순수). 각 항목은 **그 달까지의 정보로만** 계산된다.

    반환의 `month`는 CLI_KR 시계열의 시점 라벨을 쓴다 — 세 계열의 길이가
    다를 수 있으므로 기준을 하나로 고정한다(안 그러면 달이 밀린다).
    """
    kr = list(cli_kr or [])
    if len(kr) < MIN_MONTHS:
        return []
    out = []
    for i in range(MIN_MONTHS, len(kr) + 1):
        month = str(kr[i - 1][0])
        # **다른 계열도 같은 달까지만 자른다.** 길이로 자르면 발표 시차 때문에
        # 미래가 섞인다 — 시점 라벨로 잘라야 한다.
        us_cut = [x for x in (cli_us or []) if str(x[0]) <= month]
        bsi_cut = [x for x in (bsi_kr or []) if str(x[0]) <= month]
        row = phase_at(kr, us_cut, bsi_cut, upto=i)
        row["month"] = month
        out.append(row)
    return out


def transitions(history: list[dict], key: str = "consensus") -> list[dict]:
    """국면이 바뀐 지점(순수). **전환이 없으면 정확도를 잴 수 없다.**"""
    out = []
    prev = None
    for row in history or []:
        cur = row.get(key)
        if cur is None:
            continue
        if prev is not None and cur != prev:
            out.append({"month": row["month"], "from": prev, "to": cur})
        prev = cur
    return out


def describe(history: list[dict], key: str = "consensus") -> dict:
    """이력 요약(순수) — 국면 분포와 전환 횟수."""
    rows = [r for r in (history or []) if r.get(key)]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r[key]] = counts.get(r[key], 0) + 1
    flips = transitions(history, key)
    return {
        "n": len(rows),
        "span": (rows[0]["month"], rows[-1]["month"]) if rows else None,
        "counts": counts,
        "distinct": len(counts),
        "flips": len(flips),
        # **한 국면뿐이면 판정 대상이 아니다** — 나우캐스팅에서 배운 것과 같다.
        "usable": len(counts) >= 2 and len(rows) >= MIN_HISTORY,
    }


def format_report(history: list[dict]) -> str:
    d = describe(history)
    lines = ["🌐 거시 국면 이력 (소급 재구성)", ""]
    if not d["n"]:
        lines.append("  국면을 계산할 수 있는 달이 없습니다.")
        lines.append(f"  월간 원자료가 최소 {MIN_MONTHS}개월 필요합니다"
                     " (모멘텀 = 최근 6개월 vs 직전 12개월).")
        return "\n".join(lines)
    lines.append(f"  {d['n']}개월 ({d['span'][0]} ~ {d['span'][1]}) · "
                 f"국면 {d['distinct']}종 · 전환 {d['flips']}회")
    for phase, c in sorted(d["counts"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    {phase:12s} {c:3d}개월 ({c / d['n'] * 100:.1f}%)")
    lines.append("")
    flips = transitions(history)
    if flips:
        lines.append("■ 전환 지점")
        for f in flips[-10:]:
            lines.append(f"    {f['month']}  {f['from']} → {f['to']}")
        if len(flips) > 10:
            lines.append(f"    … 외 {len(flips) - 10}회")
    lines.append("")
    if d["usable"]:
        lines.append("  ✅ 국면이 여러 개이고 이력이 충분합니다 — 정확도 측정 대상입니다.")
    else:
        why = ("국면이 한 종류뿐" if d["distinct"] < 2
               else f"이력 {d['n']}개월 < {MIN_HISTORY}개월")
        lines.append(f"  ⚠️ 아직 측정 대상이 아닙니다({why}).")
        lines.append("     원자료를 더 길게 받으면 늘어납니다 — 기다릴 필요 없습니다.")
    lines.append("")
    lines.append("_각 달의 국면은 **그 달까지의 값으로만** 계산했습니다(미래 미참조)._")
    lines.append("_다만 이건 '그때 봇이 그렇게 판단했다'가 아니라 "
                 "'지금 규칙으로 그때를 판정하면 이렇다'입니다._")
    return "\n".join(lines)


# ─── 얇은 I/O ────────────────────────────────────────


def collect(months: int = 120) -> list[dict]:
    """원자료를 받아 국면 이력을 만든다(네트워크)."""
    import quant_bot as qb

    series = {}
    for name in ("CLI_KR", "CLI_US", "BSI_KR"):
        try:
            series[name] = qb.fetch_series(name, months=months)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 수집 실패: %s", name, exc)
            series[name] = []
    return rebuild(series["CLI_KR"], series["CLI_US"], series["BSI_KR"])


def save(history: list[dict], path=None) -> Optional[str]:
    """국면 이력을 저장한다. **기존 캐시를 덮지 않고 병합한다.**"""
    import json
    from pathlib import Path

    if path is None:
        from storage_paths import PATHS

        path = PATHS.private_state_dir / "phase_history.json"
    p = Path(path)
    merged: dict[str, dict] = {}
    if p.exists():
        try:
            for row in json.loads(p.read_text(encoding="utf-8")):
                merged[str(row.get("month"))] = row
        except (OSError, ValueError, TypeError):
            log.warning("기존 국면 이력을 읽지 못해 병합 없이 씁니다")
    for row in history or []:
        merged[str(row["month"])] = row
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([merged[m] for m in sorted(merged)],
                            ensure_ascii=False, indent=1), encoding="utf-8")
    return str(p)


def _cli() -> int:
    import argparse
    import sys
    from pathlib import Path

    ap = argparse.ArgumentParser(description="거시 국면 이력 소급 재구성")
    ap.add_argument("--months", type=int, default=120,
                    help="원자료를 몇 개월치 받을지 (기본 120 = 10년)")
    ap.add_argument("--save", action="store_true", help="결과를 캐시에 저장")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import env_config
        env_config.ensure_env(["ECOS_API_KEY", "FRED_API_KEY"])
    except ImportError:
        pass

    print(f"거시 원자료 {args.months}개월 수집 중… (CLI_KR · CLI_US · BSI_KR)")
    history = collect(args.months)
    print()
    print(format_report(history))
    if args.save and history:
        where = save(history)
        print()
        print(f"저장: {where} ({len(history)}개월)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
