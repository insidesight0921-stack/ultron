#!/usr/bin/env python3
"""Default-disabled, chat-scoped schedule write store for isolated tests."""
from __future__ import annotations

import sqlite3

import schedule_bot
from private_schedule_write_contract import (
    SCHEDULE_WRITE_OPERATIONS,
    ScheduleWriteIntent,
    normalize_schedule_chat_id,
    schedule_scope_hash,
    validate_schedule_write_intent,
)
from private_scoped_write_store import ScopedApprovalWriteStore, ScopedWriteIntent
from private_write_contract import WriteContractError, WriteIntent


class ScheduleWriteStore(ScopedApprovalWriteStore):
    resource_name = "schedule"
    allowed_operations = SCHEDULE_WRITE_OPERATIONS

    def _ensure_domain_schema(self, con: sqlite3.Connection) -> None:
        schedule_bot._ensure_schema(con)

    def _normalize_bound_intent(self, intent: object) -> ScopedWriteIntent:
        if not isinstance(intent, ScheduleWriteIntent):
            raise WriteContractError(
                "invalid_schedule_intent", "validated chat-scoped schedule intent is required"
            )
        normalized = validate_schedule_write_intent(
            operation=intent.intent.operation,
            chat_id=intent.chat_id,
            request_id=intent.intent.request_id,
            idempotency_key=intent.intent.idempotency_key,
            approval_id=intent.intent.approval_id,
            expected_version=intent.intent.expected_version,
            payload=intent.intent.payload,
        )
        return ScopedWriteIntent(
            intent=normalized.intent,
            scope_hash=normalized.scope_hash,
            scope_value=normalized.chat_id,
            fingerprint=normalized.fingerprint,
        )

    def _normalize_scope(self, scope_value: object) -> tuple[str, str]:
        chat_id = normalize_schedule_chat_id(scope_value)
        return chat_id, schedule_scope_hash(chat_id)

    @staticmethod
    def _event_result(con: sqlite3.Connection, event_id: int) -> dict[str, object]:
        row = con.execute(
            """SELECT id,title,when_at,notes,completed,rrule_freq,rrule_byday,
                      rrule_until,pre_notify_minutes FROM events WHERE id=?""",
            (event_id,),
        ).fetchone()
        pre = con.execute(
            "SELECT minutes_before FROM event_pre_notifications WHERE event_id=? "
            "ORDER BY minutes_before DESC",
            (event_id,),
        ).fetchall()
        return {
            "id": int(row["id"]),
            "title": str(row["title"]),
            "when_at": str(row["when_at"]),
            "notes": row["notes"],
            "completed": bool(row["completed"]),
            "rrule_freq": row["rrule_freq"],
            "rrule_byday": row["rrule_byday"],
            "rrule_until": row["rrule_until"],
            "pre_notify_minutes": int(row["pre_notify_minutes"] or 0),
            "pre_notify_minutes_list": [int(item["minutes_before"]) for item in pre],
        }

    def _apply_domain(
        self,
        con: sqlite3.Connection,
        intent: WriteIntent,
        scope_value: str,
    ) -> tuple[dict[str, object], bool]:
        if intent.operation == "schedule.add":
            payload = intent.payload
            pre_values = schedule_bot._normalize_pre_notify_minutes(
                payload.get("pre_notify_minutes")
            )
            legacy_pre = max(pre_values) if pre_values else 0
            cur = con.execute(
                """INSERT INTO events(
                       title,when_at,notes,chat_id,rrule_freq,rrule_byday,
                       rrule_until,pre_notify_minutes
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    payload["title"],
                    payload["when_at"],
                    payload.get("notes"),
                    scope_value,
                    payload.get("rrule_freq"),
                    payload.get("rrule_byday"),
                    payload.get("rrule_until"),
                    legacy_pre,
                ),
            )
            event_id = int(cur.lastrowid)
            for minutes in pre_values:
                con.execute(
                    "INSERT INTO event_pre_notifications(event_id,minutes_before,notified) "
                    "VALUES (?,?,0)",
                    (event_id, minutes),
                )
            result = self._event_result(con, event_id)
            result["created"] = True
            return result, True

        event_id = int(intent.payload["event_id"])
        owned = con.execute(
            "SELECT 1 FROM events WHERE id=? AND chat_id=?",
            (event_id, scope_value),
        ).fetchone()
        if owned is None:
            key = "deleted" if intent.operation == "schedule.delete" else "completed"
            return {"event_id": event_id, key: False}, False
        if intent.operation == "schedule.delete":
            con.execute("DELETE FROM event_pre_notifications WHERE event_id=?", (event_id,))
            con.execute("DELETE FROM events WHERE id=? AND chat_id=?", (event_id, scope_value))
            return {"event_id": event_id, "deleted": True}, True

        changed = (
            con.execute(
                "UPDATE events SET completed=1 WHERE id=? AND chat_id=? AND completed=0",
                (event_id, scope_value),
            ).rowcount
            > 0
        )
        return {"event_id": event_id, "completed": changed}, changed
