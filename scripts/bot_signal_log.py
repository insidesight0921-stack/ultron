"""bot_signal_log.py — 봇 추천을 **평가 가능한 형태로** 남긴다 (탭 B, v1).

**왜 필요한가**: 2026-09-02 실측으로 드러났다. 키움봇 주간 스캔은
`kium_weekly_YYYYMMDD.txt/.html`(사람이 읽는 리포트)만 남기고, 콴텍봇
월간 추천은 `quant_rebalance_last.json`에 **텔레그램 chat_id만** 남긴다.
종목도 점수도 어디에도 없다. 즉 **봇 신호 정확도는 잴 원자료가 없었다.**

기술적 신호는 `signal_log.jsonl`로 쌓이고 있어서 탭 B가 돌지만, 정작
매매를 만드는 봇의 판단은 남지 않았다. 지금 기록을 시작하지 않으면
반년 뒤에도 못 잰다 — `mode_log`·`entry_tags`와 같은 이유로 시계를 켠다.

설계 원칙:

1. **선정만 남기면 선정을 평가할 수 없다.** Top 10만 기록하면 "그 10개가
   좋았나"는 알아도 "고른 것이 값을 더했나"는 모른다. 순위 바로 아래
   같은 수를 **대조군**으로 함께 남긴다.
2. **대조군 크기를 미리 고정한다.** 나중에 "어디까지를 대조로 볼지" 고르면
   유리한 경계가 반드시 나온다. `CONTROL_MULTIPLE`로 못 박는다.
3. **신호 시점 가격을 함께 적는다.** 나중에 조회한 가격으로 기준을 삼으면
   그날 무엇을 보고 골랐는지 재현할 수 없다.
4. **같은 판단을 두 번 적지 않는다.** 같은 날·같은 봇·같은 종목은 한 줄이다
   (재실행해도 원장이 부풀지 않는다).
5. **어휘를 미리 고정한다.** `kind`는 선정/대조 둘뿐이다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("bot_signal_log")

LOG_NAME = "bot_signals.jsonl"
CONTROL_MULTIPLE = 1        # 선정 N개 → 대조군도 N개(바로 아래 순위)
SELECTED = "선정"
CONTROL = "대조"
KINDS = (SELECTED, CONTROL)


# ─── 순수 ────────────────────────────────────────────

def field(obj: Any, key: str, default=None):
    """dict·dataclass 양쪽에서 값 꺼내기(순수)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def control_size(top_n: int, multiple: int = CONTROL_MULTIPLE) -> int:
    """대조군 크기(순수). **미리 고정된 규칙이지 선택이 아니다.**"""
    return max(0, int(top_n) * max(0, int(multiple)))


def build_records(bot: str, ranked: Iterable, *, at: str, top_n: int,
                  price_of: Optional[Callable] = None,
                  score_of: Optional[Callable] = None,
                  extra_of: Optional[Callable] = None,
                  control_multiple: int = CONTROL_MULTIPLE,
                  **common) -> list[dict]:
    """점수 내림차순 후보 → 기록 레코드(순수).

    `ranked`는 **자르기 전** 전체 순위여야 한다. 자른 뒤 넘기면 대조군이
    사라지고, 그러면 「고른 것이 값을 더했나」를 영영 못 묻는다.
    """
    rows = list(ranked)
    keep = int(top_n) + control_size(top_n, control_multiple)
    out = []
    for i, rec in enumerate(rows[:keep], start=1):
        ticker = field(rec, "ticker")
        if not ticker:
            continue
        row = {
            "at": at, "bot": bot,
            "kind": SELECTED if i <= int(top_n) else CONTROL,
            "rank": i, "top_n": int(top_n),
            "universe_n": len(rows),
            "ticker": str(ticker),
            "name": field(rec, "name"),
            "score": (score_of or (lambda r: field(r, "score")))(rec),
            "price": (price_of or (lambda r: field(r, "current_price")))(rec),
        }
        row.update({k: v for k, v in common.items() if v is not None})
        if extra_of:
            extra = extra_of(rec)
            if extra:
                row["extra"] = extra
        out.append(row)
    return out


def record_key(rec: dict) -> str:
    """같은 판단인지 가르는 열쇠(순수) — 날·봇·종목·**top_n·국면**.

    리뷰(2026-09-02): 날·봇·종목만 보면 같은 날 `/test_quant`(또는 --top을
    바꾼 재실행)가 먼저 돌았을 때 그날 진짜 월간 판단이 0건 기록되고 먼저
    남은 행의 top_n·국면이 그날 판단으로 남는다. 판단 조건이 다르면 다른
    판단이다 — 같은 조건의 재실행만 한 줄로 합친다.
    """
    day = str(rec.get("at") or "")[:10]
    return (f"{day}|{rec.get('bot')}|{rec.get('ticker')}"
            f"|{rec.get('top_n')}|{rec.get('phase') or ''}")


def merge_records(existing: Iterable[dict], new: Iterable[dict]) -> list[dict]:
    """중복을 빼고 이어 붙인다(순수). **기존 줄은 고치지 않는다.**"""
    out = list(existing)
    seen = {record_key(r) for r in out}
    for rec in new:
        key = record_key(rec)
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def summarize_records(records: Iterable[dict]) -> dict:
    """무엇이 얼마나 쌓였나(순수) — 화면·CLI가 표본을 먼저 보게."""
    rows = list(records)
    bots: dict = {}
    for r in rows:
        b = bots.setdefault(r.get("bot") or "?", {"선정": 0, "대조": 0, "days": set()})
        if r.get("kind") in KINDS:
            b[r["kind"]] += 1
        day = str(r.get("at") or "")[:10]
        if day:
            b["days"].add(day)
    return {bot: {"선정": v["선정"], "대조": v["대조"], "days": len(v["days"])}
            for bot, v in sorted(bots.items())}


# ─── I/O ─────────────────────────────────────────────

def default_path(state_dir=None) -> Path:
    if state_dir is None:
        try:
            from storage_paths import PATHS
            return PATHS.private_state_file(LOG_NAME)
        except Exception:
            state_dir = Path(__file__).resolve().parents[1] / "data" / "private" / "state"
    return Path(state_dir) / LOG_NAME


def load_records(path=None) -> list[dict]:
    path = Path(path or default_path())
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue          # 깨진 줄은 건너뛴다 — 나머지는 살린다
    except OSError:
        return []
    return out


def append_records(records: Iterable[dict], path=None) -> int:
    """새 줄만 덧붙인다. **기록 실패가 봇을 멈추게 하지 않는다.**"""
    records = list(records)
    if not records:
        return 0
    path = Path(path or default_path())
    try:
        existing = load_records(path)
        merged = merge_records(existing, records)
        added = merged[len(existing):]
        if not added:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for rec in added:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return len(added)
    except Exception as e:                      # noqa: BLE001
        log.warning(f"봇 신호 기록 실패(무시하고 계속): {e}")
        return 0


def log_ranked(bot: str, ranked, *, at: str, top_n: int, path=None,
               **kw) -> int:
    """편의 함수 — 만들고 붙이기까지."""
    return append_records(build_records(bot, ranked, at=at, top_n=top_n, **kw),
                          path=path)
