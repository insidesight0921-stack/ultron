"""Authenticated, loopback-only client for the Private Data API."""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from private_paper_write_contract import (
    PAPER_TRADE_OPERATIONS,
    normalize_paper_slot_id,
    validate_paper_trade_write_intent,
)
from private_schedule_write_contract import (
    SCHEDULE_WRITE_OPERATIONS,
    normalize_schedule_chat_id,
    validate_schedule_write_intent,
)
from private_write_contract import normalize_idempotency_key, validate_write_intent
from private_write_readiness import require_private_write_activation_permit


DEFAULT_BASE_URL = "http://127.0.0.1:8091"
TOKEN_ENV = "AI_AGENT_PRIVATE_API_TOKEN"
MAX_RESPONSE_BYTES = 1_000_000
MAX_WATCHLIST_ITEMS = 500
MAX_SCHEDULE_EVENTS = 100
MAX_PAPER_PORTFOLIOS = 10
MAX_PAPER_POSITIONS = 1_000
MAX_PAPER_SLOTS = 20
MAX_PAPER_TRADES = 1_000
MAX_PAPER_IPO_RECORDS = 500
MAX_PAPER_IPO_STATS = 20
MAX_PAPER_MYQUANT_TAGS = 200
MAX_WRITE_REQUEST_BYTES = 36_864
_WATCHLIST_WRITE_OPERATIONS = frozenset({"watchlist.add", "watchlist.remove"})
_WRITE_STATES = frozenset({"pending", "approved", "applied", "rejected", "expired"})


class PrivateAPIError(RuntimeError):
    pass


class PrivateAPIUnavailable(PrivateAPIError):
    pass


class PrivateAPIAuthError(PrivateAPIError):
    pass


class PrivateAPIWriteDisabled(PrivateAPIError):
    pass


class PrivateAPIWriteConflict(PrivateAPIError):
    def __init__(self, code: str) -> None:
        super().__init__("Private API 쓰기 요청이 현재 상태와 충돌했습니다.")
        self.code = code


class PrivateAPIWriteRejected(PrivateAPIError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__("Private API 쓰기 요청이 거부되었습니다.")
        self.code = code
        self.status_code = status_code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirect()).open


