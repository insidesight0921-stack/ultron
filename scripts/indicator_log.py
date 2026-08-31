"""indicator_log.py — 대리 지표 시계열 적재 (순수 코어 + 얇은 경계)

**왜 필요한가.** 나우캐스팅 가중치를 정하려면 "각 지표가 국면을 앞서 맞히는가"를
재야 하는데, 2026-08-31 점검 결과 셋 다 없었다.

    지표 시계열      **0**   — `proxy_indicators`는 스냅샷만 만들고 저장하지 않았다
    국면 정답 시계열  1개월   — {'2026-08': 'Expansion'}
    국면 전환 횟수    0회

**시계가 아예 안 돌고 있었다.** 이 모듈은 그 시계를 시작한다. 가중치를 정하지
않는다 — 나중에 정할 수 있도록 원자료를 쌓기만 한다.

설계에서 지킨 것 네 가지.

1. **미확보도 기록한다.** 값이 없는 날을 건너뛰면, 나중에 보는 표본이
   "수집이 성공한 날"로 치우친다. VIX가 시장이 요동칠 때만 실패한다면 그 편향은
   결론을 뒤집는다. `value: null`로 남기고 이유도 함께 적는다.

2. **`as_of`와 관측일을 분리한다.** 오늘 받은 값이 3일 전 기준일 수 있다.
   둘을 합치면 **오래된 값이 오늘 값처럼 보인다** — 2026-08-31에 탭 E가
   커버리지 18%로 계산되던 것이 정확히 그 실수였다.

3. **하루 한 줄, 덧붙이기만 한다.** 같은 날 두 번 돌아도 덮어쓰지 않고 건너뛴다.
   과거 줄은 절대 고치지 않는다 — 나중에 "그때 무엇을 보고 있었나"를 알 수
   있어야 한다.

4. **가중치를 여기서 만들지 않는다.** 근거 없는 숫자를 하나 더 만드는 것이
   이 프로젝트에서 반복된 실패다(계획서 나우캐스팅 항목의 보류 사유와 같다).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("indicator_log")

LOG_NAME = "proxy_indicators.jsonl"


# ─── 순수 ────────────────────────────────────────────


def row_from_snapshot(snap: dict, day: str, *, recorded_at: str = "") -> dict:
    """스냅샷 → 하루치 한 줄(순수).

    **값이 없는 지표도 담는다.** 빠뜨리면 나중 표본이 '수집 성공한 날'로 치우친다.
    """
    items = []
    for it in (snap or {}).get("indicators", []) or []:
        items.append({
            "name": it.get("name"),
            "value": it.get("value"),
            "state": it.get("state"),
            "unit": it.get("unit") or "",
            # 지표가 말하는 기준일. 관측일(day)과 다를 수 있다 — 합치면 안 된다.
            "as_of": it.get("as_of"),
            "source": it.get("source") or "",
            "available": it.get("value") is not None,
        })
    summary = (snap or {}).get("summary") or {}
    return {
        "day": str(day),
        "recorded_at": recorded_at,
        "indicators": items,
        "n_available": summary.get("n_available"),
        "n_total": summary.get("n_total"),
        "lean": summary.get("lean"),
        "missing": summary.get("missing") or [],
        "missing_env": (snap or {}).get("missing_env") or [],
    }


def parse_jsonl(lines: Iterable[str]) -> list[dict]:
    """JSONL → 행 목록(순수). 깨진 줄은 건너뛰되 세어 둔다."""
    out, broken = [], 0
    for line in lines or []:
        line = (line or "").strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            broken += 1
            continue
        if isinstance(row, dict) and row.get("day"):
            out.append(row)
    if broken:
        log.warning("지표 로그에서 읽지 못한 줄 %d개 — 건너뜀", broken)
    return out


def has_day(rows: Iterable[dict], day: str) -> bool:
    return any(str((r or {}).get("day")) == str(day) for r in rows or [])


def series(rows: Iterable[dict], name: str) -> list[tuple]:
    """지표 하나의 시계열(순수) → [(day, value, as_of)].

    **값이 없는 날도 (day, None, as_of)로 남긴다.** 결측을 지우면 연속처럼 보인다.
    """
    out = []
    for row in rows or []:
        for it in (row or {}).get("indicators", []) or []:
            if it.get("name") == name:
                out.append((str(row.get("day")), it.get("value"), it.get("as_of")))
                break
    return out


def coverage(rows: Iterable[dict]) -> dict:
    """지표별 수집 성공률(순수).

    가중치를 정하기 전에 **각 지표가 얼마나 자주 비어 있는지**부터 봐야 한다.
    절반이 비는 지표에 가중치를 주는 것은 의미가 없다.
    """
    rows = list(rows or [])
    counts: dict = {}
    for row in rows:
        for it in (row or {}).get("indicators", []) or []:
            name = it.get("name")
            if not name:
                continue
            slot = counts.setdefault(name, {"n": 0, "available": 0, "stale": 0})
            slot["n"] += 1
            if it.get("value") is not None:
                slot["available"] += 1
                # 기준일이 관측일보다 오래됐으면 '지연'으로 센다
                as_of, day = str(it.get("as_of") or ""), str(row.get("day") or "")
                if as_of and day and as_of < day:
                    slot["stale"] += 1
    for slot in counts.values():
        slot["rate"] = round(slot["available"] / slot["n"] * 100, 1) if slot["n"] else 0.0
    return {"days": len(rows), "by_indicator": counts,
            "first": rows[0]["day"] if rows else None,
            "last": rows[-1]["day"] if rows else None}


def format_coverage(cov: dict) -> str:
    """수집 현황 한 눈에(순수). **가중치를 정할 수 있는 상태인지**를 말한다."""
    days = cov.get("days") or 0
    if not days:
        return "📉 지표 시계열: 아직 기록이 없습니다."
    lines = [f"📉 지표 시계열 {days}일 ({cov.get('first')} ~ {cov.get('last')})"]
    for name, s in sorted(cov.get("by_indicator", {}).items()):
        mark = "✅" if s["rate"] >= 90 else ("⚠️" if s["rate"] >= 50 else "❌")
        tail = f" · 기준일 지연 {s['stale']}일" if s.get("stale") else ""
        lines.append(f"  {mark} {name}: {s['available']}/{s['n']}일 "
                     f"({s['rate']}%){tail}")
    lines.append("")
    lines.append("_가중치는 아직 정하지 않습니다 — 지표가 국면을 앞서 맞히는지_")
    lines.append("_확인된 뒤에 얹습니다. 지금은 원자료만 쌓습니다._")
    return "\n".join(lines)


# ─── 경계 (I-O) ──────────────────────────────────────


def default_path() -> Path:
    try:
        from storage_paths import PATHS
        return Path(PATHS.private_state_file(LOG_NAME))
    except Exception:  # noqa: BLE001
        return (Path(__file__).resolve().parent.parent
                / "data" / "private" / "state" / LOG_NAME)


def load(path=None) -> list[dict]:
    p = Path(path) if path else default_path()
    try:
        return parse_jsonl(p.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("지표 로그 읽기 실패: %s", e)
        return []


def append(row: dict, path=None) -> bool:
    """한 줄 덧붙인다. 같은 날이 이미 있으면 **덮어쓰지 않고 건너뛴다.**"""
    p = Path(path) if path else default_path()
    if has_day(load(p), row.get("day")):
        log.debug("지표 로그: %s 이미 기록됨 — 건너뜀", row.get("day"))
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError as e:
        log.warning("지표 로그 저장 실패: %s", e)
        return False


def record(day: str, *, snapshot_fn=None, path=None,
           recorded_at: str = "") -> Optional[dict]:
    """오늘치 스냅샷을 적재한다. 이미 있으면 None."""
    if has_day(load(path), day):
        return None
    if snapshot_fn is None:
        import proxy_indicators
        snapshot_fn = proxy_indicators.snapshot
    snap = snapshot_fn()
    row = row_from_snapshot(snap, day, recorded_at=recorded_at)
    return row if append(row, path) else None


def _cli() -> int:
    import argparse
    from datetime import datetime

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--record", action="store_true", help="오늘치 적재")
    ap.add_argument("--day", help="기준일 YYYYMMDD (기본: 오늘)")
    ap.add_argument("--path", help="로그 파일 경로")
    args = ap.parse_args()

    day = args.day or datetime.now().strftime("%Y%m%d")
    if args.record:
        try:
            import env_config
            env_config.ensure_env(["ECOS_API_KEY", "FRED_API_KEY",
                                   "KRX_ID", "KRX_PW"])
        except ImportError:
            pass
        row = record(day, path=args.path,
                     recorded_at=datetime.now().isoformat(timespec="seconds"))
        if row is None:
            print(f"이미 기록됨: {day}")
        else:
            got = row["n_available"]
            print(f"기록 {day} — {got}/{row['n_total']}개 지표 확보"
                  + (f" · 미확보 {', '.join(row['missing'])}" if row["missing"] else ""))
    print()
    print(format_coverage(coverage(load(args.path))))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
