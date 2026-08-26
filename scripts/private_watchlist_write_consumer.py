#!/usr/bin/env python3
"""Default-disabled watchlist consumer executor for Private API writes."""
from __future__ import annotations

from dataclasses import dataclass

from private_data_api_client import PrivateDataClient
from private_write_readiness import require_private_write_activation_permit
from telegram_write_identity import (
    TelegramWriteIdentity,
    validate_watchlist_write_identity,
)


class WatchlistConsumerError(RuntimeError):
    pass


class WatchlistConsumerDisabled(WatchlistConsumerError):
    pass


class WatchlistApprovalRequired(WatchlistConsumerError):
    pass


class WatchlistTerminalState(WatchlistConsumerError):
    def __init__(self, state: str) -> None:
        super().__init__("watchlist write intent is already terminal")
        self.state = state


@dataclass(frozen=True)
class WatchlistExecutionResult:
    operation: str
    state: str
    result: dict[str, object]
    replayed: bool
    request_id: str
    approval_id: str


class WatchlistWriteExecutor:
    """Compose identity, preflight, submit, approval, and one-time apply."""

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
            raise WatchlistConsumerDisabled("watchlist write executor is disabled")
        if not bool(getattr(self.client, "writes_enabled", False)):
            raise WatchlistConsumerDisabled("Private write client is disabled")

    def require_ready(self, *, user_approved: bool) -> None:
        self._require_enabled()
        if user_approved is not True:
            raise WatchlistApprovalRequired("explicit user approval is required")

    @staticmethod
    def _payload(action: str, ticker: object, name: object) -> dict[str, object]:
        ticker_n = str(ticker or "").strip()
        if len(ticker_n) != 6 or not ticker_n.isdigit():
            raise ValueError("ticker must be six digits")
        if action == "remove":
            return {"ticker": ticker_n}
        name_n = " ".join(str(name or "").split())
        if not name_n or len(name_n) > 100:
            raise ValueError("name must be 1-100 characters")
        return {"ticker": ticker_n, "name": name_n}

    @staticmethod
    def _completed(
        record: dict[str, object],
        *,
        replayed: bool,
    ) -> WatchlistExecutionResult:
        result = record.get("result")
        if record.get("state") != "applied" or not isinstance(result, dict):
            raise WatchlistConsumerError("watchlist write did not produce an applied result")
        return WatchlistExecutionResult(
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
        ticker: str,
        name: str | None,
        identity: TelegramWriteIdentity,
        user_approved: bool = False,
    ) -> WatchlistExecutionResult:
        self.require_ready(user_approved=user_approved)
        action_n = str(action or "").strip().lower()
        identity = validate_watchlist_write_identity(identity, action=action_n)
        payload = self._payload(action_n, ticker, name)
        identity_args = {
            "operation": identity.operation,
            "request_id": identity.request_id,
            "approval_id": identity.approval_id,
            "idempotency_key": identity.idempotency_key,
        }
        preflight = self.client.preflight_watchlist_write(**identity_args)
        existed = bool(preflight["exists"])
        state = preflight["state"]
        if state == "applied":
            return self._completed(preflight, replayed=True)
        if state in {"rejected", "expired"}:
            raise WatchlistTerminalState(str(state))

        submitted = self.client.submit_watchlist_write(
            **identity_args,
            expected_version=int(preflight["expected_version"]),
            payload=payload,
        )
        state = submitted["state"]
        if state == "applied":
            return self._completed(submitted, replayed=True)
        if state in {"rejected", "expired"}:
            raise WatchlistTerminalState(str(state))

        if state == "pending":
            approved = self.client.approve_watchlist_write(
                request_id=identity.request_id,
                approval_id=identity.approval_id,
            )
            state = approved["state"]
        if state != "approved":
            raise WatchlistConsumerError("watchlist write approval state is invalid")

        applied = self.client.apply_watchlist_write(
            request_id=identity.request_id,
            approval_id=identity.approval_id,
        )
        return self._completed(
            applied,
            replayed=existed or bool(submitted["replayed"]),
        )
