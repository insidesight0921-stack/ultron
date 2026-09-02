"""factor_probe.py — 팩터 지수의 **소급 깊이**를 잰다.

`--factors`는 "오늘 이름이 있는가"만 답한다. 탭 C에 필요한 것은 다르다:
국면 이력이 **406개월(1992-09~)** 인데 지수가 2015년부터면 표본은 130개월,
2024년부터면 24개월이다. **이름이 있는 것과 그때 값이 있는 것은 다르다.**

두 가지를 반드시 구분한다 — 섞으면 오답이 조용히 나온다:

    rows 자체가 비어 있다  → **API가 그 시기를 주지 않는다**(지수 문제 아님)
    rows는 있는데 이름 없음 → **그때 그 지수가 없었다**(산출 개시 이전)

앞엣것을 뒤엣것으로 읽으면 "1990년대엔 소형주 지수가 없었다"는 거짓을 만든다.

탐색은 이분법이며 **단조성(한 번 등재되면 계속 등재)을 가정**한다. 가정은
공짜가 아니므로 경계 전후를 표본으로 되짚어 위반을 함께 보고한다.
"""
from __future__ import annotations

from typing import Callable, Optional

# 휴장일 회피용 — 그 달에서 이 순서로 시도해 처음 응답이 오는 날을 쓴다.
# 말일은 연휴에 걸리기 쉬워 중순부터 본다.
MONTH_PROBE_DAYS = (15, 16, 14, 17, 13, 18, 12, 20, 10)

PRESENT = "present"   # 그 달에 그 지수가 있다
ABSENT = "absent"     # 지수 목록은 왔는데 그 이름이 없다
EMPTY = "empty"       # 목록 자체가 비었다 — API가 그 시기를 안 준다


# ─── 순수 ────────────────────────────────────────────

