#!/usr/bin/env python3
"""Isolated schedule writer rollback rehearsal on a verified backup copy."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from private_data_security import backup_database, verify_database
from private_schedule_write_contract import validate_schedule_write_intent
from private_schedule_write_store import ScheduleWriteStore
from private_write_rollback import (
    DEFAULT_MAX_BACKUP_AGE,
    PrivateWriteRollbackError,
    _load_verified_backup,
    _require_empty_private_workspace,
    _sha256,
)
import schedule_bot


REHEARSAL_CHAT_ID = "-9223372036854775000"


class ScheduleWriteRollbackError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Schedule write rollback rehearsal failed: {code}")
        self.code = code


@dataclass(frozen=True)
class ScheduleWriteRollbackReport:
    api_write_applied: bool
    notifier_marked_delivery: bool
    notifier_advanced_recurrence: bool
    legacy_user_writer_resumed: bool
    service_rollback_preserved_api_write: bool
    emergency_restore_verified: bool
    backup_unchanged: bool
    artifacts_preserved: bool = True

    @property
    def verified(self) -> bool:
        return all(
            (
                self.api_write_applied,
                self.notifier_marked_delivery,
                self.notifier_advanced_recurrence,
                self.legacy_user_writer_resumed,
                self.service_rollback_preserved_api_write,
                self.emergency_restore_verified,
                self.backup_unchanged,
                self.artifacts_preserved,
            )
        )


def _notifier_rehearsal(db_path: Path, event_id: int, original_when: str) -> tuple[bool, bool]:
    pre_marked = schedule_bot.mark_pre_notified(event_id, 30, db_path=db_path)
    main_marked = schedule_bot.mark_notified(event_id, db_path=db_path)
    with sqlite3.connect(db_path) as con:
        event_flags = con.execute(
            "SELECT notified FROM events WHERE id=?", (event_id,)
        ).fetchone()
        pre_flag = con.execute(
            "SELECT notified FROM event_pre_notifications "
            "WHERE event_id=? AND minutes_before=30",
            (event_id,),
        ).fetchone()
    delivery_marked = bool(
        pre_marked
        and main_marked
        and event_flags is not None
        and event_flags[0] == 1
        and pre_flag is not None
        and pre_flag[0] == 1
    )

    next_when = schedule_bot.advance_recurring(event_id, db_path=db_path)
    with sqlite3.connect(db_path) as con:
        advanced = con.execute(
            "SELECT when_at,notified,pre_notified FROM events WHERE id=?",
            (event_id,),
        ).fetchone()
        pre_flags = con.execute(
            "SELECT notified FROM event_pre_notifications WHERE event_id=?",
            (event_id,),
        ).fetchall()
    recurrence_advanced = bool(
        next_when
        and advanced is not None
        and advanced[0] == next_when
        and next_when != original_when
        and advanced[1:] == (0, 0)
        and pre_flags
        and all(row[0] == 0 for row in pre_flags)
    )
    return delivery_marked, recurrence_advanced


def rehearse_schedule_write_rollback(
    manifest_path: Path,
    workspace: Path,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> ScheduleWriteRollbackReport:
    """Exercise API ownership, notifier writes, rollback, and restore on copies."""
    checked_at = now or datetime.now().astimezone()
    if checked_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if max_backup_age <= timedelta(0):
        raise ValueError("max_backup_age must be positive")
    try:
        work = _require_empty_private_workspace(Path(workspace).resolve())
        backup_path, baseline_signature = _load_verified_backup(
            Path(manifest_path).resolve(),
            now=checked_at,
            max_backup_age=max_backup_age,
        )
    except PrivateWriteRollbackError as exc:
        raise ScheduleWriteRollbackError(exc.code) from exc
    backup_before = (_sha256(backup_path), backup_path.stat().st_mtime_ns)

    active_dir = work / "active-clone"
    restored_dir = work / "emergency-restore"
    active_dir.mkdir(mode=0o700)
    restored_dir.mkdir(mode=0o700)
    active_db = active_dir / "assistant.db"
    restored_db = restored_dir / "assistant.db"
    backup_database(backup_path, active_db)

    writer = ScheduleWriteStore(active_db, writes_enabled=True)
    request_id = str(uuid.uuid4())
    approval_id = str(uuid.uuid4())
    intent = validate_schedule_write_intent(
        operation="schedule.add",
        chat_id=REHEARSAL_CHAT_ID,
        request_id=request_id,
        approval_id=approval_id,
        idempotency_key=f"schedule-rollback-{uuid.uuid4().hex}",
        expected_version=writer.current_version(REHEARSAL_CHAT_ID),
        payload={
            "title": "Schedule API rollback rehearsal",
            "when_at": "2099-01-05T10:00:00",
            "rrule_freq": "weekly",
            "rrule_byday": "MO",
            "pre_notify_minutes": [30, 5],
        },
    )
    writer.submit(intent)
    writer.approve(
        approval_id,
        request_id=request_id,
        scope_value=REHEARSAL_CHAT_ID,
    )
    applied = writer.apply(
        approval_id,
        request_id=request_id,
        scope_value=REHEARSAL_CHAT_ID,
    )
    api_event_id = int(applied.result["id"])
    api_event = schedule_bot.get_event(api_event_id, db_path=active_db)

    notifier_marked, notifier_advanced = _notifier_rehearsal(
        active_db,
        api_event_id,
        str(applied.result["when_at"]),
    )

    legacy_event_id = schedule_bot.add_event(
        "Legacy schedule rollback rehearsal",
        "2099-02-01T09:00:00",
        chat_id=REHEARSAL_CHAT_ID,
        db_path=active_db,
    )
    legacy_event = schedule_bot.get_event(legacy_event_id, db_path=active_db)
    api_after_rollback = schedule_bot.get_event(api_event_id, db_path=active_db)

    active_signature = verify_database(active_db)
    backup_database(backup_path, restored_db)
    restored_signature = verify_database(restored_db)
    backup_after = (_sha256(backup_path), backup_path.stat().st_mtime_ns)

    report = ScheduleWriteRollbackReport(
        api_write_applied=applied.state == "applied" and api_event is not None,
        notifier_marked_delivery=notifier_marked,
        notifier_advanced_recurrence=notifier_advanced,
        legacy_user_writer_resumed=legacy_event is not None,
        service_rollback_preserved_api_write=api_after_rollback is not None,
        emergency_restore_verified=(
            restored_signature == baseline_signature
            and active_signature != baseline_signature
        ),
        backup_unchanged=backup_before == backup_after,
    )
    if not report.verified:
        raise ScheduleWriteRollbackError("rehearsal_verification_failed")
    return report
