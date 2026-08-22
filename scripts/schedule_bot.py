#!/usr/bin/env python3
"""
일정봇 (schedule_bot) — SQLite 자체 관리 일정 등록/조회/삭제.

4단계 두 번째 도구. 라우터(router.py)가 자연어를 분석해 (action, title,
when_at) 파라미터를 채워 호출하면, 이 모듈은 단순 실행자 역할만 한다.

주요 설계 결정:
- 백엔드: SQLite (``storage_paths``가 선택한 Private DB). macOS 비의존, 텔레그램·웹·CLI
  공용. 백업/마이그레이션 단순. (Reminders.app 백엔드는 osascript 오버헤드
  + 자연어 파싱 부담 → 보류.)
- 자연어 일시(예: "다음주 수요일 오후 3시")는 라우터(Gemma 4 26B)가 ISO 8601
  로 변환해서 넘김. 여기서는 받은 ISO 문자열을 검증·저장만.
- (answer, sources) 인터페이스는 knowledge_bot과 동일. sources는 항상 빈
  리스트(일정은 RAG 출처 개념 없음).

API:
  add_event(title, when_at, notes=None, chat_id=None) -> int
  list_events(upcoming_only=True, limit=20, chat_id=None) -> list[dict]
  get_event(event_id) -> dict | None
  delete_event(event_id) -> bool
  mark_completed(event_id) -> bool
  run(action, **args) -> tuple[str, list[dict]]   # 라우터 entrypoint

스키마(schedule.db):
  CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    when_at     TEXT NOT NULL,          -- ISO 8601 (YYYY-MM-DDTHH:MM:SS)
    notes       TEXT,
    chat_id     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    completed   INTEGER NOT NULL DEFAULT 0
  );
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Iterator

from dateutil.rrule import (
    rrule, DAILY, WEEKLY, MONTHLY,
    MO, TU, WE, TH, FR, SA, SU,
)

from storage_paths import PATHS

HOME = Path.home()
PROJECT = HOME / "울트론" / "ai-agent"
DEFAULT_DB_PATH = PATHS.schedule_db

log = logging.getLogger("schedule_bot")

# 동시성 — telegram 핸들러는 asyncio + to_thread를 사용하므로
# SQLite 자체 락으로 충분하지만 안전을 위해 모듈 락도 둠.
_LOCK = Lock()


# ─── 충돌 감지 (v3.14) ─────────────────────────────────
# add 시 같은 chat_id 안의 ±N분 윈도우에 다른 미완료 일정이 있으면 경고.
# 차단은 하지 않음 (Warning + 등록 진행). 사용자 결정 (2026-05-08).
DEFAULT_CONFLICT_WINDOW_MINUTES = 15


# ─── 반복 일정 (RRULE) 헬퍼 ───────────────────────────


# 단순화 모델: freq + byday(weekly만) + until.
# 풀 RFC 5545 RRULE을 다 받지 않고 라우터(26B)가 만들기 쉬운 enum 형태로 제한.
VALID_FREQS = {"daily", "weekly", "monthly"}
_FREQ_MAP = {"daily": DAILY, "weekly": WEEKLY, "monthly": MONTHLY}
_BYDAY_MAP = {"MO": MO, "TU": TU, "WE": WE, "TH": TH, "FR": FR, "SA": SA, "SU": SU}


def parse_byday(byday: str | None) -> list | None:
    """'MO,WE,FR' → [MO, WE, FR]. 빈/None → None."""
    if not byday:
        return None
    out = []
    for token in str(byday).split(","):
        token = token.strip().upper()
        if token in _BYDAY_MAP:
            out.append(_BYDAY_MAP[token])
    return out or None


def next_occurrence(
    when_at: str,
    rrule_freq: str | None,
    rrule_byday: str | None = None,
    rrule_until: str | None = None,
    after: datetime | None = None,
) -> str | None:
    """현재 일정 시각(when_at) + RRULE 정의 → 다음 발화 시각 ISO 8601.

    after: 이 시각 이후의 발화만. 기본은 when_at 다음 1초.
    None 반환 = 더 이상 반복 없음 (rrule_until 도달 또는 freq 없음).
    """
    if not rrule_freq:
        return None
    freq = (rrule_freq or "").strip().lower()
    if freq not in _FREQ_MAP:
        return None

    try:
        dtstart = datetime.fromisoformat(when_at)
    except ValueError:
        return None

    until = None
    if rrule_until:
        u = parse_when(rrule_until)
        if u:
            try:
                until = datetime.fromisoformat(u)
            except ValueError:
                pass

    byweekday = parse_byday(rrule_byday) if freq == "weekly" else None
    base_after = after if after is not None else dtstart

    rule = rrule(
        freq=_FREQ_MAP[freq],
        dtstart=dtstart,
        until=until,
        byweekday=byweekday,
    )
    nxt = rule.after(base_after, inc=False)
    if nxt is None:
        return None
    if until and nxt > until:
        return None
    return nxt.strftime("%Y-%m-%dT%H:%M:%S")


# ─── 일시 파싱/검증 ──────────────────────────────────


def parse_when(raw: str) -> str | None:
    """ISO 8601 류 문자열을 검증·정규화. 실패 시 None.

    허용 포맷:
      2026-05-08T15:00:00
      2026-05-08T15:00
      2026-05-08 15:00:00
      2026-05-08 15:00
      2026-05-08              (자정으로 처리)
      2026-05-08T15:00:00+09:00  (타임존 포함, naive로 변환)

    저장 포맷은 항상 'YYYY-MM-DDTHH:MM:SS' (로컬 naive). 라우터가 KST 기준
    문자열을 보내준다고 가정.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().replace("Z", "+00:00")
    # 'YYYY-MM-DD HH:MM' → 'YYYY-MM-DDTHH:MM'
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)

    # date-only
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            d = datetime.strptime(s, "%Y-%m-%d")
            return d.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None

    # fromisoformat은 'YYYY-MM-DDTHH:MM' 도 받음 (Py3.11+)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None

    if dt.tzinfo is not None:
        # 타임존 정보가 있으면 로컬 시각으로 변환 후 naive화
        dt = dt.astimezone(tz=None).replace(tzinfo=None)

    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def fmt_when(iso: str) -> str:
    """ISO 8601 → 사람 친화 한국어 표기."""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    weekday = ["월", "화", "수", "목", "금", "토", "일"][dt.weekday()]
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return f"{dt.year}-{dt.month:02d}-{dt.day:02d} ({weekday})"
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d} ({weekday}) {dt.hour:02d}:{dt.minute:02d}"


