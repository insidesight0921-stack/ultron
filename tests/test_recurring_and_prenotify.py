"""
v3.9 — 반복 일정(RRULE) + 사전 알림(pre_notify) 테스트.

순수 SQLite + dateutil.rrule. LLM 무관.
검증:
  - parse_byday: "MO,WE,FR" → 3개 weekday
  - next_occurrence: daily/weekly/monthly + byday + until
  - add_event: rrule_freq + rrule_byday + rrule_until + pre_notify_minutes 저장
  - due_for_notification: pre 분리 윈도우 + main 윈도우, kind 필드
  - mark_pre_notified 멱등
  - advance_recurring: when_at 다음 시각으로 갱신 + flag 리셋
  - 반복 종료(until 도달) → advance_recurring None
  - 라우터 _validate_schedule_args의 RRULE/pre 인자
"""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import schedule_bot as sb
import router


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "rrule.db"


def _iso(offset_seconds: int = 0) -> str:
    return (datetime.now() + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%S")


# ─── parse_byday ─────────────────────────────────────


def test_parse_byday_normal():
    res = sb.parse_byday("MO,WE,FR")
    assert res is not None
    assert len(res) == 3


def test_parse_byday_lowercase_accepted():
    """parse_byday는 .upper() 적용하므로 소문자도 받음 (라우터 친화적)."""
    res = sb.parse_byday("mo,we")
    assert res is not None
    assert len(res) == 2


def test_parse_byday_empty():
    assert sb.parse_byday("") is None
    assert sb.parse_byday(None) is None


def test_parse_byday_invalid_tokens():
    res = sb.parse_byday("MO,XX,FR")
    # XX 무시, MO/FR만
    assert res is not None
    assert len(res) == 2


# ─── next_occurrence ─────────────────────────────────


def test_next_occurrence_daily():
    nxt = sb.next_occurrence("2026-05-01T09:00:00", "daily")
    assert nxt == "2026-05-02T09:00:00"


def test_next_occurrence_weekly_no_byday():
    # 매주 금요일 (2026-05-01은 금) → 다음 금요일
    nxt = sb.next_occurrence("2026-05-01T09:00:00", "weekly")
    assert nxt == "2026-05-08T09:00:00"


def test_next_occurrence_weekly_with_byday():
    # 2026-05-04(월). MO,WE,FR 반복 → 다음 발화는 2026-05-06(수)
    nxt = sb.next_occurrence("2026-05-04T09:00:00", "weekly", "MO,WE,FR")
    assert nxt == "2026-05-06T09:00:00"


def test_next_occurrence_monthly():
    nxt = sb.next_occurrence("2026-05-01T09:00:00", "monthly")
    assert nxt == "2026-06-01T09:00:00"


def test_next_occurrence_until_blocks():
    nxt = sb.next_occurrence(
        "2026-05-01T09:00:00", "daily",
        rrule_until="2026-05-01T23:59:59",
    )
    assert nxt is None  # until 이전이라 다음 발화 없음


def test_next_occurrence_no_freq_returns_none():
    assert sb.next_occurrence("2026-05-01T09:00:00", None) is None
    assert sb.next_occurrence("2026-05-01T09:00:00", "") is None


def test_next_occurrence_unknown_freq():
    assert sb.next_occurrence("2026-05-01T09:00:00", "yearly") is None


def test_next_occurrence_after_param():
    # after 지정 시 그 이후 발화
    nxt = sb.next_occurrence(
        "2026-05-01T09:00:00", "daily",
        after=datetime(2026, 5, 5, 12, 0),
    )
    assert nxt == "2026-05-06T09:00:00"


# ─── add_event with RRULE/pre ───────────────────────


def test_add_event_with_rrule(db):
    eid = sb.add_event(
        "주간 리뷰", "2026-05-04T09:00:00",
        rrule_freq="weekly", rrule_byday="MO,WE,FR",
        chat_id="111", db_path=db,
    )
    ev = sb.get_event(eid, db_path=db)
    assert ev["rrule_freq"] == "weekly"
    assert ev["rrule_byday"] == "MO,WE,FR"
    assert ev["pre_notify_minutes"] == 0


def test_add_event_with_pre_notify(db):
    eid = sb.add_event(
        "회의", "2026-05-01T15:00:00",
        pre_notify_minutes=10, chat_id="111", db_path=db,
    )
    ev = sb.get_event(eid, db_path=db)
    assert ev["pre_notify_minutes"] == 10
    assert ev["pre_notified"] is False


def test_add_event_invalid_freq_raises(db):
    with pytest.raises(ValueError):
        sb.add_event(
            "x", "2026-05-01T09:00:00",
            rrule_freq="hourly", db_path=db,
        )


def test_add_event_invalid_until_raises(db):
    with pytest.raises(ValueError):
        sb.add_event(
            "x", "2026-05-01T09:00:00",
            rrule_freq="daily", rrule_until="not-a-date",
            db_path=db,
        )


def test_add_event_negative_pre_clamped(db):
    eid = sb.add_event(
        "x", "2026-05-01T09:00:00",
        pre_notify_minutes=-5, db_path=db,
    )
    assert sb.get_event(eid, db_path=db)["pre_notify_minutes"] == 0


# ─── due_for_notification: pre + main ──────────────


def test_due_main_only(db):
    sb.add_event("임박", _iso(30), chat_id="111", db_path=db)
    rows = sb.due_for_notification(db_path=db)
    assert len(rows) == 1
    assert rows[0]["kind"] == "main"


def test_due_pre_only_when_main_far(db):
    """when_at은 10분 후, pre_notify=10분 → 사전 알림 시점이 지금 임박."""
    sb.add_event(
        "사전알림 테스트", _iso(60 * 10),  # 10분 후
        pre_notify_minutes=10, chat_id="111", db_path=db,
    )
    rows = sb.due_for_notification(horizon_seconds=70, db_path=db)
    # pre_at = when_at - 10분 = 지금 → horizon=70초 안에 들어옴
    pre_rows = [r for r in rows if r["kind"] == "pre"]
    main_rows = [r for r in rows if r["kind"] == "main"]
    assert len(pre_rows) == 1
    assert len(main_rows) == 0  # main은 10분 후라 horizon 밖


def test_due_pre_skipped_when_zero(db):
    sb.add_event("pre 0", _iso(60 * 10), pre_notify_minutes=0,
                 chat_id="111", db_path=db)
    rows = sb.due_for_notification(db_path=db)
    assert all(r["kind"] != "pre" for r in rows)


def test_pre_notified_idempotent(db):
    eid = sb.add_event(
        "테스트", _iso(60 * 10),
        pre_notify_minutes=10, chat_id="111", db_path=db,
    )
    rows = sb.due_for_notification(db_path=db)
    pre_rows = [r for r in rows if r["kind"] == "pre"]
    assert len(pre_rows) == 1
    sb.mark_pre_notified(eid, db_path=db)
    rows2 = sb.due_for_notification(db_path=db)
    assert all(r["kind"] != "pre" for r in rows2)


def test_main_and_pre_independent(db):
    """같은 이벤트의 main이 발송돼도 pre는 별도 멱등."""
    eid = sb.add_event(
        "x", _iso(60 * 10),
        pre_notify_minutes=10, chat_id="111", db_path=db,
    )
    sb.mark_notified(eid, db_path=db)
    rows = sb.due_for_notification(db_path=db)
    pre_rows = [r for r in rows if r["kind"] == "pre"]
    assert len(pre_rows) == 1  # main과 무관하게 pre는 여전히 살아있음


# ─── advance_recurring ──────────────────────────────


def test_advance_recurring_daily(db):
    eid = sb.add_event(
        "운동", "2026-05-01T07:00:00",
        rrule_freq="daily", chat_id="111", db_path=db,
    )
    sb.mark_notified(eid, db_path=db)
    nxt = sb.advance_recurring(eid, db_path=db)
    assert nxt == "2026-05-02T07:00:00"
    ev = sb.get_event(eid, db_path=db)
    assert ev["when_at"] == "2026-05-02T07:00:00"
    assert ev["notified"] is False  # 리셋
    assert ev["pre_notified"] is False  # 리셋


def test_advance_recurring_weekly_byday(db):
    eid = sb.add_event(
        "리뷰", "2026-05-04T09:00:00",  # 월
        rrule_freq="weekly", rrule_byday="MO,WE,FR",
        chat_id="111", db_path=db,
    )
    nxt = sb.advance_recurring(eid, db_path=db)
    assert nxt == "2026-05-06T09:00:00"  # 수


def test_advance_recurring_until_returns_none(db):
    eid = sb.add_event(
        "x", "2026-05-01T09:00:00",
        rrule_freq="daily", rrule_until="2026-05-01T23:59:59",
        chat_id="111", db_path=db,
    )
    nxt = sb.advance_recurring(eid, db_path=db)
    assert nxt is None
    # row는 그대로 (마크 변경 없음)
    ev = sb.get_event(eid, db_path=db)
    assert ev["when_at"] == "2026-05-01T09:00:00"


def test_advance_recurring_no_rrule_returns_none(db):
    eid = sb.add_event("일회성", "2026-05-01T09:00:00",
                       chat_id="111", db_path=db)
    nxt = sb.advance_recurring(eid, db_path=db)
    assert nxt is None


def test_advance_recurring_missing_id(db):
    assert sb.advance_recurring(99999, db_path=db) is None


# ─── 마이그레이션 idempotent ───────────────────────


def test_migration_v3_9_columns_exist(db):
    """v3.9 마이그레이션이 기존 db에 새 컬럼 추가."""
    sb.add_event("초기 데이터", "2027-01-01T10:00:00",
                 chat_id="111", db_path=db)
    # 두 번째 add도 정상
    sb.add_event("두번째", "2027-01-02T10:00:00",
                 rrule_freq="daily", pre_notify_minutes=15,
                 chat_id="111", db_path=db)

    # 직접 SQLite 열어서 컬럼 존재 확인
    import sqlite3
    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()}
    con.close()
    assert "rrule_freq" in cols
    assert "rrule_byday" in cols
    assert "rrule_until" in cols
    assert "pre_notify_minutes" in cols
    assert "pre_notified" in cols


