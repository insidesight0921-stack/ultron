"""
schedule_bot 알림 스케줄러용 함수 테스트 — due_for_notification + mark_notified.

순수 SQLite 로직, LLM/HTTP 무관. 멱등성·윈도우 경계·grace 동작 검증.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import schedule_bot as sb


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "notify.db"


def _iso(offset_seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%S")


def test_due_excludes_far_future(db):
    sb.add_event("미래", _iso(3600), chat_id="111", db_path=db)
    assert sb.due_for_notification(horizon_seconds=70, db_path=db) == []


def test_due_includes_imminent(db):
    sb.add_event("임박", _iso(30), chat_id="111", db_path=db)
    rows = sb.due_for_notification(horizon_seconds=70, db_path=db)
    assert len(rows) == 1
    assert rows[0]["title"] == "임박"


def test_due_includes_just_passed_in_grace(db):
    sb.add_event("방금 지남", _iso(-300), chat_id="111", db_path=db)
    rows = sb.due_for_notification(
        horizon_seconds=70, grace_seconds=600, db_path=db
    )
    assert len(rows) == 1


def test_due_excludes_past_grace(db):
    sb.add_event("아주 옛날", _iso(-7200), chat_id="111", db_path=db)
    rows = sb.due_for_notification(
        horizon_seconds=70, grace_seconds=600, db_path=db
    )
    assert rows == []


def test_due_excludes_completed(db):
    eid = sb.add_event("완료된 일정", _iso(30), chat_id="111", db_path=db)
    sb.mark_completed(eid, db_path=db)
    assert sb.due_for_notification(db_path=db) == []


def test_due_excludes_no_chat_id(db):
    sb.add_event("무chat", _iso(30), chat_id=None, db_path=db)
    sb.add_event("빈chat", _iso(30), chat_id="", db_path=db)
    assert sb.due_for_notification(db_path=db) == []


def test_due_excludes_already_notified(db):
    eid = sb.add_event("한번 알림 보냄", _iso(30), chat_id="111", db_path=db)
    rows = sb.due_for_notification(db_path=db)
    assert len(rows) == 1
    sb.mark_notified(eid, db_path=db)
    rows2 = sb.due_for_notification(db_path=db)
    assert rows2 == []  # 두 번째는 안 나옴 (멱등)


def test_mark_notified_returns_false_for_missing(db):
    assert sb.mark_notified(99999, db_path=db) is False


def test_due_returns_in_when_order(db):
    sb.add_event("늦게", _iso(60), chat_id="111", db_path=db)
    sb.add_event("빨리", _iso(20), chat_id="111", db_path=db)
    sb.add_event("중간", _iso(40), chat_id="111", db_path=db)
    rows = sb.due_for_notification(horizon_seconds=120, db_path=db)
    titles = [r["title"] for r in rows]
    assert titles == ["빨리", "중간", "늦게"]


def test_schema_migration_idempotent(db):
    """notified 컬럼은 ALTER TABLE로 마이그레이션. 두 번째 호출도 깨지지 않아야."""
    sb.add_event("첫 등록", _iso(30), chat_id="111", db_path=db)
    sb.add_event("두 번째", _iso(40), chat_id="111", db_path=db)
    rows = sb.due_for_notification(db_path=db)
    assert len(rows) == 2
