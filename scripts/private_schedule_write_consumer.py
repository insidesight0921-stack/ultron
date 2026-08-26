#!/usr/bin/env python3
"""Default-disabled, chat-scoped schedule consumer for Private API writes."""
from __future__ import annotations

from dataclasses import dataclass

from private_data_api_client import PrivateDataClient
from private_schedule_write_contract import validate_schedule_write_intent
from private_write_readiness import require_private_write_activation_permit
from telegram_write_identity import (
    TelegramWriteIdentity,
    validate_schedule_write_identity,
)


class ScheduleConsumerError(RuntimeError):
    pass


class ScheduleConsumerDisabled(ScheduleConsumerError):
    pass


class ScheduleApprovalRequired(ScheduleConsumerError):
    pass


class ScheduleTerminalState(ScheduleConsumerError):
    def __init__(self, state: str) -> None:
        super().__init__("schedule write intent is already terminal")
        self.state = state


@dataclass(frozen=True)
class ScheduleExecutionResult:
    operation: str
    state: str
    result: dict[str, object]
    replayed: bool
    request_id: str
    approval_id: str


class ScheduleWriteExecutor:
    """Compose chat scope, identity, preflight, approval, and one-time apply."""

    def __init__(
        self,
        client: PrivateDataClient,
        *,
        enabled: bool = False,
        write_activation_permit=None,
    ) -> None:
        self.client = client
        self.enabled = bool(enabled)
        if self.enabled:
            client_permit = require_private_write_activation_permit(
                getattr(client, "write_activation_permit", None)
            )
            self.write_activation_permit = require_private_write_activation_permit(
                write_activation_permit,
                matching_permit=client_permit,
            )
        elif write_activation_permit is not None:
            raise ValueError("write activation permit requires enabled executor")
        else:
            self.write_activation_permit = None

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ScheduleConsumerDisabled("schedule write executor is disabled")
        if not bool(getattr(self.client, "writes_enabled", False)):
            raise ScheduleConsumerDisabled("Private write client is disabled")

    def require_ready(self, *, user_approved: bool) -> None:
        self._require_enabled()
        if user_approved is not True:
            raise ScheduleApprovalRequired("explicit user approval is required")

    @staticmethod
    def _raw_payload(
        action: str,
        *,
        title: object,
        when_at: object,
        event_id: object,
        notes: object,
        rrule_freq: object,
        rrule_byday: object,
        rrule_until: object,
        pre_notify_minutes: object,
    ) -> dict[str, object]:
        if action != "add":
            return {"event_id": event_id}
        payload: dict[str, object] = {"title": title, "when_at": when_at}
        for key, value in (
            ("notes", notes),
            ("rrule_freq", rrule_freq),
            ("rrule_byday", rrule_byday),
            ("rrule_until", rrule_until),
            ("pre_notify_minutes", pre_notify_minutes),
        ):
            if value is not None:
                payload[key] = value
        return payload

    @staticmethod
    def _completed(
        record: dict[str, object],
        *,
        replayed: bool,
    ) -> ScheduleExecutionResult:
        result = record.get("result")
        if record.get("state") != "applied" or not isinstance(result, dict):
            raise ScheduleConsumerError("schedule write did not produce an applied result")
        return ScheduleExecutionResult(
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
        action: str,
        chat_id: str | int,
        identity: TelegramWriteIdentity,
        user_approved: bool = False,
        title: str | None = None,
        when_at: str | None = None,
        event_id: int | None = None,
        notes: str | None = None,
        rrule_freq: str | None = None,
        rrule_byday: str | None = None,
        rrule_until: str | None = None,
        pre_notify_minutes: int | list[int] | None = None,
    ) -> ScheduleExecutionResult:
        self.require_ready(user_approved=user_approved)
        action_n = str(action or "").strip().lower()
        identity = validate_schedule_write_identity(identity, action=action_n)
        bound = validate_schedule_write_intent(
            operation=identity.operation,
            chat_id=chat_id,
            request_id=identity.request_id,
            approval_id=identity.approval_id,
            idempotency_key=identity.idempotency_key,
            expected_version=0,
            payload=self._raw_payload(
                action_n,
                title=title,
                when_at=when_at,
                event_id=event_id,
                notes=notes,
                rrule_freq=rrule_freq,
                rrule_byday=rrule_byday,
                rrule_until=rrule_until,
                pre_notify_minutes=pre_notify_minutes,
            ),
        )
        identity_args = {
            "operation": identity.operation,
            "chat_id": bound.chat_id,
            "request_id": identity.request_id,
            "approval_id": identity.approval_id,
            "idempotency_key": identity.idempotency_key,
        }
        preflight = self.client.preflight_schedule_write(**identity_args)
        existed = bool(preflight["exists"])
        state = preflight["state"]
        if state == "applied":
            return self._completed(preflight, replayed=True)
        if state in {"rejected", "expired"}:
            raise ScheduleTerminalState(str(state))

        submitted = self.client.submit_schedule_write(
            **identity_args,
            expected_version=int(preflight["expected_version"]),
            payload=bound.intent.payload,
        )
        state = submitted["state"]
        if state == "applied":
            return self._completed(submitted, replayed=True)
        if state in {"rejected", "expired"}:
            raise ScheduleTerminalState(str(state))

        if state == "pending":
            approved = self.client.approve_schedule_write(
                chat_id=bound.chat_id,
                request_id=identity.request_id,
                approval_id=identity.approval_id,
            )
            state = approved["state"]
        if state != "approved":
            raise ScheduleConsumerError("schedule write approval state is invalid")

        applied = self.client.apply_schedule_write(
            chat_id=bound.chat_id,
            request_id=identity.request_id,
            approval_id=identity.approval_id,
        )
        return self._completed(
            applied,
            replayed=existed or bool(submitted["replayed"]),
        )
