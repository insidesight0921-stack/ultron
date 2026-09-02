"""factor_series.py — 팩터 지수 월말 종가를 모아 **스프레드**를 만든다.

탭 C가 필요한 것은 한 지수의 수익률이 아니라 **두 다리의 차이**다.
소형주 단독 수익률은 시장 상승을 그대로 탄다 — 2026-09-01 nowcast에서
「늘 오른다」가 63.4%였던 것과 같은 함정이다.

세 가지를 지킨다:

1. **덮어쓰지 않는다.** 2026-09-01에 일일 수집기가 407일치 VKOSPI 이력을
   780바이트로 덮어썼다. 여기서는 달 단위로 병합하고 새 값만 이긴다.
2. **중간에 끊겨도 이어 받는다.** 200개월을 한 번에 받다 끊기면 처음부터
   다시 받아야 한다면, 실패가 곧 포기가 된다.
3. **국면별 평균은 여기서 내지 않는다.** 이 파일은 원자료와 변동성까지만
   만든다. 국면을 붙이기 전에 판정선을 먼저 정해야 하기 때문이다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# 월말부터 거꾸로 — 마지막 거래일을 찾는다. 31일이 없는 달은 그냥 응답이 없다.
MONTH_END_DAYS = (31, 30, 29, 28, 27, 26, 25, 24, 23, 22)

CLOSE_FIELDS = ("CLSPRC_IDX", "CLSPRC", "TDD_CLSPRC")
NAME_FIELDS = ("IDX_NM", "IDX_NAME", "INDX_NM")


# ─── 순수 ────────────────────────────────────────────

def month_end_candidates(year: int, month: int, days=MONTH_END_DAYS) -> list[str]:
    """그 달의 마지막 거래일 후보(순수). 늦은 날부터."""
    return [f"{year:04d}{month:02d}{d:02d}" for d in days]


def _num(value) -> Optional[float]:
    """','가 든 숫자 문자열을 float로(순수). 못 읽으면 None."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def close_of(rows: list, name: str) -> Optional[float]:
    """이름이 **정확히** 일치하는 지수의 종가(순수). 부분일치 금지."""
    target = str(name).replace(" ", "")
    for row in rows or []:
        got = None
        for field in NAME_FIELDS:
            if row.get(field):
                got = str(row[field]).replace(" ", "")
                break
        if got != target:
            continue
        for field in CLOSE_FIELDS:
            value = _num(row.get(field))
            if value is not None and value > 0:
                return value
    return None


def merge_series(existing: dict, new: dict) -> dict:
    """월 단위 병합(순수). **기존을 지우지 않는다.** 같은 달은 새 값이 이긴다."""
    out = dict(existing or {})
    for month, value in (new or {}).items():
        if value is not None:
            out[month] = value
    return out


def monthly_returns(series: dict) -> dict:
    """연속한 달 사이의 수익률(순수, 소수). **끊긴 구간은 건너뛰지 않는다.**

    2010-03과 2010-06만 있는데 그 둘을 이으면 3개월 수익률을 1개월로 적는
    것이 된다. 바로 앞 달이 없으면 그 달의 수익률은 만들지 않는다.
    """
    out = {}
    for month in sorted(series):
        prev = _prev_month(month)
        if prev in series and series[prev]:
            out[month] = series[month] / series[prev] - 1.0
    return out


def _prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"


def spread(long_returns: dict, short_returns: dict) -> dict:
    """롱 − 숏(순수). **두 다리가 모두 있는 달만** 남는다."""
    return {m: long_returns[m] - short_returns[m]
            for m in sorted(set(long_returns) & set(short_returns))}


