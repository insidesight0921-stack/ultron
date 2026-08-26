#!/usr/bin/env python3
"""Default-disabled consumer for slot-scoped Private Paper writes.

The executor deliberately has no direct Paper DB fallback.  It composes a
retry-stable caller identity with the Private API preflight/approval/apply
protocol, and remains absent from operational UI/Telegram wiring until a
separate cutover decision is made.
"""
from __future__ import annotations

from dataclasses import dataclass

from paper_trade_identity import (
    PAPER_IDENTITY_CALLERS,
    PaperTradeWriteIdentity,
    validate_paper_trade_write_identity,
)
from private_data_api_client import PrivateDataClient
from private_paper_write_contract import validate_paper_trade_write_intent
from private_write_readiness import require_private_write_activation_permit


class PaperTradeConsumerError(RuntimeError):
    pass


class PaperTradeConsumerDisabled(PaperTradeConsumerError):
    pass


class PaperTradeApprovalRequired(PaperTradeConsumerError):
    pass


class PaperTradeTerminalState(PaperTradeConsumerError):
    def __init__(self, state: str) -> None:
        super().__init__("Paper trade write intent is already terminal")
        self.state = state


@dataclass(frozen=True)
class PaperTradeExecutionResult:
    operation: str
    state: str
    result: dict[str, object]
    replayed: bool
    request_id: str
    approval_id: str


class PaperTradeWriteExecutor:
    """Compose caller scope, approval policy, and one-time Private API apply."""

    def __init__(
        self,
        client: PrivateDataClient,
        *,
        enabled: bool = False,
        paper_write_activation_permit=None,
    ) -> None:
        self.client = client
        self.enabled = bool(enabled)
        if self.enabled:
            client_permit = require_private_write_activation_permit(
                getattr(client, "paper_write_activation_permit", None)
            )
            self.paper_write_activation_permit = require_private_write_activation_permit(
                paper_write_activation_permit,
                matching_permit=client_permit,
            )
        elif paper_write_activation_permit is not None:
            raise ValueError("Paper write activation permit requires enabled executor")
        else:
            self.paper_write_activation_permit = None

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise PaperTradeConsumerDisabled("Paper trade write executor is disabled")
        if not bool(getattr(self.client, "paper_writes_enabled", False)):
            raise PaperTradeConsumerDisabled("Private Paper write client is disabled")

    def require_ready(
        self,
        *,
        caller: object,
        action: object,
        user_approved: bool,
        policy_approved: bool,
    ) -> tuple[str, str]:
        self._require_enabled()
        caller_n = str(caller or "").strip().lower()
        action_n = str(action or "").strip().lower()
        if caller_n not in PAPER_IDENTITY_CALLERS:
            raise ValueError("Paper write caller is not allowed")
        if action_n not in {"buy", "sell"}:
            raise ValueError("Paper trade action is not allowed")
        if caller_n == "telegram-intraday":
            if action_n != "sell":
                raise ValueError("Telegram intraday policy permits Paper sells only")
            if policy_approved is not True or user_approved is True:
                raise PaperTradeApprovalRequired(
                    "intraday Paper sell requires policy approval only"
                )
        elif user_approved is not True or policy_approved is True:
            raise PaperTradeApprovalRequired(
                "explicit user approval is required for this Paper caller"
            )
        return caller_n, action_n

    @staticmethod
    def _raw_payload(
        action: str,
        *,
        slot_id: object,
        ticker: object,
        name: object,
        quantity: object,
        price: object,
        fees: object,
        notes: object,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "slot": slot_id,
            "ticker": ticker,
            "quantity": quantity,
            "price": price,
        }
        if action == "buy":
            payload["name"] = name
        if fees is not None:
            payload["fees"] = fees
        if notes is not None:
            payload["notes"] = notes
        return payload

    @staticmethod
    def _completed(
        record: dict[str, object],
        *,
        replayed: bool,
    ) -> PaperTradeExecutionResult:
        result = record.get("result")
        if record.get("state") != "applied" or not isinstance(result, dict):
            raise PaperTradeConsumerError(
                "Paper trade write did not produce an applied result"
            )
        return PaperTradeExecutionResult(
            operation=str(record["operation"]),
            state="applied",
            result=dict(result),
            replayed=replayed,
            request_id=str(record["request_id"]),
            approval_id=str(record["approval_id"]),
        )

    def execute(
        self,
        *,
        caller: str,
        action: str,
        slot_id: int,
        identity: PaperTradeWriteIdentity,
        user_approved: bool = False,
        policy_approved: bool = False,
        ticker: str,
        quantity: int,
        price: float,
        name: str | None = None,
        fees: float | None = None,
        notes: str | None = None,
    ) -> PaperTradeExecutionResult:
        caller_n, action_n = self.require_ready(
            caller=caller,
            action=action,
            user_approved=user_approved,
            policy_approved=policy_approved,
        )
        operation = f"paper.{action_n}"
        identity = validate_paper_trade_write_identity(
            identity,
            caller=caller_n,
            operation=operation,
            slot_id=slot_id,
        )
        bound = validate_paper_trade_write_intent(
            operation=operation,
            request_id=identity.request_id,
            approval_id=identity.approval_id,
            idempotency_key=identity.idempotency_key,
            expected_version=0,
            payload=self._raw_payload(
                action_n,
                slot_id=slot_id,
                ticker=ticker,
                name=name,
                quantity=quantity,
                price=price,
                fees=fees,
                notes=notes,
            ),
        )
        identity_args = {
            "operation": operation,
            "slot_id": bound.slot_id,
            "request_id": identity.request_id,
            "approval_id": identity.approval_id,
            "idempotency_key": identity.idempotency_key,
        }
        preflight = self.client.preflight_paper_trade_write(**identity_args)
        existed = bool(preflight["exists"])
        state = preflight["state"]
        if state == "applied":
            return self._completed(preflight, replayed=True)
        if state in {"rejected", "expired"}:
            raise PaperTradeTerminalState(str(state))

        submitted = self.client.submit_paper_trade_write(
            **identity_args,
            expected_version=int(preflight["expected_version"]),
            payload=bound.intent.payload,
        )
        state = submitted["state"]
        if state == "applied":
            return self._completed(submitted, replayed=True)
        if state in {"rejected", "expired"}:
            raise PaperTradeTerminalState(str(state))

        if state == "pending":
            approved = self.client.approve_paper_trade_write(
                slot_id=bound.slot_id,
                request_id=identity.request_id,
                approval_id=identity.approval_id,
            )
            state = approved["state"]
        if state != "approved":
            raise PaperTradeConsumerError("Paper trade approval state is invalid")

        applied = self.client.apply_paper_trade_write(
            slot_id=bound.slot_id,
            request_id=identity.request_id,
            approval_id=identity.approval_id,
        )
        return self._completed(
            applied,
            replayed=existed or bool(submitted["replayed"]),
        )
