#!/usr/bin/env python3
"""Pure, storage-free contract primitives for future Private API writes.

This module deliberately does not open SQLite, expose HTTP routes, or apply a
mutation.  It only validates the envelope that a later write implementation
must satisfy.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping


MAX_PAYLOAD_BYTES = 32_768
MAX_EXPECTED_VERSION = 2**63 - 1
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
APPROVAL_STATES = frozenset({"pending", "approved", "applied", "rejected", "expired"})
AUDIT_RESULTS = frozenset(
    {"pending", "approved", "applied", "replayed", "rejected", "expired", "conflict", "failed"}
)
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "authorization",
        "token",
        "api_token",
        "api_key",
        "secret",
        "password",
        "sql",
        "query",
        "db_path",
        "database_path",
        "table",
        "table_name",
    }
)


class WriteContractError(ValueError):
    """A stable, non-sensitive contract validation error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OperationSpec:
    domain: str
    action: str
    required_fields: frozenset[str]
    optional_fields: frozenset[str] = frozenset()
    approval_required: bool = True

    @property
    def name(self) -> str:
        return f"{self.domain}.{self.action}"


def _spec(
    domain: str,
    action: str,
    required: set[str],
    optional: set[str] | None = None,
) -> OperationSpec:
    return OperationSpec(
        domain=domain,
        action=action,
        required_fields=frozenset(required),
        optional_fields=frozenset(optional or set()),
    )


OPERATION_SPECS = {
    spec.name: spec
    for spec in (
        _spec("watchlist", "add", {"ticker", "name"}),
        _spec("watchlist", "remove", {"ticker"}),
        _spec(
            "schedule",
            "add",
            {"title", "when_at"},
            {
                "notes",
                "rrule_freq",
                "rrule_byday",
                "rrule_until",
                "pre_notify_minutes",
            },
        ),
        _spec("schedule", "delete", {"event_id"}),
        _spec("schedule", "complete", {"event_id"}),
        _spec(
            "paper",
            "buy",
            {"slot", "ticker", "name", "quantity", "price"},
            {"fees", "notes"},
        ),
        _spec(
            "paper",
            "sell",
            {"slot", "ticker", "quantity", "price"},
            {"fees", "notes"},
        ),
        _spec(
            "paper_ipo",
            "subscribe",
            {"name", "subscribed"},
            {
                "sub_start",
                "sub_end",
                "listing_date",
                "grade",
                "score",
                "factors",
                "alloc_amount",
            },
        ),
        _spec("paper_ipo", "close", {"name", "listing_price"}),
    )
}


APPROVAL_TRANSITIONS = {
    "pending": frozenset({"approved", "rejected", "expired"}),
    "approved": frozenset({"applied", "expired"}),
    "applied": frozenset(),
    "rejected": frozenset(),
    "expired": frozenset(),
}


