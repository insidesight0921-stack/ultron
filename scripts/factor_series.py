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


def matches(rows: list, name: str) -> list:
    """이름이 **정확히** 일치하는 행 전부(순수). 하나라고 가정하지 않는다."""
    target = str(name).replace(" ", "")
    out = []
    for row in rows or []:
        got = None
        for field in NAME_FIELDS:
            if row.get(field):
                got = str(row[field]).replace(" ", "")
                break
        if got == target:
            out.append(row)
    return out


def close_of(rows: list, name: str, *, strict: bool = True) -> Optional[float]:
    """이름이 정확히 일치하는 지수의 종가(순수).

    **같은 이름의 행이 둘 이상이면 값을 내지 않는다**(strict). 첫 번째를
    집으면 응답 순서에 따라 달마다 다른 지수를 집을 수 있고, 그렇게 만들어진
    시계열은 그럴듯한 모양을 유지한 채 전혀 다른 것이 된다 — 2026-09-02
    「코스피 대형주」가 2025-06 이후 3,068 → 9,421로 튄 것이 그것이다.
    소형-대형 상관이 +0.52(정상이면 0.8대)였던 것이 유일한 낌새였다.
    """
    found = matches(rows, name)
    if strict and len(found) > 1:
        return None
    for row in found:
        for field in CLOSE_FIELDS:
            value = _num(row.get(field))
            if value is not None and value > 0:
                return value
    return None


def ambiguity(rows: list, name: str, *, id_fields=("IDX_IND_CD", "IDX_CD", "IND_CD",
                                                   "BAS_TM_CONTN", "IDX_CLSS")) -> list:
    """같은 이름 행들을 구별되게 늘어놓는다(순수) — 무엇이 섞였는지 보려고."""
    out = []
    for row in matches(rows, name):
        ident = {f: row[f] for f in id_fields if row.get(f)}
        close = next((_num(row.get(f)) for f in CLOSE_FIELDS if _num(row.get(f))), None)
        out.append({"id": ident, "close": close,
                    "fields": sorted(k for k in row if row.get(k))})
    return out


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