# ─── router validator: RRULE/pre ────────────────────


def test_router_validator_weekly_byday():
    v = router._validate_schedule_args({
        "action": "add", "title": "리뷰", "when_at": "2026-05-04T09:00:00",
        "rrule_freq": "weekly", "rrule_byday": "MO,WE,FR",
    })
    assert v["rrule_freq"] == "weekly"
    assert v["rrule_byday"] == "MO,WE,FR"


def test_router_validator_byday_filters_invalid_tokens():
    v = router._validate_schedule_args({
        "action": "add", "title": "x", "when_at": "2026-05-04T09:00:00",
        "rrule_freq": "weekly", "rrule_byday": "MO,XX,FR",
    })
    assert v["rrule_byday"] == "MO,FR"  # XX 제거됨


def test_router_validator_byday_only_for_weekly():
    """daily/monthly에 byday를 줘도 byday는 무시됨."""
    v = router._validate_schedule_args({
        "action": "add", "title": "x", "when_at": "2026-05-04T09:00:00",
        "rrule_freq": "daily", "rrule_byday": "MO,WE",
    })
    assert v["rrule_freq"] == "daily"
    assert "rrule_byday" not in v


def test_router_validator_pre_notify():
    v = router._validate_schedule_args({
        "action": "add", "title": "회의", "when_at": "2026-05-02T15:00:00",
        "pre_notify_minutes": "5",  # 라우터가 string으로 보낼 수도
    })
    assert v["pre_notify_minutes"] == 5