@dataclass(frozen=True)
class WriteIntent:
    operation: str
    request_id: str
    idempotency_key: str
    approval_id: str
    expected_version: int
    payload: dict[str, object]

    @property
    def spec(self) -> OperationSpec:
        return OPERATION_SPECS[self.operation]

    @property
    def fingerprint(self) -> str:
        body = {
            "approval_id": self.approval_id,
            "expected_version": self.expected_version,
            "operation": self.operation,
            "payload": self.payload,
            "request_id": self.request_id,
        }
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _normalize_uuid(value: object, *, field: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WriteContractError("invalid_identifier", f"{field} must be a UUID") from exc
    return str(parsed)


def normalize_idempotency_key(value: object) -> str:
    key = str(value or "")
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise WriteContractError(
            "invalid_idempotency_key",
            "Idempotency-Key must be 16-128 allowlisted characters",
        )
    return key


def normalize_expected_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WriteContractError("invalid_expected_version", "expected_version must be an integer")
    if not 0 <= value <= MAX_EXPECTED_VERSION:
        raise WriteContractError("invalid_expected_version", "expected_version is out of range")
    return value


def _check_sensitive_keys(value: object) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise WriteContractError("invalid_payload", "payload keys must be strings")
                if key.strip().lower() in SENSITIVE_FIELD_NAMES:
                    raise WriteContractError("forbidden_field", "payload contains a forbidden field")
                pending.append(nested)
        elif isinstance(current, (list, tuple)):
            pending.extend(current)


def _normalize_payload(operation: str, payload: object) -> dict[str, object]:
    spec = OPERATION_SPECS.get(operation)
    if spec is None:
        raise WriteContractError("operation_not_allowed", "operation is not allowlisted")
    if not isinstance(payload, Mapping):
        raise WriteContractError("invalid_payload", "payload must be an object")
    if any(not isinstance(key, str) for key in payload):
        raise WriteContractError("invalid_payload", "payload keys must be strings")
    normalized = dict(payload)
    keys = set(normalized)
    missing = spec.required_fields - keys
    unknown = keys - spec.required_fields - spec.optional_fields
    if missing:
        raise WriteContractError("missing_field", "payload is missing required fields")
    if unknown:
        raise WriteContractError("unknown_field", "payload contains fields outside the allowlist")
    _check_sensitive_keys(normalized)
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WriteContractError("invalid_payload", "payload must be finite JSON data") from exc
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise WriteContractError("payload_too_large", "payload exceeds the size limit")
    return normalized


def validate_write_intent(
    *,
    operation: object,
    request_id: object,
    idempotency_key: object,
    approval_id: object,
    expected_version: object,
    payload: object,
) -> WriteIntent:
    operation_n = str(operation or "").strip()
    payload_n = _normalize_payload(operation_n, payload)
    return WriteIntent(
        operation=operation_n,
        request_id=_normalize_uuid(request_id, field="request_id"),
        idempotency_key=normalize_idempotency_key(idempotency_key),
        approval_id=_normalize_uuid(approval_id, field="approval_id"),
        expected_version=normalize_expected_version(expected_version),
        payload=payload_n,
    )


def validate_approval_transition(current: object, target: object) -> tuple[str, str]:
    current_n = str(current or "").strip().lower()
    target_n = str(target or "").strip().lower()
    if current_n not in APPROVAL_STATES or target_n not in APPROVAL_STATES:
        raise WriteContractError("invalid_approval_state", "approval state is not valid")
    if target_n not in APPROVAL_TRANSITIONS[current_n]:
        raise WriteContractError("invalid_approval_transition", "approval transition is not allowed")
    return current_n, target_n


def validate_idempotency_replay(existing_fingerprint: object, intent: WriteIntent) -> str:
    existing = str(existing_fingerprint or "")
    if len(existing) != 64 or not hmac_safe_equal(existing, intent.fingerprint):
        raise WriteContractError(
            "idempotency_conflict",
            "Idempotency-Key was already used for a different request",
        )
    return "replayed"


def hmac_safe_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def build_audit_event(
    intent: WriteIntent,
    *,
    result: object,
    occurred_at: datetime | None = None,
    resource_version: int | None = None,
    error_code: str | None = None,
) -> dict[str, object]:
    result_n = str(result or "").strip().lower()
    if result_n not in AUDIT_RESULTS:
        raise WriteContractError("invalid_audit_result", "audit result is not valid")
    if resource_version is not None:
        resource_version = normalize_expected_version(resource_version)
    timestamp = occurred_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise WriteContractError("invalid_audit_time", "audit time must include a timezone")
    event: dict[str, object] = {
        "request_id": intent.request_id,
        "approval_id": intent.approval_id,
        "idempotency_key_hash": hashlib.sha256(intent.idempotency_key.encode("utf-8")).hexdigest(),
        "domain": intent.spec.domain,
        "action": intent.spec.action,
        "result": result_n,
        "occurred_at": timestamp.astimezone(timezone.utc).isoformat(),
    }
    if resource_version is not None:
        event["resource_version"] = resource_version
    if error_code:
        error_code_n = str(error_code)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code_n):
            raise WriteContractError("invalid_error_code", "audit error code is not valid")
        event["error_code"] = error_code_n
    return event