def private_api_enabled() -> bool:
    return os.getenv("AI_AGENT_PRIVATE_API_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def validate_private_base_url(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.username or parsed.password:
        raise ValueError("Private API URL은 인증정보 없는 loopback HTTP여야 합니다.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Private API URL에는 경로·쿼리·fragment를 넣을 수 없습니다.")
    if parsed.port != 8091:
        raise ValueError("Private API client는 전용 8091 포트만 사용할 수 있습니다.")
    host = parsed.hostname
    if host == "localhost":
        return value
    try:
        if host and ipaddress.ip_address(host).is_loopback:
            return value
    except ValueError:
        pass
    raise ValueError("Private API URL은 loopback 주소만 허용합니다.")


def validate_client_token(token: str) -> str:
    value = str(token or "")
    if len(value) < 32 or any(character.isspace() for character in value):
        raise ValueError("Private API token 설정이 올바르지 않습니다.")
    return value


class PrivateDataClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        token: str | None = None,
        timeout: float = 1.0,
        opener: Any = _NO_REDIRECT_OPENER,
        writes_enabled: bool = False,
        write_activation_permit=None,
        paper_writes_enabled: bool = False,
        paper_write_activation_permit=None,
        paper_write_database_path: Path | str | None = None,
    ) -> None:
        self.base_url = validate_private_base_url(base_url)
        self.token = validate_client_token(token or os.getenv(TOKEN_ENV, ""))
        self.timeout = max(0.1, float(timeout))
        self._opener = opener
        self.writes_enabled = bool(writes_enabled)
        if self.writes_enabled:
            self.write_activation_permit = require_private_write_activation_permit(
                write_activation_permit
            )
        elif write_activation_permit is not None:
            raise ValueError("write activation permit requires writes_enabled=True")
        else:
            self.write_activation_permit = None
        self.paper_writes_enabled = bool(paper_writes_enabled)
        if self.paper_writes_enabled:
            if paper_write_database_path is None:
                raise ValueError("Paper write database path is required")
            paper_database = Path(paper_write_database_path).resolve()
            if paper_database.name != "paper.db":
                raise ValueError("Paper write database must be paper.db")
            self.paper_write_activation_permit = require_private_write_activation_permit(
                paper_write_activation_permit,
                database_path=paper_database,
            )
            if (
                self.write_activation_permit is not None
                and self.write_activation_permit.fingerprint
                == self.paper_write_activation_permit.fingerprint
            ):
                raise ValueError("Paper write permit must be separate from common writes")
            self.paper_write_database_path = paper_database
        elif paper_write_activation_permit is not None or paper_write_database_path is not None:
            raise ValueError(
                "Paper write permit and database path require paper_writes_enabled=True"
            )
        else:
            self.paper_write_activation_permit = None
            self.paper_write_database_path = None

    def _get_json(self, path: str, *, headers: dict[str, str] | None = None) -> Any:
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if headers:
            request_headers.update(headers)
        request = Request(
            f"{self.base_url}{path}",
            headers=request_headers,
        )
        return self._open_json(request, write_request=False)

    def _open_json(self, request: Request, *, write_request: bool) -> Any:
        try:
            with self._opener(request, timeout=self.timeout) as response:
                if response.headers.get("Cache-Control", "").lower() != "no-store":
                    raise PrivateAPIError("Private API cache 경계가 올바르지 않습니다.")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code == 401:
                raise PrivateAPIAuthError("Private API 인증에 실패했습니다.") from exc
            if write_request:
                if (
                    exc.headers is None
                    or exc.headers.get("Cache-Control", "").lower() != "no-store"
                ):
                    raise PrivateAPIError("Private API cache 경계가 올바르지 않습니다.") from exc
                code = _write_error_code(exc)
                if exc.code == 409:
                    raise PrivateAPIWriteConflict(code) from exc
                if exc.code in {400, 404, 413, 415, 422}:
                    raise PrivateAPIWriteRejected(code, exc.code) from exc
            raise PrivateAPIUnavailable("Private API를 사용할 수 없습니다.") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise PrivateAPIUnavailable("Private API를 사용할 수 없습니다.") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PrivateAPIError("Private API 응답이 너무 큽니다.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise PrivateAPIError("Private API 응답 형식이 올바르지 않습니다.") from exc

    def _post_json(
        self,
        path: str,
        *,
        body: dict[str, object] | None,
        headers: dict[str, str],
    ) -> Any:
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            **headers,
        }
        if body is None:
            encoded = b""
        else:
            encoded = json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if len(encoded) > MAX_WRITE_REQUEST_BYTES:
            raise PrivateAPIWriteRejected("payload_too_large", 413)
        request = Request(
            f"{self.base_url}{path}",
            data=encoded,
            headers=request_headers,
            method="POST",
        )
        return self._open_json(request, write_request=True)

    def _require_writes_enabled(self) -> None:
        if not self.writes_enabled:
            raise PrivateAPIWriteDisabled("Private API 쓰기 클라이언트가 비활성 상태입니다.")

    def _require_paper_writes_enabled(self) -> None:
        if not self.paper_writes_enabled:
            raise PrivateAPIWriteDisabled("Private Paper 쓰기 클라이언트가 비활성 상태입니다.")

    def submit_watchlist_write(
        self,
        *,
        operation: str,
        request_id: str,
        approval_id: str,
        idempotency_key: str,
        expected_version: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self._require_writes_enabled()
        intent = validate_write_intent(
            operation=operation,
            request_id=request_id,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            payload=payload,
        )
        if intent.operation not in _WATCHLIST_WRITE_OPERATIONS:
            raise ValueError("watchlist write operation만 허용합니다.")
        response = self._post_json(
            "/v1/private/watchlist/write/intents",
            body={
                "operation": intent.operation,
                "expected_version": intent.expected_version,
                "payload": intent.payload,
            },
            headers={
                "Idempotency-Key": intent.idempotency_key,
                "X-Request-ID": intent.request_id,
                "X-Approval-ID": intent.approval_id,
            },
        )
        result = _validate_write_response(response)
        _require_write_identity(result, intent.request_id, intent.approval_id)
        if result["operation"] != intent.operation:
            raise PrivateAPIError("Private write operation 응답이 일치하지 않습니다.")
        if result["expected_version"] != intent.expected_version:
            raise PrivateAPIError("Private write version 응답이 일치하지 않습니다.")
        return result

    def preflight_watchlist_write(
        self,
        *,
        operation: str,
        request_id: str,
        approval_id: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self._require_writes_enabled()
        operation_n = str(operation or "").strip()
        if operation_n not in _WATCHLIST_WRITE_OPERATIONS:
            raise ValueError("watchlist write operation만 허용합니다.")
        request_id_n = _normalize_uuid(request_id, field="request_id")
        approval_id_n = _normalize_uuid(approval_id, field="approval_id")
        idempotency_key_n = normalize_idempotency_key(idempotency_key)
        response = self._post_json(
            "/v1/private/watchlist/write/preflight",
            body=None,
            headers={
                "X-Write-Operation": operation_n,
                "Idempotency-Key": idempotency_key_n,
                "X-Request-ID": request_id_n,
                "X-Approval-ID": approval_id_n,
            },
        )
        result = _validate_preflight_response(response)
        _require_write_identity(result, request_id_n, approval_id_n)
        if result["operation"] != operation_n:
            raise PrivateAPIError("Private preflight operation 응답이 일치하지 않습니다.")
        return result

    def _transition_watchlist_write(
        self,
        action: str,
        *,
        request_id: str,
        approval_id: str,
    ) -> dict[str, object]:
        self._require_writes_enabled()
        request_id_n = _normalize_uuid(request_id, field="request_id")
        approval_id_n = _normalize_uuid(approval_id, field="approval_id")
        response = self._post_json(
            f"/v1/private/watchlist/write/approvals/{action}",
            body=None,
            headers={
                "X-Request-ID": request_id_n,
                "X-Approval-ID": approval_id_n,
            },
        )
        result = _validate_write_response(response)
        _require_write_identity(result, request_id_n, approval_id_n)
        return result

    def approve_watchlist_write(self, *, request_id: str, approval_id: str) -> dict[str, object]:
        result = self._transition_watchlist_write(
            "approve",
            request_id=request_id,
            approval_id=approval_id,
        )
        if result["state"] not in {"approved", "applied"}:
            raise PrivateAPIError("Private write 승인 응답 상태가 올바르지 않습니다.")
        return result

    def reject_watchlist_write(self, *, request_id: str, approval_id: str) -> dict[str, object]:
        result = self._transition_watchlist_write(
            "reject",
            request_id=request_id,
            approval_id=approval_id,
        )
        if result["state"] != "rejected":
            raise PrivateAPIError("Private write 거부 응답 상태가 올바르지 않습니다.")
        return result

    def apply_watchlist_write(self, *, request_id: str, approval_id: str) -> dict[str, object]:
        result = self._transition_watchlist_write(
            "apply",
            request_id=request_id,
            approval_id=approval_id,
        )
        if result["state"] != "applied":
            raise PrivateAPIError("Private write 적용 응답 상태가 올바르지 않습니다.")
        return result

    def submit_schedule_write(
        self,
        *,
        operation: str,
        chat_id: str | int,
        request_id: str,
        approval_id: str,
        idempotency_key: str,
        expected_version: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self._require_writes_enabled()
        bound_intent = validate_schedule_write_intent(
            operation=operation,
            chat_id=chat_id,
            request_id=request_id,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            payload=payload,
        )
        intent = bound_intent.intent
        response = self._post_json(
            "/v1/private/schedule/write/intents",
            body={
                "operation": intent.operation,
                "expected_version": intent.expected_version,
                "payload": intent.payload,
            },
            headers={
                "Idempotency-Key": intent.idempotency_key,
                "X-Request-ID": intent.request_id,
                "X-Approval-ID": intent.approval_id,
                "X-AI-Agent-Chat-ID": bound_intent.chat_id,
            },
        )
        result = _validate_write_response(
            response,
            allowed_operations=SCHEDULE_WRITE_OPERATIONS,
        )
        _require_write_identity(result, intent.request_id, intent.approval_id)
        if result["operation"] != intent.operation:
            raise PrivateAPIError("Private write operation 응답이 일치하지 않습니다.")
        if result["expected_version"] != intent.expected_version:
            raise PrivateAPIError("Private write version 응답이 일치하지 않습니다.")
        return result

    def preflight_schedule_write(
        self,
        *,
        operation: str,
        chat_id: str | int,
        request_id: str,
        approval_id: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self._require_writes_enabled()
        operation_n = str(operation or "").strip()
        if operation_n not in SCHEDULE_WRITE_OPERATIONS:
            raise ValueError("schedule write operation만 허용합니다.")
        chat_id_n = normalize_schedule_chat_id(chat_id)
        request_id_n = _normalize_uuid(request_id, field="request_id")
        approval_id_n = _normalize_uuid(approval_id, field="approval_id")
        idempotency_key_n = normalize_idempotency_key(idempotency_key)
        response = self._post_json(
            "/v1/private/schedule/write/preflight",
            body=None,
            headers={
                "X-Write-Operation": operation_n,
                "Idempotency-Key": idempotency_key_n,
                "X-Request-ID": request_id_n,
                "X-Approval-ID": approval_id_n,
                "X-AI-Agent-Chat-ID": chat_id_n,
            },
        )
        result = _validate_preflight_response(
            response,
            allowed_operations=SCHEDULE_WRITE_OPERATIONS,
        )
        _require_write_identity(result, request_id_n, approval_id_n)
        if result["operation"] != operation_n:
            raise PrivateAPIError("Private preflight operation 응답이 일치하지 않습니다.")
        return result

    def _transition_schedule_write(
        self,
        action: str,
        *,
        chat_id: str | int,
        request_id: str,
        approval_id: str,
    ) -> dict[str, object]:
        self._require_writes_enabled()
        chat_id_n = normalize_schedule_chat_id(chat_id)
        request_id_n = _normalize_uuid(request_id, field="request_id")
        approval_id_n = _normalize_uuid(approval_id, field="approval_id")
        response = self._post_json(
            f"/v1/private/schedule/write/approvals/{action}",
            body=None,
            headers={
                "X-Request-ID": request_id_n,
                "X-Approval-ID": approval_id_n,
                "X-AI-Agent-Chat-ID": chat_id_n,
            },
        )
        result = _validate_write_response(
            response,
            allowed_operations=SCHEDULE_WRITE_OPERATIONS,
        )
        _require_write_identity(result, request_id_n, approval_id_n)
        return result

    def approve_schedule_write(
        self, *, chat_id: str | int, request_id: str, approval_id: str
    ) -> dict[str, object]:
        result = self._transition_schedule_write(
            "approve",
            chat_id=chat_id,
            request_id=request_id,
            approval_id=approval_id,
        )
        if result["state"] not in {"approved", "applied"}:
            raise PrivateAPIError("Private write 승인 응답 상태가 올바르지 않습니다.")
        return result

    def reject_schedule_write(
        self, *, chat_id: str | int, request_id: str, approval_id: str
    ) -> dict[str, object]:
        result = self._transition_schedule_write(
            "reject",
            chat_id=chat_id,
            request_id=request_id,
            approval_id=approval_id,
        )
        if result["state"] != "rejected":
            raise PrivateAPIError("Private write 거부 응답 상태가 올바르지 않습니다.")
        return result

    def apply_schedule_write(
        self, *, chat_id: str | int, request_id: str, approval_id: str
    ) -> dict[str, object]:
        result = self._transition_schedule_write(
            "apply",
            chat_id=chat_id,
            request_id=request_id,
            approval_id=approval_id,
        )
        if result["state"] != "applied":
            raise PrivateAPIError("Private write 적용 응답 상태가 올바르지 않습니다.")
        return result

    def submit_paper_trade_write(
        self,
        *,
        operation: str,
        slot_id: int,
        request_id: str,
        approval_id: str,
        idempotency_key: str,
        expected_version: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self._require_paper_writes_enabled()
        slot_id_n = normalize_paper_slot_id(slot_id)
        bound_intent = validate_paper_trade_write_intent(
            operation=operation,
            request_id=request_id,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            payload=payload,
        )
        if bound_intent.slot_id != slot_id_n:
            raise ValueError("Paper payload slot과 요청 scope가 일치하지 않습니다.")
        intent = bound_intent.intent
        response = self._post_json(
            "/v1/private/paper/write/intents",
            body={
                "operation": intent.operation,
                "expected_version": intent.expected_version,
                "payload": intent.payload,
            },
            headers={
                "Idempotency-Key": intent.idempotency_key,
                "X-Request-ID": intent.request_id,
                "X-Approval-ID": intent.approval_id,
                "X-AI-Agent-Paper-Slot-ID": str(slot_id_n),
            },
        )
        result = _validate_write_response(
            response,
            allowed_operations=PAPER_TRADE_OPERATIONS,
        )
        _require_write_identity(result, intent.request_id, intent.approval_id)
        if result["operation"] != intent.operation:
            raise PrivateAPIError("Private Paper operation 응답이 일치하지 않습니다.")
        if result["expected_version"] != intent.expected_version:
            raise PrivateAPIError("Private Paper version 응답이 일치하지 않습니다.")
        _require_paper_result_scope(result, slot_id_n)
        return result

    def preflight_paper_trade_write(
        self,
        *,
        operation: str,
        slot_id: int,
        request_id: str,
        approval_id: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self._require_paper_writes_enabled()
        operation_n = str(operation or "").strip()
        if operation_n not in PAPER_TRADE_OPERATIONS:
            raise ValueError("Paper trade write operation만 허용합니다.")
        slot_id_n = normalize_paper_slot_id(slot_id)
        request_id_n = _normalize_uuid(request_id, field="request_id")
        approval_id_n = _normalize_uuid(approval_id, field="approval_id")
        idempotency_key_n = normalize_idempotency_key(idempotency_key)
        response = self._post_json(
            "/v1/private/paper/write/preflight",
            body=None,
            headers={
                "X-Write-Operation": operation_n,
                "Idempotency-Key": idempotency_key_n,
                "X-Request-ID": request_id_n,
                "X-Approval-ID": approval_id_n,
                "X-AI-Agent-Paper-Slot-ID": str(slot_id_n),
            },
        )
        result = _validate_preflight_response(
            response,
            allowed_operations=PAPER_TRADE_OPERATIONS,
        )
        _require_write_identity(result, request_id_n, approval_id_n)
        if result["operation"] != operation_n:
            raise PrivateAPIError("Private Paper preflight operation 응답이 일치하지 않습니다.")
        _require_paper_result_scope(result, slot_id_n)
        return result

    def _transition_paper_trade_write(
        self,
        action: str,
        *,
        slot_id: int,
        request_id: str,
        approval_id: str,
    ) -> dict[str, object]:
        self._require_paper_writes_enabled()
        slot_id_n = normalize_paper_slot_id(slot_id)
        request_id_n = _normalize_uuid(request_id, field="request_id")
        approval_id_n = _normalize_uuid(approval_id, field="approval_id")
        response = self._post_json(
            f"/v1/private/paper/write/approvals/{action}",
            body=None,
            headers={
                "X-Request-ID": request_id_n,
                "X-Approval-ID": approval_id_n,
                "X-AI-Agent-Paper-Slot-ID": str(slot_id_n),
            },
        )
        result = _validate_write_response(
            response,
            allowed_operations=PAPER_TRADE_OPERATIONS,
        )
        _require_write_identity(result, request_id_n, approval_id_n)
        _require_paper_result_scope(result, slot_id_n)
        return result

    def approve_paper_trade_write(
        self, *, slot_id: int, request_id: str, approval_id: str
    ) -> dict[str, object]:
        result = self._transition_paper_trade_write(
            "approve",
            slot_id=slot_id,
            request_id=request_id,
            approval_id=approval_id,
        )
        if result["state"] not in {"approved", "applied"}:
            raise PrivateAPIError("Private Paper 승인 응답 상태가 올바르지 않습니다.")
        return result

    def reject_paper_trade_write(
        self, *, slot_id: int, request_id: str, approval_id: str
    ) -> dict[str, object]:
        result = self._transition_paper_trade_write(
            "reject",
            slot_id=slot_id,
            request_id=request_id,
            approval_id=approval_id,
        )
        if result["state"] != "rejected":
            raise PrivateAPIError("Private Paper 거부 응답 상태가 올바르지 않습니다.")
        return result

    def apply_paper_trade_write(
        self, *, slot_id: int, request_id: str, approval_id: str
    ) -> dict[str, object]:
        result = self._transition_paper_trade_write(
            "apply",
            slot_id=slot_id,
            request_id=request_id,
            approval_id=approval_id,
        )
        if result["state"] != "applied":
            raise PrivateAPIError("Private Paper 적용 응답 상태가 올바르지 않습니다.")
        return result

    def list_watchlist(self) -> list[dict[str, str]]:
        payload = self._get_json("/v1/private/watchlist")
        if not isinstance(payload, dict) or set(payload) != {"items", "count"}:
            raise PrivateAPIError("watchlist 응답 형식이 올바르지 않습니다.")
        items = payload["items"]
        if (
            not isinstance(items, list)
            or not isinstance(payload["count"], int)
            or payload["count"] != len(items)
            or len(items) > MAX_WATCHLIST_ITEMS
        ):
            raise PrivateAPIError("watchlist 응답 개수가 올바르지 않습니다.")
        validated: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"ticker", "name", "created_at"}:
                raise PrivateAPIError("watchlist 항목 형식이 올바르지 않습니다.")
            ticker = item["ticker"]
            name = item["name"]
            created_at = item["created_at"]
            if not isinstance(ticker, str) or len(ticker) != 6 or not ticker.isdigit():
                raise PrivateAPIError("watchlist ticker 형식이 올바르지 않습니다.")
            if not isinstance(name, str) or not name.strip() or len(name) > 100:
                raise PrivateAPIError("watchlist name 형식이 올바르지 않습니다.")
            if not isinstance(created_at, str) or not created_at or len(created_at) > 64:
                raise PrivateAPIError("watchlist created_at 형식이 올바르지 않습니다.")
            validated.append({"ticker": ticker, "name": name, "created_at": created_at})
        return validated

    def list_schedule(
        self,
        chat_id: str | int,
        *,
        upcoming_only: bool,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        chat_id_n = _normalize_chat_id(chat_id)
        if not 1 <= int(limit) <= MAX_SCHEDULE_EVENTS:
            raise ValueError("schedule limit은 1~100 범위여야 합니다.")
        path = (
            "/v1/private/schedule/events/upcoming"
            if upcoming_only
            else "/v1/private/schedule/events"
        )
        payload = self._get_json(
            path,
            headers={"X-AI-Agent-Chat-ID": chat_id_n},
        )
        if not isinstance(payload, dict) or set(payload) != {"events", "count", "scope"}:
            raise PrivateAPIError("schedule 응답 형식이 올바르지 않습니다.")
        expected_scope = "upcoming" if upcoming_only else "all"
        events = payload["events"]
        if (
            payload["scope"] != expected_scope
            or not isinstance(events, list)
            or not isinstance(payload["count"], int)
            or payload["count"] != len(events)
            or len(events) > MAX_SCHEDULE_EVENTS
        ):
            raise PrivateAPIError("schedule 응답 개수가 올바르지 않습니다.")
        return [_validate_schedule_event(event) for event in events[: int(limit)]]

    def list_paper_portfolios(self) -> list[dict[str, object]]:
        payload = self._get_json("/v1/private/paper/portfolios")
        if not isinstance(payload, dict) or set(payload) != {"portfolios", "count"}:
            raise PrivateAPIError("paper portfolio 응답 형식이 올바르지 않습니다.")
        portfolios = payload["portfolios"]
        if (
            not isinstance(portfolios, list)
            or not isinstance(payload["count"], int)
            or isinstance(payload["count"], bool)
            or payload["count"] != len(portfolios)
            or len(portfolios) > MAX_PAPER_PORTFOLIOS
        ):
            raise PrivateAPIError("paper portfolio 응답 개수가 올바르지 않습니다.")
        return [_validate_paper_portfolio(item) for item in portfolios]

    def list_paper_positions(self, slot=None) -> list[dict[str, object]]:
        payload = self._get_json("/v1/private/paper/positions")
        if not isinstance(payload, dict) or set(payload) != {"positions", "count"}:
            raise PrivateAPIError("paper position 응답 형식이 올바르지 않습니다.")
        positions = payload["positions"]
        if (
            not isinstance(positions, list)
            or not isinstance(payload["count"], int)
            or isinstance(payload["count"], bool)
            or payload["count"] != len(positions)
            or len(positions) > MAX_PAPER_POSITIONS
        ):
            raise PrivateAPIError("paper position 응답 개수가 올바르지 않습니다.")
        validated = [_validate_paper_position(item) for item in positions]
        if slot is None:
            return validated
        if isinstance(slot, bool):
            return []
        if isinstance(slot, int):
            return [item for item in validated if item["slot_id"] == slot]
        if isinstance(slot, str):
            name = slot.strip()
            return [item for item in validated if item["slot_name"] == name]
        return []

    def list_paper_slots(self) -> list[dict[str, object]]:
        payload = self._get_json("/v1/private/paper/slots")
        if not isinstance(payload, dict) or set(payload) != {"slots", "count"}:
            raise PrivateAPIError("paper slot 응답 형식이 올바르지 않습니다.")
        slots = payload["slots"]
        if (
            not isinstance(slots, list)
            or not _valid_int(payload["count"], minimum=0)
            or payload["count"] != len(slots)
            or len(slots) > MAX_PAPER_SLOTS
        ):
            raise PrivateAPIError("paper slot 응답 개수가 올바르지 않습니다.")
        return [_validate_paper_slot(item) for item in slots]

    def list_paper_trades(self, slot=None, *, limit: int = 100) -> list[dict[str, object]]:
        if not _valid_int(limit) or limit > MAX_PAPER_TRADES:
            raise ValueError("paper trade limit은 1~1000 범위여야 합니다.")
        payload = self._get_json("/v1/private/paper/trades")
        if not isinstance(payload, dict) or set(payload) != {"trades", "count"}:
            raise PrivateAPIError("paper trade 응답 형식이 올바르지 않습니다.")
        trades = payload["trades"]
        if (
            not isinstance(trades, list)
            or not _valid_int(payload["count"], minimum=0)
            or payload["count"] != len(trades)
            or len(trades) > MAX_PAPER_TRADES
        ):
            raise PrivateAPIError("paper trade 응답 개수가 올바르지 않습니다.")
        validated = [_validate_paper_trade(item) for item in trades]
        if slot is not None:
            validated = _filter_paper_slot(validated, slot)
        return validated[:limit]

    def list_paper_ipo_records(self) -> list[dict[str, object]]:
        payload = self._get_json("/v1/private/paper/ipo/records")
        if not isinstance(payload, dict) or set(payload) != {"records", "count"}:
            raise PrivateAPIError("paper IPO records 응답 형식이 올바르지 않습니다.")
        records = payload["records"]
        if (
            not isinstance(records, list)
            or not _valid_int(payload["count"], minimum=0)
            or payload["count"] != len(records)
            or len(records) > MAX_PAPER_IPO_RECORDS
        ):
            raise PrivateAPIError("paper IPO records 응답 개수가 올바르지 않습니다.")
        return [_validate_paper_ipo_record(item) for item in records]

    def list_paper_ipo_stats(self) -> list[dict[str, object]]:
        payload = self._get_json("/v1/private/paper/ipo/stats")
        if not isinstance(payload, dict) or set(payload) != {"stats", "count"}:
            raise PrivateAPIError("paper IPO stats 응답 형식이 올바르지 않습니다.")
        stats = payload["stats"]
        if (
            not isinstance(stats, list)
            or not _valid_int(payload["count"], minimum=0)
            or payload["count"] != len(stats)
            or len(stats) > MAX_PAPER_IPO_STATS
        ):
            raise PrivateAPIError("paper IPO stats 응답 개수가 올바르지 않습니다.")
        return [_validate_paper_ipo_stat(item) for item in stats]

    def list_paper_performance(self) -> list[dict[str, object]]:
        payload = self._get_json("/v1/private/paper/performance")
        if not isinstance(payload, dict) or set(payload) != {"performance", "count"}:
            raise PrivateAPIError("paper performance 응답 형식이 올바르지 않습니다.")
        performance = payload["performance"]
        if (
            not isinstance(performance, list)
            or not _valid_int(payload["count"], minimum=0)
            or payload["count"] != len(performance)
            or len(performance) > MAX_PAPER_SLOTS
        ):
            raise PrivateAPIError("paper performance 응답 개수가 올바르지 않습니다.")
        return [_validate_paper_performance(item) for item in performance]

    def get_paper_myquant_tags(self) -> dict[str, object]:
        payload = self._get_json("/v1/private/paper/myquant-tags")
        if not isinstance(payload, dict) or set(payload) != {"tags", "text"}:
            raise PrivateAPIError("paper myquant-tags 응답 형식이 올바르지 않습니다.")
        tags = payload["tags"]
        text = payload["text"]
        if not isinstance(tags, dict) or len(tags) > MAX_PAPER_MYQUANT_TAGS:
            raise PrivateAPIError("paper myquant-tags 개수가 올바르지 않습니다.")
        if not isinstance(text, str) or len(text) > 20_000:
            raise PrivateAPIError("paper myquant-tags text 형식이 올바르지 않습니다.")
        validated_tags = {}
        for tag, metrics in tags.items():
            if not isinstance(tag, str) or not tag.strip() or len(tag) > 100:
                raise PrivateAPIError("paper myquant tag 형식이 올바르지 않습니다.")
            validated_tags[tag] = _validate_paper_tag_metrics(metrics)
        return {"tags": validated_tags, "text": text}


def _normalize_chat_id(chat_id: str | int) -> str:
    value = str(chat_id or "").strip()
    digits = value[1:] if value.startswith("-") else value
    if not digits.isdigit() or len(digits) > 19 or (value.startswith("-") and not digits):
        raise ValueError("schedule chat 범위가 올바르지 않습니다.")
    return value


def _normalize_uuid(value: object, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field} UUID가 올바르지 않습니다.") from exc


def _write_error_code(exc: HTTPError) -> str:
    try:
        raw = exc.read(4_097)
        if len(raw) > 4_096:
            return "private_write_rejected"
        payload = json.loads(raw.decode("utf-8"))
        detail = payload.get("detail") if isinstance(payload, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            return code
    except (UnicodeDecodeError, ValueError, TypeError, OSError, AttributeError):
        pass
    return "private_write_rejected"


def _require_write_identity(
    result: dict[str, object],
    request_id: str,
    approval_id: str,
) -> None:
    if result["request_id"] != request_id or result["approval_id"] != approval_id:
        raise PrivateAPIError("Private write 식별자 응답이 일치하지 않습니다.")


def _validate_write_response(
    payload: object,
    *,
    allowed_operations: frozenset[str] = _WATCHLIST_WRITE_OPERATIONS,
) -> dict[str, object]:
    fields = {
        "request_id",
        "approval_id",
        "operation",
        "state",
        "expected_version",
        "resource_version",
        "result",
        "replayed",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise PrivateAPIError("Private write 응답 형식이 올바르지 않습니다.")
    try:
        request_id = _normalize_uuid(payload["request_id"], field="request_id")
        approval_id = _normalize_uuid(payload["approval_id"], field="approval_id")
    except ValueError as exc:
        raise PrivateAPIError("Private write UUID 응답이 올바르지 않습니다.") from exc
    operation = payload["operation"]
    state = payload["state"]
    if operation not in allowed_operations or state not in _WRITE_STATES:
        raise PrivateAPIError("Private write operation/state 응답이 올바르지 않습니다.")
    if not _valid_int(payload["expected_version"], minimum=0):
        raise PrivateAPIError("Private write expected version 응답이 올바르지 않습니다.")
    if not _valid_int(payload["resource_version"], minimum=0):
        raise PrivateAPIError("Private write resource version 응답이 올바르지 않습니다.")
    if not isinstance(payload["replayed"], bool):
        raise PrivateAPIError("Private write replay 응답이 올바르지 않습니다.")
    result = _validate_private_write_result(operation, state, payload["result"])
    return {
        **payload,
        "request_id": request_id,
        "approval_id": approval_id,
        "result": result,
    }


def _validate_preflight_response(
    payload: object,
    *,
    allowed_operations: frozenset[str] = _WATCHLIST_WRITE_OPERATIONS,
) -> dict[str, object]:
    fields = {
        "exists",
        "request_id",
        "approval_id",
        "operation",
        "state",
        "expected_version",
        "current_version",
        "result",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise PrivateAPIError("Private preflight 응답 형식이 올바르지 않습니다.")
    if not isinstance(payload["exists"], bool):
        raise PrivateAPIError("Private preflight exists 응답이 올바르지 않습니다.")
    try:
        request_id = _normalize_uuid(payload["request_id"], field="request_id")
        approval_id = _normalize_uuid(payload["approval_id"], field="approval_id")
    except ValueError as exc:
        raise PrivateAPIError("Private preflight UUID 응답이 올바르지 않습니다.") from exc
    operation = payload["operation"]
    state = payload["state"]
    if operation not in allowed_operations:
        raise PrivateAPIError("Private preflight operation 응답이 올바르지 않습니다.")
    if state is not None and state not in _WRITE_STATES:
        raise PrivateAPIError("Private preflight state 응답이 올바르지 않습니다.")
    if not _valid_int(payload["expected_version"], minimum=0):
        raise PrivateAPIError("Private preflight expected version 응답이 올바르지 않습니다.")
    if not _valid_int(payload["current_version"], minimum=0):
        raise PrivateAPIError("Private preflight current version 응답이 올바르지 않습니다.")
    if payload["exists"]:
        if state is None:
            raise PrivateAPIError("Private preflight 기존 intent 상태가 누락되었습니다.")
    elif (
        state is not None
        or payload["result"] is not None
        or payload["expected_version"] != payload["current_version"]
    ):
        raise PrivateAPIError("Private preflight 신규 intent 응답이 올바르지 않습니다.")
    result = _validate_private_write_result(operation, state, payload["result"])
    return {
        **payload,
        "request_id": request_id,
        "approval_id": approval_id,
        "result": result,
    }


def _validate_private_write_result(
    operation: str,
    state: str | None,
    result: object,
) -> dict[str, object] | None:
    if operation in _WATCHLIST_WRITE_OPERATIONS:
        return _validate_watchlist_result(operation, state, result)
    if operation in SCHEDULE_WRITE_OPERATIONS:
        return _validate_schedule_write_result(operation, state, result)
    if operation in PAPER_TRADE_OPERATIONS:
        return _validate_paper_trade_write_result(operation, state, result)
    raise PrivateAPIError("Private write operation 응답이 올바르지 않습니다.")


def _validate_paper_trade_write_result(
    operation: str,
    state: str | None,
    result: object,
) -> dict[str, object] | None:
    if result is None:
        if state == "applied":
            raise PrivateAPIError("Private Paper 적용 결과가 누락되었습니다.")
        return None
    if state != "applied" or not isinstance(result, dict):
        raise PrivateAPIError("Private Paper result 응답이 올바르지 않습니다.")
    amount_field = "total_cost" if operation == "paper.buy" else "proceeds"
    fields = {
        "side",
        "trade_id",
        "slot_id",
        "ticker",
        "quantity",
        "price",
        "fees",
        amount_field,
    }
    if set(result) != fields:
        raise PrivateAPIError("Private Paper result 형식이 올바르지 않습니다.")
    expected_side = "buy" if operation == "paper.buy" else "sell"
    if result["side"] != expected_side:
        raise PrivateAPIError("Private Paper side 응답이 올바르지 않습니다.")
    if not _valid_int(result["trade_id"]) or not _valid_int(result["slot_id"]):
        raise PrivateAPIError("Private Paper id 응답이 올바르지 않습니다.")
    ticker = result["ticker"]
    if not isinstance(ticker, str) or len(ticker) != 6 or not ticker.isdigit():
        raise PrivateAPIError("Private Paper ticker 응답이 올바르지 않습니다.")
    if not _valid_int(result["quantity"]):
        raise PrivateAPIError("Private Paper quantity 응답이 올바르지 않습니다.")
    if not _valid_number(result["price"], positive=True) or not _valid_number(
        result["fees"], positive=False
    ):
        raise PrivateAPIError("Private Paper 금액 응답이 올바르지 않습니다.")
    if not _valid_number(result[amount_field], positive=False):
        raise PrivateAPIError("Private Paper 체결 합계 응답이 올바르지 않습니다.")
    gross = int(result["quantity"]) * float(result["price"])
    expected_amount = (
        gross + float(result["fees"])
        if operation == "paper.buy"
        else gross - float(result["fees"])
    )
    if expected_amount < 0 or not math.isclose(
        float(result[amount_field]), expected_amount, rel_tol=1e-12, abs_tol=1e-9
    ):
        raise PrivateAPIError("Private Paper 체결 합계 응답이 일치하지 않습니다.")
    return dict(result)


def _require_paper_result_scope(result: dict[str, object], slot_id: int) -> None:
    payload = result.get("result")
    if isinstance(payload, dict) and payload.get("slot_id") != slot_id:
        raise PrivateAPIError("Private Paper slot 응답이 일치하지 않습니다.")


def _validate_watchlist_result(
    operation: str,
    state: str | None,
    result: object,
) -> dict[str, object] | None:
    if result is None:
        if state == "applied":
            raise PrivateAPIError("Private write 적용 결과가 누락되었습니다.")
    elif state != "applied" or not isinstance(result, dict):
        raise PrivateAPIError("Private write result 응답이 올바르지 않습니다.")
    elif operation == "watchlist.add":
        if set(result) != {"ticker", "name", "created_at", "created"}:
            raise PrivateAPIError("watchlist add 결과 형식이 올바르지 않습니다.")
        _validate_watchlist_write_item(result)
        if not isinstance(result["created"], bool):
            raise PrivateAPIError("watchlist add created 형식이 올바르지 않습니다.")
    else:
        if set(result) != {"ticker", "removed"}:
            raise PrivateAPIError("watchlist remove 결과 형식이 올바르지 않습니다.")
        _validate_watchlist_ticker(result["ticker"])
        if not isinstance(result["removed"], bool):
            raise PrivateAPIError("watchlist remove 결과가 올바르지 않습니다.")
    return dict(result) if isinstance(result, dict) else None


def _validate_schedule_write_result(
    operation: str,
    state: str | None,
    result: object,
) -> dict[str, object] | None:
    if result is None:
        if state == "applied":
            raise PrivateAPIError("Private write 적용 결과가 누락되었습니다.")
        return None
    if state != "applied" or not isinstance(result, dict):
        raise PrivateAPIError("Private write result 응답이 올바르지 않습니다.")
    if operation == "schedule.add":
        if set(result) != _SCHEDULE_FIELDS | {"created"}:
            raise PrivateAPIError("schedule add 결과 형식이 올바르지 않습니다.")
        _validate_schedule_event({key: value for key, value in result.items() if key != "created"})
        if not isinstance(result["created"], bool):
            raise PrivateAPIError("schedule add created 형식이 올바르지 않습니다.")
    else:
        flag = "deleted" if operation == "schedule.delete" else "completed"
        if set(result) != {"event_id", flag}:
            raise PrivateAPIError(f"{operation} 결과 형식이 올바르지 않습니다.")
        if not _valid_int(result["event_id"], minimum=1) or not isinstance(
            result[flag], bool
        ):
            raise PrivateAPIError(f"{operation} 결과가 올바르지 않습니다.")
    return dict(result)


def _validate_watchlist_ticker(value: object) -> str:
    if not isinstance(value, str) or len(value) != 6 or not value.isdigit():
        raise PrivateAPIError("watchlist write ticker 형식이 올바르지 않습니다.")
    return value


def _validate_watchlist_write_item(item: dict[str, object]) -> None:
    _validate_watchlist_ticker(item["ticker"])
    if not isinstance(item["name"], str) or not item["name"].strip() or len(item["name"]) > 100:
        raise PrivateAPIError("watchlist write name 형식이 올바르지 않습니다.")
    if not isinstance(item["created_at"], str) or not item["created_at"] or len(item["created_at"]) > 64:
        raise PrivateAPIError("watchlist write created_at 형식이 올바르지 않습니다.")


_PAPER_PORTFOLIO_FIELDS = {"id", "name", "seed_capital", "created_at"}
_PAPER_POSITION_FIELDS = {
    "id", "slot_id", "ticker", "name", "quantity", "avg_price",
    "opened_at", "updated_at", "slot_name",
}
_PAPER_SLOT_FIELDS = {
    "id", "portfolio_id", "name", "allocation_pct", "current_capital",
    "created_at", "n_positions", "n_trades",
}
_PAPER_TRADE_FIELDS = {
    "id", "slot_id", "ticker", "name", "side", "quantity", "price",
    "fees", "notes", "executed_at", "slot_name",
}
_PAPER_IPO_RECORD_FIELDS = {
    "id", "name", "sub_start", "sub_end", "listing_date", "grade", "score",
    "factors", "subscribed", "alloc_amount", "listing_price", "return_pct",
    "notes", "created_at", "updated_at",
}
_PAPER_IPO_STAT_FIELDS = {
    "grade", "n", "avg_return", "min_return", "max_return", "n_pos",
}
_PAPER_PERFORMANCE_FIELDS = {
    "slot_id", "slot_name", "n_closed", "win_rate", "total_pnl",
    "total_return_pct", "max_drawdown_pct", "sharpe", "n_open_positions",
    "open_cost",
}


def _valid_int(value: object, *, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _valid_number(value: object, *, positive: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and (number > 0 if positive else number >= 0)


def _filter_paper_slot(
    items: list[dict[str, object]], slot: object
) -> list[dict[str, object]]:
    if isinstance(slot, bool):
        return []
    if isinstance(slot, int):
        return [item for item in items if item["slot_id"] == slot]
    if isinstance(slot, str):
        name = slot.strip()
        return [item for item in items if item["slot_name"] == name]
    return []


def _validate_paper_portfolio(item: object) -> dict[str, object]:
    if not isinstance(item, dict) or set(item) != _PAPER_PORTFOLIO_FIELDS:
        raise PrivateAPIError("paper portfolio 항목 형식이 올바르지 않습니다.")
    if not _valid_int(item["id"]):
        raise PrivateAPIError("paper portfolio id 형식이 올바르지 않습니다.")
    if not isinstance(item["name"], str) or not item["name"].strip() or len(item["name"]) > 100:
        raise PrivateAPIError("paper portfolio name 형식이 올바르지 않습니다.")
    if not _valid_number(item["seed_capital"], positive=False):
        raise PrivateAPIError("paper portfolio 자본 형식이 올바르지 않습니다.")
    if not isinstance(item["created_at"], str) or not item["created_at"] or len(item["created_at"]) > 64:
        raise PrivateAPIError("paper portfolio created_at 형식이 올바르지 않습니다.")
    return dict(item)


def _validate_paper_position(item: object) -> dict[str, object]:
    if not isinstance(item, dict) or set(item) != _PAPER_POSITION_FIELDS:
        raise PrivateAPIError("paper position 항목 형식이 올바르지 않습니다.")
    if not _valid_int(item["id"]) or not _valid_int(item["slot_id"]):
        raise PrivateAPIError("paper position id 형식이 올바르지 않습니다.")
    ticker = item["ticker"]
    if not isinstance(ticker, str) or len(ticker) != 6 or not ticker.isdigit():
        raise PrivateAPIError("paper position ticker 형식이 올바르지 않습니다.")
    name = item["name"]
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 100):
        raise PrivateAPIError("paper position name 형식이 올바르지 않습니다.")
    if not _valid_int(item["quantity"]) or not _valid_number(item["avg_price"], positive=True):
        raise PrivateAPIError("paper position 보유 필드가 올바르지 않습니다.")
    slot_name = item["slot_name"]
    if not isinstance(slot_name, str) or not slot_name.strip() or len(slot_name) > 100:
        raise PrivateAPIError("paper position slot 형식이 올바르지 않습니다.")
    for field in ("opened_at", "updated_at"):
        value = item[field]
        if not isinstance(value, str) or not value or len(value) > 64:
            raise PrivateAPIError(f"paper position {field} 형식이 올바르지 않습니다.")
    return dict(item)


def _validate_paper_slot(item: object) -> dict[str, object]:
    if not isinstance(item, dict) or set(item) != _PAPER_SLOT_FIELDS:
        raise PrivateAPIError("paper slot 항목 형식이 올바르지 않습니다.")
    if not _valid_int(item["id"]) or not _valid_int(item["portfolio_id"]):
        raise PrivateAPIError("paper slot id 형식이 올바르지 않습니다.")
    if not isinstance(item["name"], str) or not item["name"].strip() or len(item["name"]) > 100:
        raise PrivateAPIError("paper slot name 형식이 올바르지 않습니다.")
    allocation = item["allocation_pct"]
    if not _valid_number(allocation, positive=False) or float(allocation) > 1:
        raise PrivateAPIError("paper slot allocation 형식이 올바르지 않습니다.")
    if not _valid_number(item["current_capital"], positive=False):
        raise PrivateAPIError("paper slot capital 형식이 올바르지 않습니다.")
    if not _valid_int(item["n_positions"], minimum=0) or not _valid_int(item["n_trades"], minimum=0):
        raise PrivateAPIError("paper slot 집계 형식이 올바르지 않습니다.")
    if not isinstance(item["created_at"], str) or not item["created_at"] or len(item["created_at"]) > 64:
        raise PrivateAPIError("paper slot created_at 형식이 올바르지 않습니다.")
    return dict(item)


def _validate_paper_trade(item: object) -> dict[str, object]:
    if not isinstance(item, dict) or set(item) != _PAPER_TRADE_FIELDS:
        raise PrivateAPIError("paper trade 항목 형식이 올바르지 않습니다.")
    if not _valid_int(item["id"]) or not _valid_int(item["slot_id"]):
        raise PrivateAPIError("paper trade id 형식이 올바르지 않습니다.")
    ticker = item["ticker"]
    if not isinstance(ticker, str) or len(ticker) != 6 or not ticker.isdigit():
        raise PrivateAPIError("paper trade ticker 형식이 올바르지 않습니다.")
    name = item["name"]
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 100):
        raise PrivateAPIError("paper trade name 형식이 올바르지 않습니다.")
    if item["side"] not in {"buy", "sell"} or not _valid_int(item["quantity"]):
        raise PrivateAPIError("paper trade 체결 구분 형식이 올바르지 않습니다.")
    if not _valid_number(item["price"], positive=True) or not _valid_number(item["fees"], positive=False):
        raise PrivateAPIError("paper trade 금액 형식이 올바르지 않습니다.")
    notes = item["notes"]
    if notes is not None and (not isinstance(notes, str) or not notes.strip() or len(notes) > 500):
        raise PrivateAPIError("paper trade notes 형식이 올바르지 않습니다.")
    slot_name = item["slot_name"]
    if not isinstance(slot_name, str) or not slot_name.strip() or len(slot_name) > 100:
        raise PrivateAPIError("paper trade slot 형식이 올바르지 않습니다.")
    if not isinstance(item["executed_at"], str) or not item["executed_at"] or len(item["executed_at"]) > 64:
        raise PrivateAPIError("paper trade executed_at 형식이 올바르지 않습니다.")
    return dict(item)


def _optional_ipo_text(value: object, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise PrivateAPIError(f"paper IPO {field} 형식이 올바르지 않습니다.")
    return value


def _optional_ipo_number(
    value: object, *, field: str, nonnegative: bool
) -> int | float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (nonnegative and float(value) < 0)
    ):
        raise PrivateAPIError(f"paper IPO {field} 형식이 올바르지 않습니다.")
    return value


def _validate_ipo_date(value: object, *, field: str) -> None:
    if value is None:
        return
    text = _optional_ipo_text(value, field=field, max_length=10)
    compact = text.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise PrivateAPIError(f"paper IPO {field} 형식이 올바르지 않습니다.")


def _validate_paper_ipo_record(item: object) -> dict[str, object]:
    if not isinstance(item, dict) or set(item) != _PAPER_IPO_RECORD_FIELDS:
        raise PrivateAPIError("paper IPO record 항목 형식이 올바르지 않습니다.")
    if not _valid_int(item["id"]):
        raise PrivateAPIError("paper IPO id 형식이 올바르지 않습니다.")
    if not isinstance(item["name"], str) or not item["name"].strip() or len(item["name"]) > 200:
        raise PrivateAPIError("paper IPO name 형식이 올바르지 않습니다.")
    for field in ("sub_start", "sub_end", "listing_date"):
        _validate_ipo_date(item[field], field=field)
    _optional_ipo_text(item["grade"], field="grade", max_length=20)
    score = _optional_ipo_number(item["score"], field="score", nonnegative=False)
    if score is not None and not 0 <= float(score) <= 100:
        raise PrivateAPIError("paper IPO score 형식이 올바르지 않습니다.")
    factors = _optional_ipo_text(item["factors"], field="factors", max_length=10_000)
    if factors is not None:
        try:
            parsed = json.loads(factors)
        except (TypeError, ValueError) as exc:
            raise PrivateAPIError("paper IPO factors 형식이 올바르지 않습니다.") from exc
        if not isinstance(parsed, dict):
            raise PrivateAPIError("paper IPO factors 형식이 올바르지 않습니다.")
    if item["subscribed"] not in {0, 1} or isinstance(item["subscribed"], bool):
        raise PrivateAPIError("paper IPO subscribed 형식이 올바르지 않습니다.")
    for field in ("alloc_amount", "listing_price"):
        _optional_ipo_number(item[field], field=field, nonnegative=True)
    _optional_ipo_number(item["return_pct"], field="return_pct", nonnegative=False)
    _optional_ipo_text(item["notes"], field="notes", max_length=1_000)
    for field in ("created_at", "updated_at"):
        value = item[field]
        if not isinstance(value, str) or not value or len(value) > 64:
            raise PrivateAPIError(f"paper IPO {field} 형식이 올바르지 않습니다.")
    return dict(item)


def _validate_paper_ipo_stat(item: object) -> dict[str, object]:
    if not isinstance(item, dict) or set(item) != _PAPER_IPO_STAT_FIELDS:
        raise PrivateAPIError("paper IPO stat 항목 형식이 올바르지 않습니다.")
    _optional_ipo_text(item["grade"], field="grade", max_length=20)
    if not _valid_int(item["n"]) or not _valid_int(item["n_pos"], minimum=0):
        raise PrivateAPIError("paper IPO stat 개수 형식이 올바르지 않습니다.")
    if item["n_pos"] > item["n"]:
        raise PrivateAPIError("paper IPO stat 개수 형식이 올바르지 않습니다.")
    for field in ("avg_return", "min_return", "max_return"):
        if _optional_ipo_number(item[field], field=field, nonnegative=False) is None:
            raise PrivateAPIError(f"paper IPO stat {field} 형식이 올바르지 않습니다.")
    return dict(item)


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _validate_paper_performance(item: object) -> dict[str, object]:
    if not isinstance(item, dict) or set(item) != _PAPER_PERFORMANCE_FIELDS:
        raise PrivateAPIError("paper performance 항목 형식이 올바르지 않습니다.")
    if not _valid_int(item["slot_id"]):
        raise PrivateAPIError("paper performance slot id 형식이 올바르지 않습니다.")
    if not isinstance(item["slot_name"], str) or not item["slot_name"].strip() or len(item["slot_name"]) > 100:
        raise PrivateAPIError("paper performance slot name 형식이 올바르지 않습니다.")
    if not _valid_int(item["n_closed"], minimum=0) or not _valid_int(item["n_open_positions"], minimum=0):
        raise PrivateAPIError("paper performance count 형식이 올바르지 않습니다.")
    win_rate = item["win_rate"]
    if win_rate is not None and (not _finite_number(win_rate) or not 0 <= float(win_rate) <= 100):
        raise PrivateAPIError("paper performance win_rate 형식이 올바르지 않습니다.")
    for field in ("total_pnl", "total_return_pct"):
        if not _finite_number(item[field]):
            raise PrivateAPIError(f"paper performance {field} 형식이 올바르지 않습니다.")
    for field in ("max_drawdown_pct", "open_cost"):
        if not _finite_number(item[field]) or float(item[field]) < 0:
            raise PrivateAPIError(f"paper performance {field} 형식이 올바르지 않습니다.")
    if item["sharpe"] is not None and not _finite_number(item["sharpe"]):
        raise PrivateAPIError("paper performance sharpe 형식이 올바르지 않습니다.")
    return dict(item)


def _validate_paper_tag_metrics(metrics: object) -> dict[str, object]:
    if not isinstance(metrics, dict) or set(metrics) != {"n", "pnl", "win_rate"}:
        raise PrivateAPIError("paper myquant tag metrics 형식이 올바르지 않습니다.")
    if not _valid_int(metrics["n"]):
        raise PrivateAPIError("paper myquant tag count 형식이 올바르지 않습니다.")
    if not _finite_number(metrics["pnl"]):
        raise PrivateAPIError("paper myquant tag pnl 형식이 올바르지 않습니다.")
    if not _finite_number(metrics["win_rate"]) or not 0 <= float(metrics["win_rate"]) <= 100:
        raise PrivateAPIError("paper myquant tag win_rate 형식이 올바르지 않습니다.")
    return dict(metrics)


_SCHEDULE_FIELDS = {
    "id",
    "title",
    "when_at",
    "notes",
    "completed",
    "rrule_freq",
    "rrule_byday",
    "rrule_until",
    "pre_notify_minutes",
    "pre_notify_minutes_list",
}


def _validate_schedule_event(event: object) -> dict[str, object]:
    if not isinstance(event, dict) or set(event) != _SCHEDULE_FIELDS:
        raise PrivateAPIError("schedule 항목 형식이 올바르지 않습니다.")
    event_id = event["id"]
    title = event["title"]
    when_at = event["when_at"]
    notes = event["notes"]
    if not isinstance(event_id, int) or event_id < 1:
        raise PrivateAPIError("schedule id 형식이 올바르지 않습니다.")
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        raise PrivateAPIError("schedule title 형식이 올바르지 않습니다.")
    if not isinstance(when_at, str):
        raise PrivateAPIError("schedule when_at 형식이 올바르지 않습니다.")
    try:
        datetime.fromisoformat(when_at)
    except ValueError as exc:
        raise PrivateAPIError("schedule when_at 형식이 올바르지 않습니다.") from exc
    if notes is not None and (not isinstance(notes, str) or len(notes) > 10_000):
        raise PrivateAPIError("schedule notes 형식이 올바르지 않습니다.")
    if event["completed"] is not False:
        raise PrivateAPIError("schedule completed 형식이 올바르지 않습니다.")
    if event["rrule_freq"] not in {None, "daily", "weekly", "monthly"}:
        raise PrivateAPIError("schedule rrule_freq 형식이 올바르지 않습니다.")
    for field, max_length in (("rrule_byday", 32), ("rrule_until", 32)):
        value = event[field]
        if value is not None and (not isinstance(value, str) or len(value) > max_length):
            raise PrivateAPIError(f"schedule {field} 형식이 올바르지 않습니다.")
    pre = event["pre_notify_minutes"]
    pre_list = event["pre_notify_minutes_list"]
    if not isinstance(pre, int) or pre < 0:
        raise PrivateAPIError("schedule 사전알림 형식이 올바르지 않습니다.")
    if (
        not isinstance(pre_list, list)
        or any(not isinstance(value, int) or value < 1 for value in pre_list)
        or pre_list != sorted(set(pre_list), reverse=True)
    ):
        raise PrivateAPIError("schedule 사전알림 목록이 올바르지 않습니다.")
    return dict(event)
