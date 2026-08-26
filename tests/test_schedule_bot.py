"""
schedule_bot 단위 테스트.

- 일시 파싱 (parse_when): 다양한 입력 포맷
- CRUD: add → get → list (upcoming) → complete → delete
- run() entrypoint: add/list/upcoming/delete/complete + 에러 케이스
- chat_id 격리: 다른 채팅의 일정은 list에 안 나옴

LLM 호출 없음 (schedule_bot은 결정론적).
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import schedule_bot as sb
import telegram_write_identity as twi
from private_data_api_client import PrivateAPIUnavailable


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """매 테스트마다 새 SQLite 파일 — 격리 보장."""
    return tmp_path / "test_schedule.db"


def _write_identity(action="add"):
    return twi.build_schedule_write_identity(
        update_id=8001,
        chat_id=-100987654321,
        user_id=654321,
        message_id=4001,
        action=action,
    )


# ─── parse_when ───────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected_iso",
    [
        ("2026-05-08T15:00:00", "2026-05-08T15:00:00"),
        ("2026-05-08T15:00", "2026-05-08T15:00:00"),
        ("2026-05-08 15:00:00", "2026-05-08T15:00:00"),
        ("2026-05-08 15:00", "2026-05-08T15:00:00"),
        ("2026-05-08", "2026-05-08T00:00:00"),
        ("  2026-05-08T15:00  ", "2026-05-08T15:00:00"),
    ],
)
def test_parse_when_valid(raw, expected_iso):
    assert sb.parse_when(raw) == expected_iso


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "내일",
        "2026-13-40",
        "abc",
        "2026/05/08",
    ],
)
def test_parse_when_invalid(raw):
    assert sb.parse_when(raw) is None


def test_parse_when_with_timezone():
    # +09:00 KST → naive로 정규화 (시각은 그대로 유지될 수도, 변환될 수도 있어
    # 시스템 TZ에 따라 다르므로 'None이 아닌 ISO 형식'만 검증)
    out = sb.parse_when("2026-05-08T15:00:00+09:00")
    assert out is not None
    # ISO 8601 naive 포맷
    datetime.fromisoformat(out)


# ─── fmt_when ────────────────────────────────────────


def test_fmt_when_with_time():
    out = sb.fmt_when("2026-05-08T15:00:00")
    # 2026-05-08은 금요일
    assert "2026-05-08" in out
    assert "(금)" in out
    assert "15:00" in out


def test_fmt_when_date_only():
    # 자정은 시각 표기 생략
    out = sb.fmt_when("2026-05-08T00:00:00")
    assert "15:00" not in out
    assert "(금)" in out


def test_fmt_when_invalid():
    # 잘못된 입력은 그대로 반환
    assert sb.fmt_when("not-an-iso") == "not-an-iso"


# ─── CRUD ────────────────────────────────────────────


def test_add_get_event(db):
    eid = sb.add_event("콴텍봇 리뷰", "2026-05-12T15:00:00", db_path=db)
    assert eid > 0
    ev = sb.get_event(eid, db_path=db)
    assert ev is not None
    assert ev["title"] == "콴텍봇 리뷰"
    assert ev["when_at"] == "2026-05-12T15:00:00"
    assert ev["completed"] is False


def test_add_event_with_chat_id_and_notes(db):
    eid = sb.add_event(
        "백테스트", "2026-06-01T10:00:00",
        notes="모멘텀 팩터", chat_id="111", db_path=db,
    )
    ev = sb.get_event(eid, db_path=db)
    assert ev["notes"] == "모멘텀 팩터"
    assert ev["chat_id"] == "111"


def test_add_event_empty_title_raises(db):
    with pytest.raises(ValueError):
        sb.add_event("", "2026-05-12T15:00:00", db_path=db)
    with pytest.raises(ValueError):
        sb.add_event("   ", "2026-05-12T15:00:00", db_path=db)


def test_add_event_invalid_when_raises(db):
    with pytest.raises(ValueError):
        sb.add_event("foo", "내일 오후 3시", db_path=db)


def test_get_event_not_found(db):
    assert sb.get_event(9999, db_path=db) is None


def test_list_upcoming_only_future(db):
    past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    future = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    sb.add_event("과거", past, db_path=db)
    sb.add_event("미래", future, db_path=db)

    upcoming = sb.list_events(upcoming_only=True, db_path=db)
    titles = [e["title"] for e in upcoming]
    assert "미래" in titles
    assert "과거" not in titles

    # upcoming_only=False면 둘 다
    everything = sb.list_events(upcoming_only=False, db_path=db)
    titles_all = [e["title"] for e in everything]
    assert "과거" in titles_all
    assert "미래" in titles_all


def test_list_sorted_ascending(db):
    sb.add_event("늦음", "2026-12-01T10:00:00", db_path=db)
    sb.add_event("이름", "2026-11-01T10:00:00", db_path=db)
    sb.add_event("가운데", "2026-11-15T10:00:00", db_path=db)
    res = sb.list_events(upcoming_only=False, db_path=db)
    titles = [e["title"] for e in res]
    assert titles == ["이름", "가운데", "늦음"]


def test_list_chat_id_isolation(db):
    sb.add_event("A 채팅", "2027-01-01T10:00:00", chat_id="A", db_path=db)
    sb.add_event("B 채팅", "2027-01-02T10:00:00", chat_id="B", db_path=db)
    a_only = sb.list_events(upcoming_only=False, chat_id="A", db_path=db)
    assert len(a_only) == 1
    assert a_only[0]["title"] == "A 채팅"


def test_list_excludes_completed_by_default(db):
    eid = sb.add_event("끝난 일", "2027-01-01T10:00:00", db_path=db)
    sb.mark_completed(eid, db_path=db)
    active = sb.list_events(upcoming_only=False, db_path=db)
    assert len(active) == 0
    with_done = sb.list_events(
        upcoming_only=False, include_completed=True, db_path=db
    )
    assert len(with_done) == 1


def test_delete_event(db):
    eid = sb.add_event("지울 일정", "2027-01-01T10:00:00", db_path=db)
    assert sb.delete_event(eid, db_path=db) is True
    assert sb.get_event(eid, db_path=db) is None
    assert sb.delete_event(eid, db_path=db) is False  # 두 번째는 False


def test_mark_completed(db):
    eid = sb.add_event("완료할 일", "2027-01-01T10:00:00", db_path=db)
    assert sb.mark_completed(eid, db_path=db) is True
    ev = sb.get_event(eid, db_path=db)
    assert ev["completed"] is True
    assert sb.mark_completed(99999, db_path=db) is False


# ─── run() entrypoint ────────────────────────────────


def test_run_add_success(db):
    msg, sources = sb.run(
        "add",
        title="회의",
        when_at="2027-01-01T10:00:00",
        db_path=db,
    )
    assert "✅" in msg
    assert "회의" in msg
    assert sources == []


def test_run_add_missing_args(db):
    msg, _ = sb.run("add", title="제목만", db_path=db)
    assert "❌" in msg


def test_run_add_invalid_when(db):
    msg, _ = sb.run("add", title="t", when_at="내일", db_path=db)
    assert "❌" in msg


def test_run_upcoming_empty(db):
    msg, _ = sb.run("upcoming", db_path=db)
    assert "다가오는 일정" in msg
    assert "(없음)" in msg


def test_run_upcoming_lists(db):
    sb.add_event("앞일", "2027-01-01T10:00:00", db_path=db)
    msg, _ = sb.run("upcoming", db_path=db)
    assert "앞일" in msg


def test_run_list_prefers_private_api_without_local_db(monkeypatch):
    class Client:
        def list_schedule(self, chat_id, *, upcoming_only, limit):
            assert chat_id == "111"
            assert upcoming_only is False
            assert limit == 20
            return [
                {
                    "id": 7,
                    "title": "API 일정",
                    "when_at": "2099-01-01T10:00:00",
                    "notes": None,
                    "completed": False,
                    "rrule_freq": None,
                    "rrule_byday": None,
                    "rrule_until": None,
                    "pre_notify_minutes": 0,
                    "pre_notify_minutes_list": [],
                }
            ]

    monkeypatch.setattr(
        sb,
        "list_events",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("local DB must not be read")),
    )
    message, _ = sb.run("list", chat_id="111", private_client=Client())
    assert "API 일정" in message


def test_run_upcoming_falls_back_when_private_api_is_unavailable(monkeypatch, db):
    sb.add_event("폴백 일정", "2099-01-01T10:00:00", chat_id="111", db_path=db)

    class Client:
        def list_schedule(self, chat_id, *, upcoming_only, limit):
            raise PrivateAPIUnavailable("down")

    monkeypatch.setattr(sb, "DEFAULT_DB_PATH", db)
    message, _ = sb.run("upcoming", chat_id="111", private_client=Client())
    assert "폴백 일정" in message


def test_schedule_writes_do_not_call_private_api(db):
    class Client:
        def list_schedule(self, *args, **kwargs):
            raise AssertionError("write actions must not use Private API")

    message, _ = sb.run(
        "add",
        title="직접 쓰기 유지",
        when_at="2099-01-01T10:00:00",
        chat_id="111",
        db_path=db,
        private_client=Client(),
    )
    assert "등록" in message


def test_schedule_run_accepts_matching_identity_without_changing_direct_write(db):
    message, _ = sb.run(
        "add",
        title="identity 일정",
        when_at="2099-01-01T10:00:00",
        chat_id="111",
        db_path=db,
        write_identity=_write_identity("add"),
    )

    assert "등록" in message
    assert len(sb.list_events(upcoming_only=False, chat_id="111", db_path=db)) == 1


def test_schedule_run_rejects_identity_for_different_operation(db):
    with pytest.raises(ValueError, match="does not match"):
        sb.run(
            "complete",
            event_id=1,
            chat_id="111",
            db_path=db,
            write_identity=_write_identity("delete"),
        )
    assert db.exists() is False


def test_run_delete_existing(db):
    eid = sb.add_event("지울 것", "2027-01-01T10:00:00", db_path=db)
    msg, _ = sb.run("delete", event_id=eid, db_path=db)
    assert "삭제" in msg
    assert sb.get_event(eid, db_path=db) is None


def test_run_delete_missing_id(db):
    msg, _ = sb.run("delete", db_path=db)
    assert "❌" in msg


def test_run_complete(db):
    eid = sb.add_event("완료할 것", "2027-01-01T10:00:00", db_path=db)
    msg, _ = sb.run("complete", event_id=eid, db_path=db)
    assert "✅" in msg
    assert sb.get_event(eid, db_path=db)["completed"] is True


def test_run_unknown_action(db):
    msg, _ = sb.run("dance", db_path=db)
    assert "알 수 없는" in msg


# ─── 스키마 idempotent ───────────────────────────────


def test_schema_idempotent(db):
    """두 번째 연결도 정상 동작 (CREATE IF NOT EXISTS)."""
    sb.add_event("첫 일정", "2027-01-01T10:00:00", db_path=db)
    # 다시 새 연결 — 스키마 재생성 시도해도 깨지지 않아야 함
    sb.add_event("두 번째 일정", "2027-01-02T10:00:00", db_path=db)
    assert len(sb.list_events(upcoming_only=False, db_path=db)) == 2

    # 직접 SQLite 열어서 인덱스 확인
    con = sqlite3.connect(str(db))
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    names = {r[0] for r in cur.fetchall()}
    con.close()
    assert "idx_events_when" in names
    assert "idx_events_chat" in names


# ─── 충돌 감지 (v3.14) ───────────────────────────────


CHAT_A = "chat_alpha"
CHAT_B = "chat_beta"


def test_find_conflicts_exact_same_time(db):
    """완전히 같은 시각에 같은 chat의 다른 일정 → 충돌."""
    sb.add_event("기존 회의", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db)
    conflicts = sb.find_conflicts("2027-03-15T14:00:00", CHAT_A, db_path=db)
    assert len(conflicts) == 1
    assert conflicts[0]["title"] == "기존 회의"


def test_find_conflicts_within_default_window(db):
    """기본 15분 윈도우 안 (10분 차이) → 충돌."""
    sb.add_event("앞 회의", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db)
    conflicts = sb.find_conflicts("2027-03-15T14:10:00", CHAT_A, db_path=db)
    assert len(conflicts) == 1


def test_find_conflicts_outside_default_window(db):
    """기본 15분 윈도우 밖 (30분 차이) → 충돌 없음."""
    sb.add_event("앞 회의", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db)
    conflicts = sb.find_conflicts("2027-03-15T14:30:00", CHAT_A, db_path=db)
    assert conflicts == []


def test_find_conflicts_at_window_boundary(db):
    """정확히 윈도우 끝(15분)도 포함."""
    sb.add_event("앞 회의", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db)
    # 14:15 정각은 ±15분 윈도우 안 (포함)
    conflicts = sb.find_conflicts("2027-03-15T14:15:00", CHAT_A, db_path=db)
    assert len(conflicts) == 1


def test_find_conflicts_chat_id_isolation(db):
    """다른 chat_id의 같은 시각 일정 → 충돌 없음."""
    sb.add_event("타인 회의", "2027-03-15T14:00:00", chat_id=CHAT_B, db_path=db)
    conflicts = sb.find_conflicts("2027-03-15T14:00:00", CHAT_A, db_path=db)
    assert conflicts == []


def test_find_conflicts_excludes_completed(db):
    """completed=1 일정은 충돌 검사에서 제외."""
    eid = sb.add_event("끝난 일", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db)
    sb.mark_completed(eid, db_path=db)
    conflicts = sb.find_conflicts("2027-03-15T14:05:00", CHAT_A, db_path=db)
    assert conflicts == []


def test_find_conflicts_no_chat_id_returns_empty(db):
    """chat_id가 None/빈 문자열이면 빈 리스트 (1인 격리 가정)."""
    sb.add_event("이름 없는 일정", "2027-03-15T14:00:00", chat_id=None, db_path=db)
    assert sb.find_conflicts("2027-03-15T14:00:00", None, db_path=db) == []
    assert sb.find_conflicts("2027-03-15T14:00:00", "", db_path=db) == []


def test_find_conflicts_window_zero_disables_check(db):
    """window_minutes=0이면 검사 비활성 → 항상 빈 리스트."""
    sb.add_event("기존", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db)
    conflicts = sb.find_conflicts(
        "2027-03-15T14:00:00", CHAT_A, window_minutes=0, db_path=db
    )
    assert conflicts == []


def test_find_conflicts_custom_window(db):
    """custom window — 30분이면 25분 차이도 충돌, 35분 차이는 아님."""
    sb.add_event("기존", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db)
    near = sb.find_conflicts(
        "2027-03-15T14:25:00", CHAT_A, window_minutes=30, db_path=db
    )
    far = sb.find_conflicts(
        "2027-03-15T14:35:00", CHAT_A, window_minutes=30, db_path=db
    )
    assert len(near) == 1
    assert far == []


def test_find_conflicts_exclude_event_id(db):
    """자기 자신 제외 옵션 — id 매치하면 빈 리스트."""
    eid = sb.add_event("자신", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db)
    excluded = sb.find_conflicts(
        "2027-03-15T14:00:00", CHAT_A, exclude_event_id=eid, db_path=db
    )
    assert excluded == []
    # 다른 일정 추가하면 그건 잡힘
    sb.add_event("타", "2027-03-15T14:05:00", chat_id=CHAT_A, db_path=db)
    excl2 = sb.find_conflicts(
        "2027-03-15T14:00:00", CHAT_A, exclude_event_id=eid, db_path=db
    )
    assert len(excl2) == 1
    assert excl2[0]["title"] == "타"


def test_find_conflicts_invalid_when_at_returns_empty(db):
    """파싱 불가 시각이면 빈 리스트 (graceful)."""
    sb.add_event("기존", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db)
    assert sb.find_conflicts("내일", CHAT_A, db_path=db) == []
    assert sb.find_conflicts("", CHAT_A, db_path=db) == []


def test_find_conflicts_sorted_ascending(db):
    """여러 충돌 → when_at 오름차순 정렬."""
    sb.add_event("두번째", "2027-03-15T14:08:00", chat_id=CHAT_A, db_path=db)
    sb.add_event("첫번째", "2027-03-15T13:55:00", chat_id=CHAT_A, db_path=db)
    sb.add_event("세번째", "2027-03-15T14:12:00", chat_id=CHAT_A, db_path=db)
    conflicts = sb.find_conflicts("2027-03-15T14:00:00", CHAT_A, db_path=db)
    assert [c["title"] for c in conflicts] == ["첫번째", "두번째", "세번째"]


# ─── run("add") 통합 — 충돌 경고 ─────────────────────


def test_run_add_warns_on_conflict(db):
    """run("add") 호출 시 충돌이 있으면 메시지에 ⚠️ + 충돌 일정 목록 포함."""
    sb.add_event("기존 미팅", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db)
    msg, _ = sb.run(
        "add",
        title="새 미팅",
        when_at="2027-03-15T14:10:00",
        chat_id=CHAT_A,
        db_path=db,
    )
    # 등록은 성공
    assert "✅" in msg
    assert "새 미팅" in msg
    # 경고 표시
    assert "⚠️" in msg
    assert "충돌" in msg
    assert "기존 미팅" in msg


def test_run_add_no_conflict_no_warning(db):
    """충돌 없으면 ⚠️ 안 뜸."""
    msg, _ = sb.run(
        "add",
        title="단독 일정",
        when_at="2027-03-15T14:00:00",
        chat_id=CHAT_A,
        db_path=db,
    )
    assert "✅" in msg
    assert "⚠️" not in msg
    assert "충돌" not in msg


def test_run_add_conflict_window_zero_skips_check(db):
    """conflict_window_minutes=0 명시 → 충돌 있어도 경고 안 함."""
    sb.add_event("기존", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db)
    msg, _ = sb.run(
        "add",
        title="중복 강제",
        when_at="2027-03-15T14:00:00",
        chat_id=CHAT_A,
        conflict_window_minutes=0,
        db_path=db,
    )
    assert "✅" in msg
    assert "⚠️" not in msg


def test_run_add_warns_only_within_chat_id(db):
    """다른 chat_id의 같은 시각 일정은 경고 안 함."""
    sb.add_event("타인 약속", "2027-03-15T14:00:00", chat_id=CHAT_B, db_path=db)
    msg, _ = sb.run(
        "add",
        title="내 약속",
        when_at="2027-03-15T14:00:00",
        chat_id=CHAT_A,
        db_path=db,
    )
    assert "✅" in msg
    assert "⚠️" not in msg


def test_run_add_recurring_main_only(db):
    """반복 일정은 메인 when_at만 비교 — 다음 occurrence는 펼치지 않음."""
    # 매주 월요일 14시 반복
    sb.add_event(
        "주간 회의", "2027-03-15T14:00:00",
        chat_id=CHAT_A, rrule_freq="weekly", db_path=db,
    )
    # 다음 주 월요일 14시(2027-03-22)에 새 일정 추가 — 메인 when_at 비교 시 충돌 없음
    msg, _ = sb.run(
        "add",
        title="다음주 회의",
        when_at="2027-03-22T14:00:00",
        chat_id=CHAT_A,
        db_path=db,
    )
    assert "✅" in msg
    assert "⚠️" not in msg  # 반복 occurrence 펼치지 않으므로


def test_run_add_excludes_completed_from_warning(db):
    """완료 처리된 일정은 충돌 경고에 안 나옴."""
    eid = sb.add_event(
        "이미 완료", "2027-03-15T14:00:00", chat_id=CHAT_A, db_path=db
    )
    sb.mark_completed(eid, db_path=db)
    msg, _ = sb.run(
        "add",
        title="새 일정",
        when_at="2027-03-15T14:05:00",
        chat_id=CHAT_A,
        db_path=db,
    )
    assert "✅" in msg
    assert "⚠️" not in msg


def test_run_add_warning_lists_multiple_conflicts(db):
    """여러 충돌 일정이 있을 때 모두 목록에 표시."""
    sb.add_event("회의 1", "2027-03-15T13:55:00", chat_id=CHAT_A, db_path=db)
    sb.add_event("회의 2", "2027-03-15T14:08:00", chat_id=CHAT_A, db_path=db)
    msg, _ = sb.run(
        "add",
        title="새 일정",
        when_at="2027-03-15T14:00:00",
        chat_id=CHAT_A,
        db_path=db,
    )
    assert "⚠️" in msg
    assert "회의 1" in msg
    assert "회의 2" in msg
    # 카운트 표기 검증
    assert "2건" in msg


def test_run_add_default_constant():
    """DEFAULT_CONFLICT_WINDOW_MINUTES 상수 노출 확인 (라우터·문서가 참조 가능)."""
    assert sb.DEFAULT_CONFLICT_WINDOW_MINUTES == 15