# ─── DB 헬퍼 ─────────────────────────────────────────


@contextmanager
def _conn(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        _ensure_schema(con)
        yield con
    finally:
        con.close()


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            when_at     TEXT NOT NULL,
            notes       TEXT,
            chat_id     TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            completed   INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_when ON events(when_at);")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_chat ON events(chat_id, when_at);"
    )
    # notified 컬럼 마이그레이션 (v3.6 추가) — IF NOT EXISTS 미지원이므로
    # PRAGMA table_info로 존재 여부 확인 후 ALTER TABLE.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(events)").fetchall()}
    if "notified" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_notify "
            "ON events(notified, when_at) WHERE notified = 0"
        )
    # RRULE + pre_notify 마이그레이션 (v3.9 추가)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(events)").fetchall()}
    if "rrule_freq" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN rrule_freq TEXT")
        con.execute("ALTER TABLE events ADD COLUMN rrule_byday TEXT")
        con.execute("ALTER TABLE events ADD COLUMN rrule_until TEXT")
    if "pre_notify_minutes" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN pre_notify_minutes INTEGER NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE events ADD COLUMN pre_notified INTEGER NOT NULL DEFAULT 0")
    # 멀티 사전알림 (v3.15) — 별도 정규화 테이블.
    # events.pre_notify_minutes/pre_notified는 단일 알림 시절 호환을 위해 유지
    # (최대값 caching + legacy 표시). 신규 코드는 모두 event_pre_notifications 사용.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS event_pre_notifications (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        INTEGER NOT NULL,
            minutes_before  INTEGER NOT NULL,
            notified        INTEGER NOT NULL DEFAULT 0,
            UNIQUE(event_id, minutes_before),
            FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_pn_event ON event_pre_notifications(event_id)")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_pn_pending "
        "ON event_pre_notifications(notified, minutes_before) WHERE notified = 0"
    )
    # 1회 backfill — 기존 단일 pre_notify_minutes를 새 테이블로 옮김 (멱등).
    # INSERT OR IGNORE → UNIQUE 충돌 무시. 이미 마이그레이션 끝났으면 0건 영향.
    con.execute(
        "INSERT OR IGNORE INTO event_pre_notifications "
        "(event_id, minutes_before, notified) "
        "SELECT id, pre_notify_minutes, pre_notified FROM events "
        "WHERE pre_notify_minutes >= 1"
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    keys = row.keys() if hasattr(row, "keys") else []
    def _g(k, default=None):
        return row[k] if k in keys else default
    return {
        "id": row["id"],
        "title": row["title"],
        "when_at": row["when_at"],
        "when_pretty": fmt_when(row["when_at"]),
        "notes": row["notes"],
        "chat_id": row["chat_id"],
        "created_at": row["created_at"],
        "completed": bool(row["completed"]),
        "notified": bool(_g("notified", 0)),
        "rrule_freq": _g("rrule_freq"),
        "rrule_byday": _g("rrule_byday"),
        "rrule_until": _g("rrule_until"),
        "pre_notify_minutes": int(_g("pre_notify_minutes", 0) or 0),
        "pre_notified": bool(_g("pre_notified", 0)),
        # v3.15: 멀티 사전알림 list — 호출 측이 _attach_pre_alerts() 적용해 채움
        "pre_notify_minutes_list": [],
    }


def _attach_pre_alerts(con: sqlite3.Connection, ev: dict) -> dict:
    """ev dict에 pre_notify_minutes_list 채워서 반환. 호출 측이 con 보유 시 사용."""
    eid = ev.get("id")
    if eid is None:
        return ev
    rows = con.execute(
        "SELECT minutes_before FROM event_pre_notifications "
        "WHERE event_id = ? ORDER BY minutes_before DESC",
        (int(eid),),
    ).fetchall()
    ev["pre_notify_minutes_list"] = [int(r["minutes_before"]) for r in rows]
    return ev


# ─── 충돌 감지 함수 (v3.14) ──────────────────────────


def find_conflicts(
    when_at: str,
    chat_id: str | None,
    *,
    window_minutes: int = DEFAULT_CONFLICT_WINDOW_MINUTES,
    exclude_event_id: int | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict]:
    """주어진 시각 ±window_minutes 윈도우 안의 같은 chat_id 미완료 일정 반환.

    설계 결정:
      - 같은 chat_id 안에서만 검사 (1인 운용 가정 → 다른 chat의 일정은 무관).
      - chat_id가 없으면 항상 빈 리스트 (격리 의미 없음 → 검사 skip).
      - completed=0 인 것만 (지난 완료 일정과는 충돌 없음).
      - window_minutes <= 0 이면 빈 리스트 (검사 비활성화).
      - 반복 일정은 메인 when_at만 검사 (RRULE occurrence는 펼치지 않음).
      - exclude_event_id: 자기 자신 update 시 제외.

    호출 측이 결과를 보고 "Warning + 등록 진행" 또는 차단 등 정책 결정.
    """
    if window_minutes is None or int(window_minutes) <= 0:
        return []
    if not chat_id:
        return []
    iso = parse_when(when_at)
    if iso is None:
        return []

    from datetime import timedelta as _td
    target = datetime.fromisoformat(iso)
    lo = (target - _td(minutes=int(window_minutes))).strftime("%Y-%m-%dT%H:%M:%S")
    hi = (target + _td(minutes=int(window_minutes))).strftime("%Y-%m-%dT%H:%M:%S")

    sql = (
        "SELECT * FROM events "
        "WHERE chat_id = ? AND completed = 0 "
        "AND when_at >= ? AND when_at <= ?"
    )
    params: list = [str(chat_id), lo, hi]
    if exclude_event_id is not None:
        sql += " AND id != ?"
        params.append(int(exclude_event_id))
    sql += " ORDER BY when_at ASC"

    with _conn(db_path) as con:
        rows = con.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


# ─── 공개 CRUD API ───────────────────────────────────


def add_event(
    title: str,
    when_at: str,
    notes: str | None = None,
    chat_id: str | None = None,
    rrule_freq: str | None = None,
    rrule_byday: str | None = None,
    rrule_until: str | None = None,
    pre_notify_minutes: int = 0,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """일정 등록 → event_id 반환. when_at 파싱 실패 시 ValueError.

    반복 옵션:
      rrule_freq ∈ {"daily", "weekly", "monthly"}
      rrule_byday "MO,WE,FR" (weekly만 의미)
      rrule_until ISO 8601 종료일 (선택)

    사전 알림:
      pre_notify_minutes ≥ 1 이면 (when_at - X분) 시점에 사전 알림 발송.
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("title이 비어있습니다")

    iso = parse_when(when_at)
    if iso is None:
        raise ValueError(f"when_at 파싱 실패: {when_at!r}")

    if rrule_freq is not None:
        rrule_freq = rrule_freq.strip().lower() or None
        if rrule_freq and rrule_freq not in VALID_FREQS:
            raise ValueError(f"rrule_freq 허용값 외: {rrule_freq!r} (daily/weekly/monthly)")

    until_iso = None
    if rrule_until:
        until_iso = parse_when(rrule_until)
        if until_iso is None:
            raise ValueError(f"rrule_until 파싱 실패: {rrule_until!r}")

    # v3.15: pre_notify_minutes가 int 또는 list[int] 모두 허용.
    # 정규화 — 음수/0/중복 제거하고 unique 양수 list로.
    pn_list = _normalize_pre_notify_minutes(pre_notify_minutes)
    pn_legacy = max(pn_list) if pn_list else 0  # events.pre_notify_minutes (legacy 표시용)

    with _LOCK, _conn(db_path) as con:
        cur = con.execute(
            "INSERT INTO events("
            "title, when_at, notes, chat_id, "
            "rrule_freq, rrule_byday, rrule_until, pre_notify_minutes"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, iso, notes, chat_id,
             rrule_freq, rrule_byday, until_iso, pn_legacy),
        )
        event_id = cur.lastrowid
        # 멀티 사전알림 — 각 alert를 별도 row로 (UNIQUE 보장).
        for m in pn_list:
            con.execute(
                "INSERT OR IGNORE INTO event_pre_notifications "
                "(event_id, minutes_before, notified) VALUES (?, ?, 0)",
                (int(event_id), int(m)),
            )
    log.info(
        f"📅 일정 등록 #{event_id}: {title} @ {iso}"
        f"{' [' + rrule_freq + ']' if rrule_freq else ''}"
        f"{f' (사전알림 ' + str(pn_list) + '분)' if pn_list else ''}"
    )
    return int(event_id)


def _normalize_pre_notify_minutes(value) -> list[int]:
    """int 또는 list[int|str]을 unique 양수 int list로 정규화. 내림차순 정렬.

    예:
      None → []
      0 → []
      5 → [5]
      [5, 30] → [30, 5]
      [5, 5, "30", -1, 0] → [30, 5]  (중복·음수·0 제거)
    """
    if value is None:
        return []
    if isinstance(value, (int, float, str)):
        try:
            v = int(value)
            return [v] if v >= 1 else []
        except (TypeError, ValueError):
            return []
    if isinstance(value, (list, tuple, set)):
        out = set()
        for x in value:
            try:
                v = int(x)
                if v >= 1:
                    out.add(v)
            except (TypeError, ValueError):
                continue
        return sorted(out, reverse=True)  # 큰 값(먼저 알림)이 앞
    return []


def get_event(event_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> dict | None:
    with _conn(db_path) as con:
        row = con.execute(
            "SELECT * FROM events WHERE id = ?", (int(event_id),)
        ).fetchone()
        if not row:
            return None
        ev = _row_to_dict(row)
        return _attach_pre_alerts(con, ev)


def list_events(
    upcoming_only: bool = True,
    limit: int = 20,
    chat_id: str | None = None,
    include_completed: bool = False,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict]:
    """가까운 일정부터 정렬해서 반환.

    upcoming_only=True면 현재 시각 이후만. 같은 chat_id의 일정만 보고 싶으면
    chat_id 지정.
    """
    where = []
    params: list = []
    if upcoming_only:
        where.append("when_at >= ?")
        params.append(datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    if chat_id is not None:
        where.append("chat_id = ?")
        params.append(chat_id)
    if not include_completed:
        where.append("completed = 0")

    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY when_at ASC LIMIT ?"
    params.append(int(limit))

    with _conn(db_path) as con:
        rows = con.execute(sql, params).fetchall()
        return [_attach_pre_alerts(con, _row_to_dict(r)) for r in rows]


def delete_event(event_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    with _LOCK, _conn(db_path) as con:
        # v3.15: FK ON DELETE CASCADE는 PRAGMA에 의존. 안전하게 명시 DELETE.
        con.execute(
            "DELETE FROM event_pre_notifications WHERE event_id = ?",
            (int(event_id),),
        )
        cur = con.execute("DELETE FROM events WHERE id = ?", (int(event_id),))
        deleted = cur.rowcount > 0
    if deleted:
        log.info(f"🗑  일정 삭제 #{event_id}")
    return deleted


def mark_completed(event_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    with _LOCK, _conn(db_path) as con:
        cur = con.execute(
            "UPDATE events SET completed = 1 WHERE id = ?", (int(event_id),)
        )
        updated = cur.rowcount > 0
    if updated:
        log.info(f"✅ 일정 완료 #{event_id}")
    return updated


# ─── 라우터 entrypoint ───────────────────────────────


def _format_list(events: list[dict], header: str = "📅 일정") -> str:
    if not events:
        return f"{header}\n(없음)"
    lines = [header]
    for e in events:
        flag = "✅ " if e["completed"] else ""
        rep = ""
        if e.get("rrule_freq"):
            rep = f" 🔁{e['rrule_freq']}"
            if e.get("rrule_byday"):
                rep += f"({e['rrule_byday']})"
        pre = f" 🔔-{e['pre_notify_minutes']}분" if e.get("pre_notify_minutes") else ""
        line = f"#{e['id']} {flag}{e['title']} — {e['when_pretty']}{rep}{pre}"
        if e["notes"]:
            line += f" ({e['notes']})"
        lines.append(line)
    return "\n".join(lines)


def run(
    action: str,
    title: str | None = None,
    when_at: str | None = None,
    event_id: int | None = None,
    notes: str | None = None,
    chat_id: str | None = None,
    limit: int = 20,
    db_path: Path | str = DEFAULT_DB_PATH,
    **kwargs,
) -> tuple[str, list[dict]]:
    """라우터에서 호출하는 단일 entrypoint.

    반환: (사용자 표시용 텍스트, 빈 sources 리스트)
    knowledge_bot과 동일한 (answer, chunks) 인터페이스.
    """
    action = (action or "").strip().lower()

    try:
        if action == "add":
            if not title or not when_at:
                return ("❌ 일정 등록에는 제목과 일시가 필요합니다.", [])
            # v3.14: 충돌 감지 — 등록 전 ±window 분 안의 같은 chat_id 미완료 일정 조회.
            # 라우터가 conflict_window_minutes 안 주면 DEFAULT(15분). 0/음수면 검사 skip.
            try:
                conflict_window = int(
                    kwargs.get("conflict_window_minutes")
                    if kwargs.get("conflict_window_minutes") is not None
                    else DEFAULT_CONFLICT_WINDOW_MINUTES
                )
            except (TypeError, ValueError):
                conflict_window = DEFAULT_CONFLICT_WINDOW_MINUTES
            conflicts = find_conflicts(
                when_at, chat_id,
                window_minutes=conflict_window,
                db_path=db_path,
            )
            try:
                eid = add_event(
                    title, when_at, notes=notes, chat_id=chat_id,
                    rrule_freq=kwargs.get("rrule_freq"),
                    rrule_byday=kwargs.get("rrule_byday"),
                    rrule_until=kwargs.get("rrule_until"),
                    pre_notify_minutes=kwargs.get("pre_notify_minutes"),
                    db_path=db_path,
                )
            except ValueError as e:
                return (f"❌ 등록 실패: {e}", [])
            ev = get_event(eid, db_path=db_path)
            extra_lines = []
            if ev.get("rrule_freq"):
                rep = ev["rrule_freq"]
                if ev.get("rrule_byday"):
                    rep += f" ({ev['rrule_byday']})"
                if ev.get("rrule_until"):
                    rep += f" until {ev['rrule_until']}"
                extra_lines.append(f"🔁 반복: {rep}")
            # v3.15: pre_notify_minutes_list가 있으면 우선 표시 (멀티), 없으면 단일값
            pn_list = ev.get("pre_notify_minutes_list") or []
            if pn_list:
                if len(pn_list) == 1:
                    extra_lines.append(f"🔔 사전 알림: {pn_list[0]}분 전")
                else:
                    parts = ", ".join(f"{m}분 전" for m in pn_list)
                    extra_lines.append(f"🔔 사전 알림 ({len(pn_list)}회): {parts}")
            elif ev.get("pre_notify_minutes"):
                extra_lines.append(f"🔔 사전 알림: {ev['pre_notify_minutes']}분 전")
            # 충돌 경고 — Warning + 등록 진행 (사용자 결정). 차단 안 함.
            if conflicts:
                extra_lines.append(
                    f"⚠️ 충돌 감지 — ±{conflict_window}분 안에 다른 일정 {len(conflicts)}건:"
                )
                for c in conflicts:
                    extra_lines.append(f"  • #{c['id']} {c['title']} — {c['when_pretty']}")
            return (
                f"✅ 일정 등록 #{eid}\n"
                f"📌 {ev['title']}\n"
                f"📅 {ev['when_pretty']}"
                + (f"\n📝 {ev['notes']}" if ev["notes"] else "")
                + ("\n" + "\n".join(extra_lines) if extra_lines else ""),
                [],
            )

        if action in ("list", "upcoming"):
            events = list_events(
                upcoming_only=(action == "upcoming"),
                limit=limit,
                chat_id=chat_id,
                db_path=db_path,
            )
            header = "📅 다가오는 일정" if action == "upcoming" else "📅 전체 일정"
            return (_format_list(events, header), [])

        if action == "delete":
            if event_id is None:
                return ("❌ 삭제할 일정 번호(event_id)를 알려주세요. 예: '#3 삭제해줘'", [])
            ok = delete_event(int(event_id), db_path=db_path)
            return (
                f"🗑 일정 #{event_id} 삭제 완료" if ok else f"⚠️ #{event_id} 일정을 찾지 못했습니다.",
                [],
            )

        if action == "complete":
            if event_id is None:
                return ("❌ 완료 처리할 일정 번호(event_id)를 알려주세요.", [])
            ok = mark_completed(int(event_id), db_path=db_path)
            return (
                f"✅ 일정 #{event_id} 완료 표시" if ok else f"⚠️ #{event_id} 일정을 찾지 못했습니다.",
                [],
            )

        return (f"❌ 알 수 없는 action: {action!r} (add/list/upcoming/delete/complete)", [])

    except Exception as e:
        log.exception("schedule_bot.run 실패")
        return (f"❌ 일정봇 오류: {e}", [])





# ─── 알림 스케줄러용 API (v3.6) ──────────────────────


def due_for_notification(
    *,
    horizon_seconds: int = 70,
    grace_seconds: int = 12 * 60 * 60,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict]:
    """발화 임박/기한 도래한 일정 (메인 + 사전) 반환.

    각 dict에 'kind' ∈ {'main', 'pre'} 필드 포함. 호출 측이 메시지 분기.

    윈도우:
      main: now - grace ≤ when_at ≤ now + horizon, notified=0
      pre:  now - grace ≤ (when_at - pre_notify_minutes*60) ≤ now + horizon,
            pre_notified=0, pre_notify_minutes ≥ 1
    완료/no chat_id는 제외.
    """
    from datetime import datetime, timedelta as _td

    now = datetime.now()
    lo = (now - _td(seconds=grace_seconds)).strftime("%Y-%m-%dT%H:%M:%S")
    hi = (now + _td(seconds=horizon_seconds)).strftime("%Y-%m-%dT%H:%M:%S")

    out: list[dict] = []
    with _conn(db_path) as con:
        # 메인 알림
        for r in con.execute(
            "SELECT * FROM events "
            "WHERE notified = 0 AND completed = 0 "
            "AND chat_id IS NOT NULL AND chat_id != '' "
            "AND when_at >= ? AND when_at <= ? "
            "ORDER BY when_at ASC",
            (lo, hi),
        ).fetchall():
            d = _row_to_dict(r)
            d = _attach_pre_alerts(con, d)
            d["kind"] = "main"
            out.append(d)
        # v3.15: 멀티 사전알림 — event_pre_notifications JOIN으로 각 알림을 별도 row로.
        # pn.notified=0, e.completed=0, e.chat_id 유효, pn.minutes_before>=1,
        # (e.when_at - pn.minutes_before*60s) ∈ [lo, hi]
        for r in con.execute(
            "SELECT e.*, "
            "  pn.id AS pn_id, pn.minutes_before AS pn_minutes_before, "
            "  strftime('%Y-%m-%dT%H:%M:%S', e.when_at, "
            "           '-' || pn.minutes_before || ' minutes') AS pre_at "
            "FROM events e "
            "JOIN event_pre_notifications pn ON pn.event_id = e.id "
            "WHERE pn.notified = 0 AND e.completed = 0 "
            "AND pn.minutes_before >= 1 "
            "AND e.chat_id IS NOT NULL AND e.chat_id != '' "
            "AND strftime('%Y-%m-%dT%H:%M:%S', e.when_at, "
            "             '-' || pn.minutes_before || ' minutes') >= ? "
            "AND strftime('%Y-%m-%dT%H:%M:%S', e.when_at, "
            "             '-' || pn.minutes_before || ' minutes') <= ? "
            "ORDER BY pre_at ASC",
            (lo, hi),
        ).fetchall():
            d = _row_to_dict(r)
            d = _attach_pre_alerts(con, d)
            d["kind"] = "pre"
            d["pre_at"] = r["pre_at"]
            d["minutes_before"] = int(r["pn_minutes_before"])
            out.append(d)
    return out


def mark_pre_notified(
    event_id: int,
    minutes_before: int | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> bool:
    """v3.15: minutes_before 명시 시 그 alert만 마킹. None이면 모든 alert + legacy column.

    BC: 기존 mark_pre_notified(eid) 호출은 모든 alert를 마킹 (단일 알림 시절 동등).
    """
    with _LOCK, _conn(db_path) as con:
        if minutes_before is None:
            # 전체 마킹 — legacy column + 모든 새 테이블 row
            cur1 = con.execute(
                "UPDATE events SET pre_notified = 1 WHERE id = ?", (int(event_id),)
            )
            cur2 = con.execute(
                "UPDATE event_pre_notifications SET notified = 1 WHERE event_id = ?",
                (int(event_id),),
            )
            updated = cur1.rowcount > 0 or cur2.rowcount > 0
        else:
            # 특정 alert만 — 새 테이블 그 row만
            cur = con.execute(
                "UPDATE event_pre_notifications SET notified = 1 "
                "WHERE event_id = ? AND minutes_before = ?",
                (int(event_id), int(minutes_before)),
            )
            updated = cur.rowcount > 0
            # legacy column은 모든 alert 다 notified일 때만 1로 (멱등 일관성)
            if updated:
                pending = con.execute(
                    "SELECT COUNT(*) AS c FROM event_pre_notifications "
                    "WHERE event_id = ? AND notified = 0",
                    (int(event_id),),
                ).fetchone()
                if pending and int(pending["c"]) == 0:
                    con.execute(
                        "UPDATE events SET pre_notified = 1 WHERE id = ?",
                        (int(event_id),),
                    )
    if updated:
        suffix = f" {minutes_before}분-전" if minutes_before is not None else ""
        log.info(f"🔔 사전 알림 표시 #{event_id}{suffix}")
    return updated


def advance_recurring(event_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> str | None:
    """반복 일정의 when_at을 다음 발화 시각으로 갱신 + notified/pre_notified 리셋.

    반환: 새 when_at ISO. 더 이상 반복 없으면(rrule_until 도달 등) None
          (이때는 row를 그대로 두되 마크만 1로 유지 → 더 이상 발화 안 됨).
    """
    with _LOCK, _conn(db_path) as con:
        row = con.execute("SELECT * FROM events WHERE id = ?", (int(event_id),)).fetchone()
        if row is None:
            return None
        nxt = next_occurrence(
            row["when_at"],
            row["rrule_freq"],
            row["rrule_byday"],
            row["rrule_until"],
        )
        if nxt is None:
            return None
        con.execute(
            "UPDATE events SET when_at = ?, notified = 0, pre_notified = 0 WHERE id = ?",
            (nxt, int(event_id)),
        )
        # v3.15: 새 테이블의 모든 alert도 notified=0 리셋 (다음 occurrence에서 재발화)
        con.execute(
            "UPDATE event_pre_notifications SET notified = 0 WHERE event_id = ?",
            (int(event_id),),
        )
    log.info(f"🔁 반복 일정 #{event_id} 다음 발화: {nxt}")
    return nxt


def mark_notified(event_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    """알림 발송 후 호출 — 멱등성 보장 (같은 이벤트 두 번 알림 방지)."""
    with _LOCK, _conn(db_path) as con:
        cur = con.execute(
            "UPDATE events SET notified = 1 WHERE id = ?", (int(event_id),)
        )
        updated = cur.rowcount > 0
    if updated:
        log.info(f"🔔 알림 발송 표시 #{event_id}")
    return updated


# ─── CLI 테스트 ──────────────────────────────────────


def _cli() -> None:
    import argparse
    import json as _json

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("title")
    p_add.add_argument("when")
    p_add.add_argument("--notes")

    sub.add_parser("list")
    sub.add_parser("upcoming")

    p_del = sub.add_parser("delete")
    p_del.add_argument("event_id", type=int)

    p_done = sub.add_parser("complete")
    p_done.add_argument("event_id", type=int)

    p_run = sub.add_parser("run", help="라우터 entrypoint 시뮬레이션 (JSON 인자)")
    p_run.add_argument("payload", help='예: {"action":"add","title":"...","when_at":"..."}')

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "add":
        eid = add_event(args.title, args.when, notes=args.notes)
        print(f"등록됨 #{eid}")
    elif args.cmd in ("list", "upcoming"):
        msg, _ = run(args.cmd)
        print(msg)
    elif args.cmd == "delete":
        ok = delete_event(args.event_id)
        print("삭제됨" if ok else "없음")
    elif args.cmd == "complete":
        ok = mark_completed(args.event_id)
        print("완료" if ok else "없음")
    elif args.cmd == "run":
        payload = _json.loads(args.payload)
        msg, _ = run(**payload)
        print(msg)


if __name__ == "__main__":
    _cli()