def pearson(xs: list, ys: list) -> Optional[float]:
    """상관(순수). 2개 미만이거나 분산이 0이면 None."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    if dx <= 0 or dy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (dx * dy) ** 0.5


def jumps(series: dict, *, threshold: float = 0.25) -> list:
    """한 달에 threshold 넘게 움직인 달(순수) — 지수 교체·오조회는 여기서 튄다."""
    out = []
    for month in sorted(series):
        prev = _prev_month(month)
        if prev in series and series[prev]:
            change = series[month] / series[prev] - 1.0
            if abs(change) >= threshold:
                out.append((month, series[prev], series[month], change))
    return out


def audit(series_by_name: dict, *, corr_floor: float = 0.7,
          jump: float = 0.25) -> dict:
    """스프레드를 만들기 **전에** 원자료를 의심한다(순수).

    2026-09-02: 이 검사가 없어서 오염된 200개월을 그대로 국면 분석에 넣을
    뻔했다. 누적 −89.9%p·표준편차 6.07%p는 전혀 이상해 보이지 않았다.
    """
    names = list(series_by_name)
    rets = {n: monthly_returns(series_by_name[n]) for n in names}
    common = sorted(set.intersection(*[set(r) for r in rets.values()])) if rets else []
    corr = None
    if len(names) == 2 and common:
        corr = pearson([rets[names[0]][m] for m in common],
                       [rets[names[1]][m] for m in common])
    bad = {n: jumps(series_by_name[n], threshold=jump) for n in names}
    problems = []
    if corr is not None and corr < corr_floor:
        problems.append(f"두 다리의 상관이 {corr:+.3f} — {corr_floor:+.2f} 미만이면 "
                        f"같은 시장의 대·소형이라 보기 어렵다")
    for n, js in bad.items():
        if js:
            problems.append(f"{n}: 월 {jump:.0%} 넘는 급변 {len(js)}회 "
                            f"({', '.join(m for m, *_ in js[:4])}…)")
    return {"months": len(common), "corr": corr, "jumps": bad,
            "problems": problems, "ok": not problems}


def format_audit(result: dict) -> str:
    """감사 결과(순수). **문제가 있으면 스프레드를 만들지 않는다.**"""
    lines = ["🔍 원자료 감사", ""]
    lines.append(f"  겹치는 달: {result.get('months', 0)}")
    if result.get("corr") is not None:
        lines.append(f"  두 다리 상관: {result['corr']:+.3f}")
    lines.append("")
    if result.get("ok"):
        lines.append("  ✅ 걸린 것 없음 — 스프레드를 만들어도 됩니다.")
        return chr(10).join(lines)
    lines.append("  ❌ 이 원자료로는 스프레드를 만들지 않습니다:")
    for problem in result.get("problems", []):
        lines.append(f"   · {problem}")
    return chr(10).join(lines)


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


def quarantine(path: Path, name: str, *, note: str = "") -> dict:
    """오염이 확인된 계열을 **지우지 않고** 격리한다.

    지우면 무엇이 잘못됐었는지 다시 볼 수 없고, 남겨두면 다음 수집이
    「이미 있는 달」로 건너뛰어 오염이 살아남는다. 그래서 옮긴다.
    """
    from datetime import datetime
    data = load_cache(path)
    if name not in data:
        return data
    box = data.setdefault("__quarantine__", {})
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    box[f"{name}@{stamp}"] = {"note": note, "series": data.pop(name)}
    tmp = Path(path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(Path(path))
    return data


def collect(fetch_day, names: list[str], months: list[tuple[int, int]],
            *, path: Optional[Path] = None, have: Optional[dict] = None,
            save_every: int = 12, on_progress=None) -> dict:
    """월말 종가를 모은다. 이미 있는 달은 **부르지 않는다**(이어 받기)."""
    have = {n: dict((have or {}).get(n, {})) for n in names}
    ambiguous: dict = {}
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
                found = matches(rows, name)
                if len(found) > 1:
                    ambiguous.setdefault(name, []).append(key)
                    continue          # **집지 않는다.** 첫 행을 집으면 시계열이 섞인다
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
    if ambiguous:
        have["__ambiguous__"] = ambiguous   # 호출자가 반드시 보게 한다
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
    ap.add_argument("--audit", action="store_true",
                    help="이미 받은 캐시만 검사한다(네트워크 없이)")
    ap.add_argument("--forget", nargs="+", metavar="NAME",
                    help="오염된 계열을 캐시에서 격리한다(지우지 않고 옮긴다)")
    ap.add_argument("--dump", metavar="YYYYMMDD",
                    help="그 날짜에 같은 이름의 행이 몇 개인지 그대로 보여준다")
    args = ap.parse_args()

    path0 = cache_path(args.service, root)
    if args.forget:
        for name in args.forget:
            quarantine(path0, name, note="2026-09-02 같은 이름 중복 의심")
            print(f"  격리: {name} → __quarantine__ (지우지 않았습니다)")
        return 0
    if args.audit:
        cached = load_cache(path0)
        picked = {n: cached.get(n, {}) for n in args.names if cached.get(n)}
        if len(picked) < len(args.names):
            print(f"  캐시에 없는 이름: {[n for n in args.names if n not in picked]}")
        print(format_audit(audit(picked)))
        return 0 if audit(picked)["ok"] else 1

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

    if args.dump:
        payload = k.fetch(p, args.dump)
        rows = [] if payload.get("error") else k.rows_of(payload)
        print(f"  {args.dump} · 응답 {len(rows)}행")
        for name in args.names:
            found = ambiguity(rows, name)
            print(f"\n  「{name}」 정확일치 {len(found)}행")
            for item in found:
                print(f"    close={item['close']} id={item['id']}")
                print(f"    필드: {item['fields']}")
        return 0

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
    flagged = series.pop("__ambiguous__", None)
    if flagged:
        print("\n  ⚠️ 같은 이름의 행이 둘 이상이라 **집지 않은** 달:")
        for name, months in flagged.items():
            print(f"    {name}: {len(months)}개월 ({months[0]} ~ {months[-1]})")
        print("    --dump 으로 무엇이 섞였는지 보세요.")

    checked = audit({n: series.get(n, {}) for n in args.names[:2]})
    print()
    print(format_audit(checked))
    if not checked["ok"]:
        print("\n  스프레드를 만들지 않았습니다. 원자료를 먼저 해결하세요.")
        return 1

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
