#!/usr/bin/env python3
"""Ownership inventory and deterministic identities for future Paper writes.

This module is pure: it does not import Paper DB code, open SQLite, call the
Private API, or change current Paper UI/Telegram runtime behavior.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from private_paper_write_contract import (
    PAPER_TRADE_OPERATIONS,
    normalize_paper_slot_id,
)
from private_write_contract import normalize_idempotency_key


_REQUEST_NAMESPACE = uuid.UUID("2bb4eeeb-fd2d-5d93-b8c8-3cd672243f53")
_APPROVAL_NAMESPACE = uuid.UUID("4687931c-ecf3-543e-8c6e-da9bd9394e93")
PAPER_IDENTITY_CALLERS = frozenset(
    {"paper-ui", "telegram-intraday", "telegram-kium", "telegram-quant"}
)


@dataclass(frozen=True)
class PaperDirectWriterPolicy:
    key: str
    module: str
    function: str
    operations: frozenset[str]
    current_owner: str
    target_owner: str
    approval_mode: str
    target_mode: str


PAPER_DIRECT_WRITER_INVENTORY = (
    PaperDirectWriterPolicy(
        key="paper-ui-buy",
        module="paper_ui.py",
        function="api_buy",
        operations=frozenset({"paper.buy"}),
        current_owner="paper-ui",
        target_owner="private-data-api",
        approval_mode="explicit-request-gap",
        target_mode="private-client",
    ),
    PaperDirectWriterPolicy(
        key="paper-ui-sell",
        module="paper_ui.py",
        function="api_sell",
        operations=frozenset({"paper.sell"}),
        current_owner="paper-ui",
        target_owner="private-data-api",
        approval_mode="explicit-request-gap",
        target_mode="private-client",
    ),
    PaperDirectWriterPolicy(
        # 2026-08-29 추가. IPO 슬롯 유휴 자본 파킹(단기통안채 매수/매도).
        # 사람 승인 없이 도는 자동 경로이므로 목록에 명시한다.
        key="telegram-idle-cash",
        module="telegram_bot.py",
        function="idle_cash_job",
        operations=PAPER_TRADE_OPERATIONS,
        current_owner="telegram-idle-cash-direct",
        target_owner="private-data-api",
        approval_mode="policy-auto-park-only",
        target_mode="private-client",
    ),
    PaperDirectWriterPolicy(
        # 2026-08-28 추가. 장외 신호 대기 큐가 개장 후 체결할 때도 직접 쓴다.
        # 목록에서 빠져 있었는데, 탐지기가 **호출만** 세고 있어서 드러나지 않았다
        # (`asyncio.to_thread(_pdb.record_buy, ...)`는 호출이 아니라 참조다).
        key="telegram-pending",
        module="telegram_bot.py",
        function="_drain_pending_orders",
        operations=frozenset({"paper.buy"}),
        current_owner="telegram-pending-drain",
        target_owner="private-data-api",
        approval_mode="explicit-user-batch",
        target_mode="private-client",
    ),
    PaperDirectWriterPolicy(
        key="telegram-intraday",
        module="telegram_bot.py",
        function="intraday_monitor_job",
        operations=frozenset({"paper.sell"}),
        current_owner="telegram-intraday-monitor",
        target_owner="private-data-api",
        approval_mode="policy-auto-sell-only",
        target_mode="private-client",
    ),
    PaperDirectWriterPolicy(
        key="telegram-kium",
        module="telegram_bot.py",
        function="handle_kium_paper_callback",
        operations=PAPER_TRADE_OPERATIONS,
        current_owner="telegram-kium-callback",
        target_owner="private-data-api",
        approval_mode="explicit-user-batch",
        target_mode="private-client",
    ),
    PaperDirectWriterPolicy(
        key="telegram-quant",
        module="telegram_bot.py",
        function="handle_quant_paper_callback",
        operations=PAPER_TRADE_OPERATIONS,
        current_owner="telegram-quant-callback",
        target_owner="private-data-api",
        approval_mode="explicit-user-batch",
        target_mode="private-client",
    ),
    PaperDirectWriterPolicy(
        key="operator-cli",
        module="paper_db.py",
        function="_cli",
        operations=PAPER_TRADE_OPERATIONS,
        current_owner="operator-cli",
        target_owner="none",
        approval_mode="operator-manual",
        target_mode="retire-direct-rollback-only",
    ),
    PaperDirectWriterPolicy(
        key="verified-backup-clone-rehearsal",
        module="private_paper_write_rollback.py",
        function="rehearse_paper_write_rollback",
        operations=PAPER_TRADE_OPERATIONS,
        current_owner="verified-backup-clone-rehearsal",
        target_owner="none",
        approval_mode="isolated-rehearsal",
        target_mode="verified-backup-clone-only",
    ),
)


@dataclass(frozen=True)
class PaperTradeWriteIdentity:
    caller: str
    operation: str
    request_id: str
    approval_id: str
    idempotency_key: str
    _slot_id: int = field(repr=False)

    @property
    def slot_id(self) -> int:
        return self._slot_id


def _coordinate(value: object, *, field_name: str) -> str:
    if value is None:
        raise ValueError(f"{field_name} is required")
    normalized = str(value).strip()
    if (
        not normalized
        or len(normalized) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError(f"{field_name} is invalid")
    return normalized


def _digest(value: str, *, domain: str) -> str:
    return hashlib.sha256(f"{domain}\0{value}".encode("utf-8")).hexdigest()


def build_paper_trade_write_identity(
    *,
    caller: object,
    operation: object,
    slot_id: object,
    actor_id: object,
    source_event_id: object,
    item_key: object,
) -> PaperTradeWriteIdentity:
    """Build one retry-stable identity without retaining source coordinates.

    ``source_event_id`` identifies the UI request, Telegram update/callback, or
    scheduled monitor cycle. ``item_key`` is a stable ordinal within a batch;
    payload values such as ticker, quantity, price, and notes must not be used.
    Payload drift is intentionally handled by the write fingerprint conflict.
    """
    caller_n = str(caller or "").strip().lower()
    if caller_n not in PAPER_IDENTITY_CALLERS:
        raise ValueError("Paper write identity caller is not allowed")
    operation_n = str(operation or "").strip().lower()
    if operation_n not in PAPER_TRADE_OPERATIONS:
        raise ValueError("Paper trade write operation is not allowed")
    if caller_n == "telegram-intraday" and operation_n != "paper.sell":
        raise ValueError("Telegram intraday policy permits Paper sells only")
    slot_id_n = normalize_paper_slot_id(slot_id)
    actor_hash = _digest(
        _coordinate(actor_id, field_name="actor_id"),
        domain="paper-actor-v1",
    )
    event_hash = _digest(
        _coordinate(source_event_id, field_name="source_event_id"),
        domain="paper-event-v1",
    )
    item_hash = _digest(
        _coordinate(item_key, field_name="item_key"),
        domain="paper-item-v1",
    )
    canonical = (
        f"paper-trade-identity-v1:{caller_n}:{operation_n}:{slot_id_n}:"
        f"{actor_hash}:{event_hash}:{item_hash}"
    )
    request_id = str(uuid.uuid5(_REQUEST_NAMESPACE, canonical))
    approval_id = str(uuid.uuid5(_APPROVAL_NAMESPACE, canonical))
    identity_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    idempotency_key = normalize_idempotency_key(
        f"paper-{caller_n}-{identity_hash[:48]}"
    )
    return PaperTradeWriteIdentity(
        caller=caller_n,
        operation=operation_n,
        request_id=request_id,
        approval_id=approval_id,
        idempotency_key=idempotency_key,
        _slot_id=slot_id_n,
    )


def validate_paper_trade_write_identity(
    identity: object,
    *,
    caller: object,
    operation: object,
    slot_id: object,
) -> PaperTradeWriteIdentity:
    if not isinstance(identity, PaperTradeWriteIdentity):
        raise ValueError("Paper trade write identity is required")
    caller_n = str(caller or "").strip().lower()
    operation_n = str(operation or "").strip().lower()
    slot_id_n = normalize_paper_slot_id(slot_id)
    if (
        identity.caller != caller_n
        or identity.operation != operation_n
        or identity.slot_id != slot_id_n
    ):
        raise ValueError("Paper trade write identity scope does not match")
    if caller_n not in PAPER_IDENTITY_CALLERS or operation_n not in PAPER_TRADE_OPERATIONS:
        raise ValueError("Paper trade write identity is not allowed")
    try:
        uuid.UUID(identity.request_id)
        uuid.UUID(identity.approval_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Paper trade write identity UUID is invalid") from exc
    normalize_idempotency_key(identity.idempotency_key)
    return identity