def test_router_validator_pre_zero_skipped():
    v = router._validate_schedule_args({
        "action": "add", "title": "x", "when_at": "2026-05-02T15:00:00",
        "pre_notify_minutes": 0,
    })
    assert "pre_notify_minutes" not in v


def test_router_validator_until_pass_through():
    v = router._validate_schedule_args({
        "action": "add", "title": "x", "when_at": "2026-05-01T09:00:00",
        "rrule_freq": "daily", "rrule_until": "2026-12-31T23:59:59",
    })
    assert v["rrule_until"] == "2026-12-31T23:59:59"


# ─── 멀티 사전알림 (v3.15) ──────────────────────────


def test_pre_notify_minutes_accepts_list(db):
    """list[int] 입력 → events.pre_notify_minutes는 max, 새 테이블에 각 row."""
    eid = sb.add_event(
        "회의", "2027-04-01T15:00:00",
        pre_notify_minutes=[5, 30],
        chat_id="111", db_path=db,
    )
    ev = sb.get_event(eid, db_path=db)
    # legacy column = max
    assert ev["pre_notify_minutes"] == 30
    # 신규 list = 내림차순
    assert ev["pre_notify_minutes_list"] == [30, 5]


def test_pre_notify_minutes_int_still_works(db):
    """기존 int 입력은 그대로 동작 (BC)."""
    eid = sb.add_event(
        "단일", "2027-04-01T15:00:00",
        pre_notify_minutes=10,
        chat_id="111", db_path=db,
    )
    ev = sb.get_event(eid, db_path=db)
    assert ev["pre_notify_minutes"] == 10
    assert ev["pre_notify_minutes_list"] == [10]


