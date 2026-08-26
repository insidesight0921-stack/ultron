#!/usr/bin/env python3
"""Deterministic, non-plaintext write identities for Telegram mutations."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from private_write_contract import normalize_idempotency_key


_REQUEST_NAMESPACE = uuid.UUID("6b46c1db-732d-51e5-a312-160b26025ddf")
_APPROVAL_NAMESPACE = uuid.UUID("dda5c1ad-42eb-54e6-88d6-e70007ee0504")
_WATCHLIST_ACTIONS = frozenset({"add", "remove"})
_SCHEDULE_ACTIONS = frozenset({"add", "delete", "complete"})


@dataclass(frozen=True)
class TelegramWriteIdentity:
    operation: str
    request_id: str
    approval_id: str
    idempotency_key: str


def _required_int(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int = 2**63 - 1,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if (
        str(normalized) != str(value).strip()
        or normalized < minimum
        or normalized > maximum
    ):
        raise ValueError(f"{field} is out of range")
    return normalized


def build_watchlist_write_identity(
    *,
    update_id: object,
    chat_id: object,
    user_id: object,
    message_id: object,
    action: object,
) -> TelegramWriteIdentity:
    action_n = str(action or "").strip().lower()
    if action_n not in _WATCHLIST_ACTIONS:
        raise ValueError("watchlist mutation action must be add or remove")
    update_id_n = _required_int(update_id, field="update_id", minimum=0)
    chat_id_n = _required_int(chat_id, field="chat_id", minimum=-(2**63))
    if chat_id_n == 0:
        raise ValueError("chat_id must not be zero")
    user_id_n = _required_int(user_id, field="user_id", minimum=1)
    message_id_n = _required_int(message_id, field="message_id", minimum=1)
    operation = f"watchlist.{action_n}"
    canonical = (
        f"telegram-watchlist-v1:{update_id_n}:{chat_id_n}:"
        f"{user_id_n}:{message_id_n}:{operation}"
    )
    request_id = str(uuid.uuid5(_REQUEST_NAMESPACE, canonical))
    approval_id = str(uuid.uuid5(_APPROVAL_NAMESPACE, canonical))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    idempotency_key = normalize_idempotency_key(f"tg-watchlist-{digest[:48]}")
    return TelegramWriteIdentity(
        operation=operation,
        request_id=request_id,
        approval_id=approval_id,
        idempotency_key=idempotency_key,
    )


def build_schedule_write_identity(
    *,
    update_id: object,
    chat_id: object,
    user_id: object,
    message_id: object,
    action: object,
) -> TelegramWriteIdentity:
    action_n = str(action or "").strip().lower()
    if action_n not in _SCHEDULE_ACTIONS:
        raise ValueError("schedule mutation action must be add, delete, or complete")
    update_id_n = _required_int(update_id, field="update_id", minimum=0)
    chat_id_n = _required_int(chat_id, field="chat_id", minimum=-(2**63))
    if chat_id_n == 0:
        raise ValueError("chat_id must not be zero")
    user_id_n = _required_int(user_id, field="user_id", minimum=1)
    message_id_n = _required_int(message_id, field="message_id", minimum=1)
    operation = f"schedule.{action_n}"
    canonical = (
        f"telegram-schedule-v1:{update_id_n}:{chat_id_n}:"
        f"{user_id_n}:{message_id_n}:{operation}"
    )
    request_id = str(uuid.uuid5(_REQUEST_NAMESPACE, canonical))
    approval_id = str(uuid.uuid5(_APPROVAL_NAMESPACE, canonical))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    idempotency_key = normalize_idempotency_key(f"tg-schedule-{digest[:48]}")
    return TelegramWriteIdentity(
        operation=operation,
        request_id=request_id,
        approval_id=approval_id,
        idempotency_key=idempotency_key,
    )


def validate_watchlist_write_identity(
    identity: TelegramWriteIdentity,
    *,
    action: object,
) -> TelegramWriteIdentity:
    if not isinstance(identity, TelegramWriteIdentity):
        raise ValueError("Telegram write identity is required")
    action_n = str(action or "").strip().lower()
    if identity.operation != f"watchlist.{action_n}":
        raise ValueError("Telegram write identity operation does not match")
    try:
        uuid.UUID(identity.request_id)
        uuid.UUID(identity.approval_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Telegram write identity UUID is invalid") from exc
    normalize_idempotency_key(identity.idempotency_key)
    return identity


def validate_schedule_write_identity(
    identity: TelegramWriteIdentity,
    *,
    action: object,
) -> TelegramWriteIdentity:
    if not isinstance(identity, TelegramWriteIdentity):
        raise ValueError("Telegram write identity is required")
    action_n = str(action or "").strip().lower()
    if action_n not in _SCHEDULE_ACTIONS:
        raise ValueError("schedule mutation action must be add, delete, or complete")
    if identity.operation != f"schedule.{action_n}":
        raise ValueError("Telegram write identity operation does not match")
    try:
        uuid.UUID(identity.request_id)
        uuid.UUID(identity.approval_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Telegram write identity UUID is invalid") from exc
    normalize_idempotency_key(identity.idempotency_key)
    return identity
