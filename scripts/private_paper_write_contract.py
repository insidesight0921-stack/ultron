#!/usr/bin/env python3
"""Pure, slot-scoped contract for future Private Paper buy/sell writes.

This module normalizes one simulated-trading request and binds it to a stable
numeric slot scope.  It does not open SQLite, expose HTTP routes, or activate
an operational writer.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

from private_write_contract import WriteContractError, WriteIntent, validate_write_intent


PAPER_TRADE_OPERATIONS = frozenset({"paper.buy", "paper.sell"})
MAX_NAME_LENGTH = 200
MAX_NOTES_LENGTH = 10_000
MAX_QUANTITY = 2**31 - 1
MAX_MONEY = 10**15
_TICKER_RE = re.compile(r"^[0-9]{6}$")


@dataclass(frozen=True)
class PaperTradeWriteIntent:
    """Validated Paper intent bound to one numeric slot without plaintext repr."""

    intent: WriteIntent = field(repr=False)
    scope_hash: str
    _slot_id: int = field(repr=False)

    @property
    def slot_id(self) -> int:
        return self._slot_id

    @property
    def fingerprint(self) -> str:
        encoded = f"paper-trade-write-v1\0{self.intent.fingerprint}\0{self.scope_hash}"
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_paper_slot_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WriteContractError(
            "invalid_paper_slot", "Paper trade slot must be a positive integer"
        )
    return value


def paper_slot_scope_hash(slot_id: object) -> str:
    normalized = normalize_paper_slot_id(slot_id)
    return hashlib.sha256(f"paper-slot-v1:{normalized}".encode("utf-8")).hexdigest()


def _text(
    value: object,
    *,
    field_name: str,
    maximum: int,
    required: bool,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise WriteContractError("invalid_paper_payload", f"{field_name} must be text")
    normalized = value.strip()
    if (required and not normalized) or len(normalized) > maximum:
        raise WriteContractError("invalid_paper_payload", f"{field_name} is invalid")
    return normalized or None


def _quantity(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_QUANTITY
    ):
        raise WriteContractError(
            "invalid_paper_quantity", "Paper trade quantity is out of range"
        )
    return value


def _money(value: object, *, field_name: str, allow_zero: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WriteContractError("invalid_paper_money", f"{field_name} must be numeric")
    normalized = float(value)
    lower_ok = normalized >= 0 if allow_zero else normalized > 0
    if not math.isfinite(normalized) or not lower_ok or normalized > MAX_MONEY:
        raise WriteContractError("invalid_paper_money", f"{field_name} is out of range")
    return normalized


def _normalize_payload(operation: str, payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise WriteContractError("invalid_paper_payload", "Paper payload must be an object")
    required = (
        {"slot", "ticker", "name", "quantity", "price"}
        if operation == "paper.buy"
        else {"slot", "ticker", "quantity", "price"}
    )
    allowed = required | {"fees", "notes"}
    if set(payload) - allowed:
        raise WriteContractError(
            "unknown_field", "Paper payload contains fields outside the allowlist"
        )
    if required - set(payload):
        raise WriteContractError("missing_field", "Paper payload is missing required fields")

    slot_id = normalize_paper_slot_id(payload["slot"])
    ticker = payload["ticker"]
    if not isinstance(ticker, str) or _TICKER_RE.fullmatch(ticker.strip()) is None:
        raise WriteContractError("invalid_paper_ticker", "Paper ticker must be six digits")
    normalized: dict[str, object] = {
        "slot": slot_id,
        "ticker": ticker.strip(),
        "quantity": _quantity(payload["quantity"]),
        "price": _money(payload["price"], field_name="price", allow_zero=False),
    }
    if operation == "paper.buy":
        normalized["name"] = _text(
            payload["name"], field_name="name", maximum=MAX_NAME_LENGTH, required=True
        )
    if "fees" in payload:
        normalized["fees"] = _money(
            payload["fees"], field_name="fees", allow_zero=True
        )
    if "notes" in payload:
        notes = _text(
            payload["notes"], field_name="notes", maximum=MAX_NOTES_LENGTH, required=False
        )
        if notes is not None:
            normalized["notes"] = notes
    return normalized


def validate_paper_trade_write_intent(
    *,
    operation: object,
    request_id: object,
    idempotency_key: object,
    approval_id: object,
    expected_version: object,
    payload: object,
) -> PaperTradeWriteIntent:
    operation_n = str(operation or "").strip()
    if operation_n not in PAPER_TRADE_OPERATIONS:
        raise WriteContractError("operation_not_allowed", "operation is not a Paper trade")
    normalized_payload = _normalize_payload(operation_n, payload)
    intent = validate_write_intent(
        operation=operation_n,
        request_id=request_id,
        idempotency_key=idempotency_key,
        approval_id=approval_id,
        expected_version=expected_version,
        payload=normalized_payload,
    )
    slot_id = int(normalized_payload["slot"])
    return PaperTradeWriteIntent(
        intent=intent,
        scope_hash=paper_slot_scope_hash(slot_id),
        _slot_id=slot_id,
    )