def test_pre_notify_minutes_normalizes_duplicates_and_negatives(db):
    """list에 중복·음수·0·문자열 섞여 있어도 정리."""
    eid = sb.add_event(
        "정리 테스트", "2027-04-01T15:00:00",
        pre_notify_minutes=[5, 5, "30", -1, 0, "abc"],
        chat_id="111", db_path=db,
    )
    ev = sb.get_event(eid, db_path=db)
    assert ev["pre_notify_minutes_list"] == [30, 5]


def test_pre_notify_minutes_empty_list(db):
    """빈 list → events.pre_notify_minutes=0, list=[]."""
    eid = sb.add_event(
        "빈 list", "2027-04-01T15:00:00",
        pre_notify_minutes=[],
        chat_id="111", db_path=db,
    )
    ev = sb.get_event(eid, db_path=db)
    assert ev["pre_notify_minutes"] == 0
    assert ev["pre_notify_minutes_list"] == []


def test_due_for_notification_emits_one_row_per_alert(db):
    """30분 전, 5분 전 모두 윈도우 안이면 두 개의 'pre' row 반환."""
    # when_at = 지금 + 30분. 30분 전 알림 시점 = 지금 → due
    sb.add_event(
        "이벤트", _iso(60 * 30),
        pre_notify_minutes=[5, 30],
        chat_id="111", db_path=db,
    )
    rows = sb.due_for_notification(horizon_seconds=70, db_path=db)
    pre_rows = [r for r in rows if r["kind"] == "pre"]
    # 30분 전만 due (5분 전은 25분 후 발화 예정)
    assert len(pre_rows) == 1
    assert pre_rows[0]["minutes_before"] == 30


def test_due_for_notification_skips_already_notified_alert(db):
    """30분 전 알림 발송 후엔 그 alert만 skip, 5분 전 alert는 살아있음."""
    eid = sb.add_event(
        "이벤트", _iso(60 * 30),
        pre_notify_minutes=[5, 30],
        chat_id="111", db_path=db,
    )
    # 30분 전 alert 마킹
    sb.mark_pre_notified(eid, minutes_before=30, db_path=db)

    rows = sb.due_for_notification(horizon_seconds=70, db_path=db)
    pre_rows = [r for r in rows if r["kind"] == "pre"]
    assert len(pre_rows) == 0  # 30분 전은 마킹됨, 5분 전은 아직 25분 후라 윈도우 밖


def test_mark_pre_notified_specific_alert_only(db):
    """minutes_before 명시 → 그 alert만 마킹. 다른 alert는 그대로."""
    eid = sb.add_event(
        "이벤트", "2027-04-01T15:00:00",
        pre_notify_minutes=[5, 30],
        chat_id="111", db_path=db,
    )
    sb.mark_pre_notified(eid, minutes_before=30, db_path=db)

    # 새 테이블 직접 조회
    import sqlite3
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT minutes_before, notified FROM event_pre_notifications "
        "WHERE event_id = ? ORDER BY minutes_before DESC",
        (eid,),
    ).fetchall()
    con.close()

    notified_map = {r["minutes_before"]: r["notified"] for r in rows}
    assert notified_map == {30: 1, 5: 0}  # 30분 전만 마킹됨


def test_mark_pre_notified_all_marks_legacy_column(db):
    """모든 alert이 마킹되면 events.pre_notified=1로 갱신 (legacy 일관성)."""
    eid = sb.add_event(
        "이벤트", "2027-04-01T15:00:00",
        pre_notify_minutes=[5, 30],
        chat_id="111", db_path=db,
    )
    sb.mark_pre_notified(eid, minutes_before=30, db_path=db)
    sb.mark_pre_notified(eid, minutes_before=5, db_path=db)

    ev = sb.get_event(eid, db_path=db)
    assert ev["pre_notified"] is True


def test_mark_pre_notified_partial_keeps_legacy_zero(db):
    """일부만 마킹되면 events.pre_notified는 아직 0 유지."""
    eid = sb.add_event(
        "이벤트", "2027-04-01T15:00:00",
        pre_notify_minutes=[5, 30],
        chat_id="111", db_path=db,
    )
    sb.mark_pre_notified(eid, minutes_before=30, db_path=db)

    ev = sb.get_event(eid, db_path=db)
    assert ev["pre_notified"] is False  # 5분 전 아직 안 됨