def stdev(values) -> Optional[float]:
    """표본 표준편차(순수). 2개 미만이면 None."""
    xs = [v for v in values if v is not None]
    if len(xs) < 2:
        return None
    mean = sum(xs) / len(xs)
    return (sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def block_means(values_by_month: dict, blocks: list[tuple[str, str]]) -> list[float]:
    """에피소드(연속 구간) 단위 평균(순수).

    **달이 아니라 에피소드가 독립 단위다.** 같은 국면이 17개월 이어졌다면
    그 17개월은 한 사건이다. 달을 독립 표본으로 세면 표본이 4배로 부풀고,
    무엇이든 유의해진다 — 탭 D의 `time` 축에서 배운 것과 같다.
    """
    out = []
    for start, end in blocks:
        xs = [v for m, v in values_by_month.items() if start <= m <= end and v is not None]
        if xs:
            out.append(sum(xs) / len(xs))
    return out


def mde(block_sd: Optional[float], n_a: int, n_b: int,
        *, z_alpha: float = 1.96, z_power: float = 0.84) -> Optional[float]:
    """이 표본으로 **알아볼 수 있는 최소 차이**(순수).

    구하기 전에 답을 아는 것이 아니라, **잴 수 없는지를 먼저 아는 것**이다.
    2026-09-01 금리 발표에서 n=11로는 하루 2.47%p 미만을 못 잰다고 계산해
    도구를 만들지 않기로 한 것과 같은 계산.
    """
    if not block_sd or n_a < 1 or n_b < 1:
        return None
    return (z_alpha + z_power) * block_sd * (1.0 / n_a + 1.0 / n_b) ** 0.5


def format_readiness(sd_month: Optional[float], sd_block: Optional[float],
                     groups: dict, *, label: str = "") -> str:
    """국면별 평균을 **보기 전에** 내는 보고서(순수)."""
    lines = [f"📐 측정 가능성 — {label}" if label else "📐 측정 가능성", ""]
    if sd_month is None:
        lines.append("  표본이 없습니다.")
        return "\n".join(lines)
    lines.append(f"  월 스프레드 표준편차: {sd_month * 100:.2f}%p")
    if sd_block is not None:
        lines.append(f"  에피소드 평균의 표준편차: {sd_block * 100:.2f}%p  ← 검정이 쓰는 값")
    lines.append("")
    total = sum(groups.values())
    for name, n in sorted(groups.items(), key=lambda kv: -kv[1]):
        rest = total - n
        value = mde(sd_block, n, rest)
        shown = f"{value * 100:.2f}%p/월" if value else "계산 불가"
        lines.append(f"  · {name}: 에피소드 {n}개 vs 나머지 {rest}개 → 최소 검출 차이 **{shown}**")
    lines.append("")
    lines.append("_국면별 평균은 아직 보지 않았습니다. 판정선을 먼저 정하기 위해서입니다._")
    return "\n".join(lines)


# ─── I/O ─────────────────────────────────────────────

def cache_path(service: str, root: Path) -> Path:
    safe = "".join(ch if ch.isalnum() else "_" for ch in service)
    return Path(root) / f"factor_index_{safe}.json"


def load_cache(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(path: Path, data: dict) -> Path:
    """원자적 쓰기 + **병합**. 다른 이름의 이력을 지우지 않는다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = load_cache(path)
    for name, series in data.items():
        merged[name] = merge_series(merged.get(name, {}), series)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(path)
    return path


def collect(fetch_day, names: list[str], months: list[tuple[int, int]],
            *, path: Optional[Path] = None, have: Optional[dict] = None,
            save_every: int = 12, on_progress=None) -> dict:
    """월말 종가를 모은다. 이미 있는 달은 **부르지 않는다**(이어 받기)."""
    have = {n: dict((have or {}).get(n, {})) for n in names}
    done = 0
    for year, month in months:
        key = f"{year:04d}-{month:02d}"
        if all(key in have[n] for n in names):
            continue
        for bas in month_end_candidates(year, month):
            rows = fetch_day(bas)
            if not rows:
                continue
            for name in names:
                value = close_of(rows, name)
                if value is not None:
                    have[name][key] = value
            break
        done += 1
        if on_progress:
            on_progress(key, {n: len(have[n]) for n in names})
        if path and save_every and done % save_every == 0:
            save_cache(path, have)
    if path:
        save_cache(path, have)
    return have


def _cli() -> int:
    import argparse
    import sys
    from datetime import date

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import krx_openapi as k
    from factor_probe import months_between
    try:
        from paths import PATHS
        root = PATHS.shareable_cache_dir
    except Exception:
        root = Path(__file__).resolve().parents[1] / "data" / "cache"

    ap = argparse.ArgumentParser(description="팩터 지수 월말 종가 수집")
    ap.add_argument("--names", nargs="+", required=True, help="정확한 지수명 2개 이상(롱 먼저)")
    ap.add_argument("--service", default="KOSPI 시리즈", choices=sorted(k.INDEX_ENDPOINTS))
    ap.add_argument("--from", dest="start", default="2010-01")
    ap.add_argument("--to", dest="end", default=None)
    args = ap.parse_args()

    if not k._auth_key():
        print(f"❌ {k.KEY_NAME}가 없습니다.")
        return 1
    y0, m0 = (int(x) for x in args.start.split("-"))
    if args.end:
        y1, m1 = (int(x) for x in args.end.split("-"))
    else:
        t = date.today()
        y1, m1 = (t.year, t.month - 1) if t.month > 1 else (t.year - 1, 12)
    months = months_between((y0, m0), (y1, m1))
    path = cache_path(args.service, root)
    have = load_cache(path)
    todo = sum(1 for y, m in months
               if not all(f"{y:04d}-{m:02d}" in have.get(n, {}) for n in args.names))
    print(f"  {args.service} · {len(months)}개월 중 받을 것 {todo}개월 · 캐시 {path.name}")

    p = k.INDEX_ENDPOINTS[args.service]

    def fetch_day(bas_dd: str) -> list:
        payload = k.fetch(p, bas_dd)
        return [] if payload.get("error") else k.rows_of(payload)

    def progress(month, counts):
        if month.endswith("-12") or month.endswith("-06"):
            print(f"    {month} … " + " / ".join(f"{n}:{c}" for n, c in counts.items()))

    series = collect(fetch_day, args.names, months, path=path, have=have,
                     on_progress=progress)
    print()
    for name in args.names:
        got = series.get(name, {})
        if got:
            print(f"  · {name}: {len(got)}개월 ({min(got)} ~ {max(got)})")
        else:
            print(f"  · {name}: 0개월 — 이름이 맞는지, 서비스가 맞는지 확인하세요")
    long_name, short_name = args.names[0], args.names[1]
    sp = spread(monthly_returns(series.get(long_name, {})),
                monthly_returns(series.get(short_name, {})))
    if not sp:
        print("\n  두 다리가 겹치는 달이 없어 스프레드를 만들지 못했습니다.")
        return 1
    print(f"\n  스프레드({long_name} − {short_name}): {len(sp)}개월")
    print(f"  누적: {sum(sp.values()) * 100:+.1f}%p · 월평균 {sum(sp.values()) / len(sp) * 100:+.3f}%p")
    print(f"  월 표준편차: {(stdev(sp.values()) or 0) * 100:.2f}%p")
    print("\n  국면별 평균은 내지 않았습니다 — 판정선을 먼저 정합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
