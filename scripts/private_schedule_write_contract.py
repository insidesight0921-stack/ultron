#!/usr/bin/env python3
"""Pure, chat-scoped contract for future Private schedule mutations.

This module validates and fingerprints schedule intents only.  It does not
open SQLite, expose HTTP routes, activate a writer, or change a service.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from private_write_contract import WriteContractError, WriteIntent, validate_write_intent


SCHEDULE_WRITE_OPERATIONS = frozenset(
    {"schedule.add", "schedule.delete", "schedule.complete"}
)
VALID_RRULE_FREQS = frozenset({"daily", "weekly", "monthly"})
VALID_RRULE_DAYS = frozenset({"MO", "TU", "WE", "TH", "FR", "SA", "SU"})
MAX_TITLE_LENGTH = 300
MAX_NOTES_LENGTH = 10_000
MAX_PRE_NOTIFY_MINUTES = 525_600


@dataclass(frozen=True)
class ScheduleWriteIntent:
    """Validated generic intent bound to one private Telegram chat scope."""

    intent: WriteIntent
    scope_hash: str
    _chat_id: str = field(repr=False)

    @property
    def chat_id(self) -> str:
        return self._chat_id

    @property
    def fingerprint(self) -> str:
        encoded = f"schedule-write-v1\0{self.intent.fingerprint}\0{self.scope_hash}"
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_schedule_chat_id(value: object) -> str:
    raw = str(value or "").strip()
    digits = raw[1:] if raw.startswith("-") else raw
    if not digits.isdigit() or not digits or len(digits) > 19:
        raise WriteContractError("invalid_schedule_scope", "schedule chat scope is invalid")
    number = int(raw)
    if number == 0 or not -(2**63) <= number <= 2**63 - 1:
        raise WriteContractError("invalid_schedule_scope", "schedule chat scope is invalid")
    return str(number)


def schedule_scope_hash(chat_id: object) -> str:
    normalized = normalize_schedule_chat_id(chat_id)
    return hashlib.sha256(f"schedule-chat-v1:{normalized}".encode("utf-8")).hexdigest()


def _iso_local(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WriteContractError("invalid_schedule_time", f"{field_name} must be ISO datetime")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise WriteContractError(
            "invalid_schedule_time", f"{field_name} must be ISO datetime"
        ) from exc
    if parsed.tzinfo is not None:
        raise WriteContractError(
            "invalid_schedule_time", f"{field_name} must use local naive time"
        )
    return parsed.isoformat(timespec="seconds")


def _optional_text(value: object, *, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WriteContractError("invalid_schedule_payload", f"{field_name} must be text")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise WriteContractError("invalid_schedule_payload", f"{field_name} is too long")
    return normalized or None


def _pre_notify(value: object) -> int | list[int]:
    if isinstance(value, bool):
        raise WriteContractError(
            "invalid_schedule_pre_notify", "pre_notify_minutes must be positive integers"
        )
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    normalized: set[int] = set()
    for item in values:
        if isinstance(item, bool) or not isinstance(item, int):
            raise WriteContractError(
                "invalid_schedule_pre_notify", "pre_notify_minutes must be positive integers"
            )
        if not 1 <= item <= MAX_PRE_NOTIFY_MINUTES:
            raise WriteContractError(
                "invalid_schedule_pre_notify", "pre_notify_minutes is out of range"
            )
        normalized.add(item)
    if not normalized:
        raise WriteContractError(
            "invalid_schedule_pre_notify", "pre_notify_minutes must not be empty"
        )
    ordered = sorted(normalized, reverse=True)
    return ordered[0] if len(ordered) == 1 else ordered


def _normalize_add_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise WriteContractError("invalid_schedule_payload", "schedule payload must be an object")
    allowed = {
        "title",
        "when_at",
        "notes",
        "rrule_freq",
        "rrule_byday",
        "rrule_until",
        "pre_notify_minutes",
    }
    if set(payload) - allowed:
        raise WriteContractError(
            "unknown_field", "schedule payload contains fields outside the allowlist"
        )
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE_LENGTH:
        raise WriteContractError("invalid_schedule_title", "schedule title is invalid")
    when_at = _iso_local(payload.get("when_at"), field_name="when_at")
    normalized: dict[str, object] = {"title": title.strip(), "when_at": when_at}

    notes = _optional_text(payload.get("notes"), field_name="notes", max_length=MAX_NOTES_LENGTH)
    if notes is not None:
        normalized["notes"] = notes

    freq_value = payload.get("rrule_freq")
    if freq_value is not None:
        if not isinstance(freq_value, str) or freq_value.strip().lower() not in VALID_RRULE_FREQS:
            raise WriteContractError("invalid_schedule_rrule", "rrule_freq is invalid")
        freq = freq_value.strip().lower()
        normalized["rrule_freq"] = freq
        byday_value = payload.get("rrule_byday")
        if byday_value is not None:
            if not isinstance(byday_value, str) or freq != "weekly":
                raise WriteContractError("invalid_schedule_rrule", "rrule_byday requires weekly")
            days = [part.strip().upper() for part in byday_value.split(",")]
            if not days or any(day not in VALID_RRULE_DAYS for day in days):
                raise WriteContractError("invalid_schedule_rrule", "rrule_byday is invalid")
            ordered_days = list(dict.fromkeys(days))
            normalized["rrule_byday"] = ",".join(ordered_days)
        until_value = payload.get("rrule_until")
        if until_value is not None:
            until = _iso_local(until_value, field_name="rrule_until")
            if until < when_at:
                raise WriteContractError("invalid_schedule_rrule", "rrule_until precedes when_at")
            normalized["rrule_until"] = until
    elif payload.get("rrule_byday") is not None or payload.get("rrule_until") is not None:
        raise WriteContractError("invalid_schedule_rrule", "rrule options require rrule_freq")

    if "pre_notify_minutes" in payload:
        normalized["pre_notify_minutes"] = _pre_notify(payload["pre_notify_minutes"])
    return normalized


def _normalize_event_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {"event_id"}:
        raise WriteContractError(
            "invalid_schedule_event", "schedule event mutation requires only event_id"
        )
    event_id = payload["event_id"]
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
        raise WriteContractError("invalid_schedule_event", "event_id must be a positive integer")
    return {"event_id": event_id}


def validate_schedule_write_intent(
    *,
    operation: object,
    chat_id: object,
    request_id: object,
    idempotency_key: object,
    approval_id: object,
    expected_version: object,
    payload: object,
) -> ScheduleWriteIntent:
    operation_n = str(operation or "").strip()
    if operation_n not in SCHEDULE_WRITE_OPERATIONS:
        raise WriteContractError("operation_not_allowed", "operation is not a schedule mutation")
    normalized_payload = (
        _normalize_add_payload(payload)
        if operation_n == "schedule.add"
        else _normalize_event_payload(payload)
    )
    chat_id_n = normalize_schedule_chat_id(chat_id)
    intent = validate_write_intent(
        operation=operation_n,
        request_id=request_id,
        idempotency_key=idempotency_key,
        approval_id=approval_id,
        expected_version=expected_version,
        payload=normalized_payload,
    )
    scope_hash = schedule_scope_hash(chat_id_n)
    return ScheduleWriteIntent(intent=intent, scope_hash=scope_hash, _chat_id=chat_id_n)


def schedule_audit_identity(intent: ScheduleWriteIntent) -> dict[str, str]:
    """Return only non-plaintext identifiers suitable for audit metadata."""
    return {
        "request_fingerprint": intent.fingerprint,
        "scope_hash": intent.scope_hash,
        "payload_hash": hashlib.sha256(
            json.dumps(
                intent.intent.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