def months_between(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """[start, end] 월 목록(순수). 오래된 것부터."""
    (y0, m0), (y1, m1) = start, end
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def candidate_dates(year: int, month: int, days=MONTH_PROBE_DAYS) -> list[str]:
    """그 달에 시도할 기준일들(순수). YYYYMMDD."""
    return [f"{year:04d}{month:02d}{d:02d}" for d in days]


def name_state(rows: list, name: str, *, name_fields=("IDX_NM", "IDX_NAME", "INDX_NM")) -> str:
    """그 달의 상태(순수). **정확일치**로만 본다 — 부분일치 금지."""
    if not rows:
        return EMPTY
    target = str(name).replace(" ", "")
    for row in rows:
        for field in name_fields:
            value = row.get(field)
            if value and str(value).replace(" ", "") == target:
                return PRESENT
    return ABSENT


def bisect_first_true(items: list, predicate: Callable[[object], bool]) -> Optional[int]:
    """`predicate`가 처음 참이 되는 자리(순수). 끝까지 거짓이면 None.

    **단조성을 가정한다.** 가정이 틀리면 엉뚱한 경계를 반환하므로
    호출자는 `monotonicity_violations`로 되짚어야 한다.
    """
    lo, hi, found = 0, len(items) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if predicate(items[mid]):
            found, hi = mid, mid - 1
        else:
            lo = mid + 1
    return found


def sample_indices(low: int, high: int, count: int = 3) -> list[int]:
    """[low, high]에서 고르게 뽑은 확인용 자리(순수). 범위가 좁으면 그만큼만."""
    if high < low:
        return []
    span = high - low
    if span < count:
        return list(range(low, high + 1))
    step = span / (count - 1) if count > 1 else span
    return sorted({int(round(low + step * i)) for i in range(count)})


def monotonicity_violations(items: list, predicate: Callable[[object], bool],
                            boundary: int, *, count: int = 3) -> list:
    """경계 앞은 거짓, 뒤는 참이어야 한다(순수 로직·판정은 predicate가 한다).

    이분법이 든 가정을 **표본으로 되짚는다**. 위반이 있으면 경계는 무의미하다.
    """
    bad = []
    for i in sample_indices(boundary + 1, len(items) - 1, count):
        if not predicate(items[i]):
            bad.append(("뒤인데 없음", items[i]))
    for i in sample_indices(0, boundary - 1, count):
        if predicate(items[i]):
            bad.append(("앞인데 있음", items[i]))
    return bad


def coverage(first: Optional[tuple[int, int]], last: tuple[int, int]) -> int:
    """소급 가능한 개월 수(순수). 없으면 0."""
    if first is None:
        return 0
    return len(months_between(first, last))


def format_availability(results: dict, *, phase_months: int = 0) -> str:
    """사람이 고르라고 내놓는 표(순수). **고르지 않는다.**"""
    lines = ["📏 팩터 지수 소급 깊이", ""]
    if not results:
        lines.append("  잰 지수가 없습니다.")
        return "\n".join(lines)
    for name, r in results.items():
        if r.get("api_window") is not None and r["api_window"] is False:
            lines.append(f"  · {name}: **API가 그 시기를 주지 않습니다** — 지수 문제가 아닙니다")
            continue
        first, months = r.get("first"), r.get("months", 0)
        if first is None:
            lines.append(f"  · {name}: 조회 구간 어디에도 없습니다")
            continue
        head = f"  · {name}: {first[0]}-{first[1]:02d}부터 · {months}개월"
        if phase_months:
            head += f" (국면 이력 {phase_months}개월의 {months * 100 // phase_months}%)"
        lines.append(head)
        if r.get("violations"):
            lines.append(f"      ⚠️ 단조성 위반 {len(r['violations'])}건 — 경계를 신뢰하지 마세요: "
                         f"{r['violations'][:3]}")
        if r.get("calls"):
            lines.append(f"      조회 {r['calls']}회")
    lines.append("")
    lines.append("_깊이만 잰 것입니다. 어느 것을 쓸지는 사람이 정합니다._")
    lines.append("_롱숏 스프레드로 쓰려면 **두 다리 모두** 같은 구간이 필요합니다._")
    return "\n".join(lines)


# ─── I/O ─────────────────────────────────────────────

def month_rows(fetch_month, year: int, month: int, cache: dict) -> list:
    """그 달의 지수 목록. 휴장일을 피해 며칠 시도하고, 달 단위로 캐시한다.

    캐시가 없으면 이름 5개를 재는 데 같은 달을 5번 부른다.
    """
    key = (year, month)
    if key in cache:
        return cache[key]
    rows: list = []
    for bas in candidate_dates(year, month):
        got = fetch_month(bas)
        if got:
            rows = got
            break
    cache[key] = rows
    return rows


def earliest_month(fetch_month, name: str, months: list[tuple[int, int]],
                   cache: Optional[dict] = None) -> dict:
    """이름이 처음 등장하는 달을 이분법으로 찾고, 가정을 되짚는다."""
    cache = {} if cache is None else cache
    calls_before = len(cache)

    def present(ym) -> bool:
        return name_state(month_rows(fetch_month, ym[0], ym[1], cache), name) == PRESENT

    idx = bisect_first_true(months, present)
    if idx is None:
        # 목록 자체가 한 번도 오지 않았다면 지수가 아니라 API 구간 문제다.
        any_rows = any(month_rows(fetch_month, y, m, cache) for y, m in
                       [months[i] for i in sample_indices(0, len(months) - 1, 5)])
        return {"first": None, "months": 0, "violations": [],
                "api_window": bool(any_rows), "calls": len(cache) - calls_before}
    first = months[idx]
    return {"first": first,
            "months": coverage(first, months[-1]),
            "violations": monotonicity_violations(months, present, idx),
            "api_window": True,
            "calls": len(cache) - calls_before}


def probe_names(fetch_month, names: list[str], months: list[tuple[int, int]]) -> dict:
    """여러 이름을 한 캐시로 잰다 — 달 조회를 공유한다."""
    cache: dict = {}
    return {name: earliest_month(fetch_month, name, months, cache) for name in names}


def _cli() -> int:
    import argparse
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import krx_openapi as k

    ap = argparse.ArgumentParser(description="팩터 지수의 소급 깊이 측정")
    ap.add_argument("--names", nargs="+", required=True,
                    help="정확한 지수명(따옴표로 감쌀 것). --factors 출력에서 그대로 복사")
    ap.add_argument("--service", default="KOSPI 시리즈",
                    choices=sorted(k.INDEX_ENDPOINTS), help="어느 지수 서비스에서 찾을지")
    ap.add_argument("--from", dest="start", default="1992-09", help="시작 YYYY-MM")
    ap.add_argument("--to", dest="end", default=None, help="끝 YYYY-MM (기본: 지난달)")
    ap.add_argument("--phase-months", type=int, default=406,
                    help="비교 기준이 되는 국면 이력 길이")
    args = ap.parse_args()

    if not k._auth_key():
        print(f"❌ {k.KEY_NAME}가 없습니다.")
        return 1

    from datetime import date
    y0, m0 = (int(x) for x in args.start.split("-"))
    if args.end:
        y1, m1 = (int(x) for x in args.end.split("-"))
    else:
        today = date.today()
        y1, m1 = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    months = months_between((y0, m0), (y1, m1))
    path = k.INDEX_ENDPOINTS[args.service]

    def fetch_month(bas_dd: str) -> list:
        payload = k.fetch(path, bas_dd)
        if payload.get("error"):
            return []
        return k.rows_of(payload)

    print(f"  구간 {y0}-{m0:02d} ~ {y1}-{m1:02d} · {len(months)}개월 · {args.service}")
    results = probe_names(fetch_month, args.names, months)
    print()
    print(format_availability(results, phase_months=args.phase_months))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