def test_mark_pre_notified_no_minutes_marks_all(db):
    """기존 시그니처 mark_pre_notified(eid) — 모든 alert 마킹 (BC)."""
    eid = sb.add_event(
        "이벤트", "2027-04-01T15:00:00",
        pre_notify_minutes=[5, 30],
        chat_id="111", db_path=db,
    )
    sb.mark_pre_notified(eid, db_path=db)  # minutes_before 없음

    import sqlite3
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT notified FROM event_pre_notifications WHERE event_id = ?",
        (eid,),
    ).fetchall()
    con.close()
    assert all(r["notified"] == 1 for r in rows)


def test_advance_recurring_resets_all_alerts(db):
    """반복 일정 advance → 모든 alert notified=0 리셋."""
    eid = sb.add_event(
        "주간 회의", "2026-05-04T09:00:00",
        rrule_freq="weekly",
        pre_notify_minutes=[5, 30, 60],
        chat_id="111", db_path=db,
    )
    sb.mark_pre_notified(eid, minutes_before=30, db_path=db)
    sb.mark_pre_notified(eid, minutes_before=5, db_path=db)
    sb.mark_pre_notified(eid, minutes_before=60, db_path=db)
    sb.mark_notified(eid, db_path=db)

    nxt = sb.advance_recurring(eid, db_path=db)
    assert nxt is not None

    import sqlite3
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT notified FROM event_pre_notifications WHERE event_id = ?",
        (eid,),
    ).fetchall()
    con.close()
    assert all(r["notified"] == 0 for r in rows)


def test_delete_event_cascades_alerts(db):
    """delete_event → event_pre_notifications row도 정리."""
    eid = sb.add_event(
        "삭제 대상", "2027-04-01T15:00:00",
        pre_notify_minutes=[5, 30],
        chat_id="111", db_path=db,
    )
    assert sb.delete_event(eid, db_path=db) is True

    import sqlite3
    con = sqlite3.connect(str(db))
    rows = con.execute(
        "SELECT COUNT(*) FROM event_pre_notifications WHERE event_id = ?",
        (eid,),
    ).fetchone()
    con.close()
    assert rows[0] == 0


def test_run_add_displays_multiple_alerts(db):
    """run("add")가 list 입력 시 응답 메시지에 'N회' + 각 분 표시."""
    msg, _ = sb.run(
        "add",
        title="회의",
        when_at="2027-04-01T15:00:00",
        chat_id="111",
        pre_notify_minutes=[5, 30],
        db_path=db,
    )
    assert "✅" in msg
    assert "사전 알림" in msg
    assert "30분 전" in msg
    assert "5분 전" in msg
    assert "2회" in msg


def test_run_add_displays_single_alert_unchanged(db):
    """단일 알림은 기존 표기 유지."""
    msg, _ = sb.run(
        "add",
        title="회의",
        when_at="2027-04-01T15:00:00",
        chat_id="111",
        pre_notify_minutes=10,
        db_path=db,
    )
    assert "🔔 사전 알림: 10분 전" in msg
    assert "회" not in msg or "2회" not in msg  # "N회" 표기 안 나타남


def test_backfill_existing_pre_notify_to_new_table(db, tmp_path):
    """기존 v3.9 db (pre_notify_minutes만)도 첫 로드 시 새 테이블로 backfill."""
    # 1) 새 테이블 없는 상태 흉내 — db를 먼저 만들고 새 테이블만 비움
    eid = sb.add_event(
        "기존 일정", "2027-04-01T15:00:00",
        pre_notify_minutes=15,
        chat_id="111", db_path=db,
    )
    # 새 테이블 row를 강제로 비움 (마치 마이그레이션 전 상태)
    import sqlite3
    con = sqlite3.connect(str(db))
    con.execute("DELETE FROM event_pre_notifications")
    con.commit()
    con.close()

    # 2) 다시 _conn 호출 → _ensure_schema의 backfill 트리거
    ev = sb.get_event(eid, db_path=db)
    assert ev["pre_notify_minutes_list"] == [15]


def test_pre_notify_alerts_unique_constraint(db):
    """같은 (event_id, minutes_before) 중복 insert는 무시 (UNIQUE 제약)."""
    eid = sb.add_event(
        "이벤트", "2027-04-01T15:00:00",
        pre_notify_minutes=[5, 5, 5, 30],
        chat_id="111", db_path=db,
    )
    ev = sb.get_event(eid, db_path=db)
    # 5는 한 번만, 30은 한 번
    assert sorted(ev["pre_notify_minutes_list"]) == [5, 30]
