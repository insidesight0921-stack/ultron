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
    PaperTradeWriteIdentity,
    validate_paper_trade_write_identity,
)
from idle_cash import PARK_SLOT, PARK_TICKER
from private_data_api_client import PrivateDataClient
from private_paper_write_contract import validate_paper_trade_write_intent
from private_paper_write_readiness import (
    PAPER_PARK_APPROVAL_MODE,
    PAPER_POLICY_APPROVAL_MODES,
    paper_caller_policy,
)
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
        ticker: object = None,
    ) -> tuple[str, str]:
        self._require_enabled()
        caller_n = str(caller or "").strip().lower()
        action_n = str(action or "").strip().lower()
        # 규칙은 활성화 번들이 증명하는 표 하나에서 읽는다(2026-11-10) — 호출자별 if 분기를
        # 여기 따로 두면 그 표와 어긋난다(10-28에 실제로 어긋났다).
        policy = paper_caller_policy(caller_n)
        if policy is None:
            raise ValueError("Paper write caller is not allowed")
        if action_n not in {"buy", "sell"}:
            raise ValueError("Paper trade action is not allowed")
        if f"paper.{action_n}" not in policy.operations:
            raise ValueError(f"Paper caller {caller_n} does not permit paper.{action_n}")
        if policy.approval_mode in PAPER_POLICY_APPROVAL_MODES:
            if policy_approved is not True or user_approved is True:
                raise PaperTradeApprovalRequired(
                    f"{caller_n} Paper write requires policy approval only"
                )
        elif user_approved is not True or policy_approved is True:
            raise PaperTradeApprovalRequired(
                "explicit user approval is required for this Paper caller"
            )
        if policy.approval_mode == PAPER_PARK_APPROVAL_MODE:
            # 사람 승인 없이 **매수까지** 하는 파킹은 종목·슬롯을 여기서 묶는다(슬롯은
            # execute의 _require_park_slot). 호출하는 쪽만 믿으면 아무 종목이나 살 수 있다.
            if str(ticker or "").strip() != PARK_TICKER:
                raise ValueError("Idle-cash policy permits only the parking ETF")
        return caller_n, action_n

    def _require_park_slot(self, slot_id: object) -> None:
        """유휴 자본 파킹은 IPO 슬롯에서만 — 슬롯 번호는 이름을 몰라 API에서 찾는다."""
        names = {int(s["id"]): s.get("name") for s in self.client.list_paper_slots()}
        try:
            name = names.get(int(slot_id))
        except (TypeError, ValueError):
            name = None
        if name != PARK_SLOT:
            raise ValueError("Idle-cash policy permits only the parking slot")

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
            ticker=ticker,
        )
        if paper_caller_policy(caller_n).approval_mode == PAPER_PARK_APPROVAL_MODE:
            self._require_park_slot(slot_id)
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
