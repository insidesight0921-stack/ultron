#!/usr/bin/env python3
"""Authenticated loopback-only skeleton for the Private Data API.

This process exposes only explicitly implemented Private domains.  Write
domains remain default-disabled and require explicit writer injection plus a
validated common activation permit.
It keeps its port, token, router, and storage implementation separate from the
Shareable API.  Only allowlisted paper reads are present; generic file/SQL and
all writes remain absent.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import uuid
from collections.abc import Mapping

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from private_paper_write_contract import (
    normalize_paper_slot_id,
    validate_paper_trade_write_intent,
)
from private_schedule_write_contract import (
    normalize_schedule_chat_id,
    validate_schedule_write_intent,
)
from private_write_contract import MAX_PAYLOAD_BYTES, WriteContractError, validate_write_intent
from private_write_readiness import require_private_write_activation_permit
from private_read_store import (
    PrivateReadStoreError,
    get_paper_myquant_tags,
    get_paper_performance,
    list_paper_ipo_records,
    list_paper_ipo_stats,
    list_paper_portfolios,
    list_paper_positions,
    list_paper_slots,
    list_paper_trades,
    list_schedule_events,
    list_watchlist_items,
    normalize_chat_id,
)
from storage_paths import PATHS


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8091
API_VERSION = "v1"
TOKEN_ENV = "AI_AGENT_PRIVATE_API_TOKEN"
RESERVED_SERVICE_PORTS = {8080, 8082, 8090, 11434}
MAX_WRITE_REQUEST_BYTES = MAX_PAYLOAD_BYTES + 4_096


def validate_bind_host(host: str) -> str:
    value = str(host or "").strip()
    if value == "localhost":
        return value
    try:
        if ipaddress.ip_address(value).is_loopback:
            return value
    except ValueError:
        pass
    raise ValueError("Private Data API는 loopback 주소에만 바인딩할 수 있습니다.")


def validate_private_token(token: str) -> str:
    value = str(token or "")
    if len(value) < 32:
        raise ValueError("Private API token은 32자 이상이어야 합니다.")
    if any(character.isspace() for character in value):
        raise ValueError("Private API token에는 공백을 넣을 수 없습니다.")
    if value.lower() in {"change-me", "changeme", "example", "test-token"}:
        raise ValueError("Private API token placeholder는 사용할 수 없습니다.")
    return value


def validate_private_port(port: int) -> int:
    value = int(port)
    if not 1 <= value <= 65535:
        raise ValueError("port는 1~65535 범위여야 합니다.")
    if value in RESERVED_SERVICE_PORTS:
        raise ValueError("Private Data API는 기존 서비스와 다른 포트를 사용해야 합니다.")
    return value


def resolve_private_token(environ: Mapping[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    token = validate_private_token(source.get(TOKEN_ENV, ""))
    telegram_token = str(source.get("TELEGRAM_BOT_TOKEN", ""))
    if telegram_token and hmac.compare_digest(token, telegram_token):
        raise ValueError("Private API token은 Telegram token과 분리해야 합니다.")
    return token


def _duplicate_safe_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


async def _write_json(request: Request) -> dict[str, object]:
    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="application/json is required")
    content_length = request.headers.get("Content-Length")
    if content_length:
        try:
            length = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if length < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if length > MAX_WRITE_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Private write request is too large")
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_WRITE_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Private write request is too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_safe_object)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return payload


def _write_error(exc: WriteContractError) -> HTTPException:
    if exc.code == "payload_too_large":
        status_code = 413
    elif exc.code in {
        "invalid_ticker",
        "invalid_name",
        "invalid_paper_payload",
        "invalid_paper_slot",
        "invalid_paper_ticker",
        "invalid_paper_quantity",
        "invalid_paper_money",
        "invalid_schedule_title",
        "invalid_schedule_time",
        "invalid_schedule_payload",
        "invalid_schedule_pre_notify",
        "invalid_schedule_rrule",
        "invalid_schedule_event",
    }:
        status_code = 422
    elif exc.code in {"approval_not_found", "paper_slot_not_found"}:
        status_code = 404
    elif exc.code in {
        "idempotency_conflict",
        "identifier_conflict",
        "scope_conflict",
        "version_conflict",
        "approval_required",
        "approval_terminal",
        "paper_insufficient_capital",
        "paper_position_not_found",
        "paper_quantity_exceeds_position",
        "paper_negative_proceeds",
    }:
        status_code = 409
    elif exc.code == "writes_disabled":
        status_code = 503
    else:
        status_code = 400
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


def _record_response(record) -> dict[str, object]:
    return {
        "request_id": record.request_id,
        "approval_id": record.approval_id,
        "operation": record.operation,
        "state": record.state,
        "expected_version": record.expected_version,
        "resource_version": record.resource_version,
        "result": record.result,
        "replayed": record.replayed,
    }


def _preflight_response(record) -> dict[str, object]:
    return {
        "exists": record.exists,
        "request_id": record.request_id,
        "approval_id": record.approval_id,
        "operation": record.operation,
        "state": record.state,
        "expected_version": record.expected_version,
        "current_version": record.current_version,
        "result": record.result,
    }


def _uuid_header(request: Request, name: str) -> str:
    value = request.headers.get(name, "")
    if not value:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be a UUID") from exc


def _schedule_scope_header(request: Request) -> str:
    try:
        return normalize_schedule_chat_id(request.headers.get("X-AI-Agent-Chat-ID", ""))
    except WriteContractError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


def _paper_scope_header(request: Request) -> int:
    raw = request.headers.get("X-AI-Agent-Paper-Slot-ID", "").strip()
    try:
        if not raw.isdigit():
            raise WriteContractError("invalid_paper_slot", "Paper slot scope is invalid")
        slot_id = normalize_paper_slot_id(int(raw))
        if raw != str(slot_id):
            raise WriteContractError("invalid_paper_slot", "Paper slot scope is invalid")
        return slot_id
    except WriteContractError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


async def _require_empty_write_body(request: Request) -> None:
    body = await request.body()
    if body:
        raise HTTPException(status_code=400, detail="Request body is not supported")


def create_app(
    token: str,
    *,
    watchlist_reader=None,
    schedule_reader=None,
    paper_portfolio_reader=None,
    paper_position_reader=None,
    paper_slot_reader=None,
    paper_trade_reader=None,
    paper_ipo_record_reader=None,
    paper_ipo_stat_reader=None,
    paper_performance_reader=None,
    paper_myquant_tag_reader=None,
    watchlist_writer=None,
    enable_watchlist_writes: bool = False,
    schedule_writer=None,
    enable_schedule_writes: bool = False,
    paper_trade_writer=None,
    enable_paper_trade_writes: bool = False,
    write_activation_permit=None,
    paper_write_activation_permit=None,
) -> FastAPI:
    expected_token = validate_private_token(token)
    read_watchlist = watchlist_reader or list_watchlist_items
    read_schedule = schedule_reader or list_schedule_events
    read_paper_portfolios = paper_portfolio_reader or list_paper_portfolios
    read_paper_positions = paper_position_reader or list_paper_positions
    read_paper_slots = paper_slot_reader or list_paper_slots
    read_paper_trades = paper_trade_reader or list_paper_trades
    read_paper_ipo_records = paper_ipo_record_reader or list_paper_ipo_records
    read_paper_ipo_stats = paper_ipo_stat_reader or list_paper_ipo_stats
    read_paper_performance = paper_performance_reader or get_paper_performance
    read_paper_myquant_tags = paper_myquant_tag_reader or get_paper_myquant_tags
    watchlist_writes_active = bool(enable_watchlist_writes)
    schedule_writes_active = bool(enable_schedule_writes)
    paper_trade_writes_active = bool(enable_paper_trade_writes)
    writes_active = (
        watchlist_writes_active
        or schedule_writes_active
        or paper_trade_writes_active
    )
    if watchlist_writes_active and watchlist_writer is None:
        raise ValueError("watchlist writer is required when writes are enabled")
    if watchlist_writes_active and not bool(getattr(watchlist_writer, "writes_enabled", False)):
        raise ValueError("watchlist writer must explicitly enable mutations")
    if schedule_writes_active and schedule_writer is None:
        raise ValueError("schedule writer is required when writes are enabled")
    if schedule_writes_active and not bool(getattr(schedule_writer, "writes_enabled", False)):
        raise ValueError("schedule writer must explicitly enable mutations")
    if paper_trade_writes_active and paper_trade_writer is None:
        raise ValueError("Paper trade writer is required when writes are enabled")
    if paper_trade_writes_active and not bool(
        getattr(paper_trade_writer, "writes_enabled", False)
    ):
        raise ValueError("Paper trade writer must explicitly enable mutations")
    if watchlist_writes_active:
        require_private_write_activation_permit(
            write_activation_permit,
            database_path=getattr(watchlist_writer, "db_path", None),
        )
    if schedule_writes_active:
        require_private_write_activation_permit(
            write_activation_permit,
            database_path=getattr(schedule_writer, "db_path", None),
        )
    elif write_activation_permit is not None:
        if not watchlist_writes_active:
            raise ValueError("write activation permit requires enabled writes")
    if paper_trade_writes_active:
        require_private_write_activation_permit(
            paper_write_activation_permit,
            database_path=getattr(paper_trade_writer, "db_path", None),
        )
    elif paper_write_activation_permit is not None:
        raise ValueError("Paper write activation permit requires enabled Paper writes")
    app = FastAPI(
        title="Ultron Private Data API",
        version=API_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def require_private_auth(request: Request) -> None:
        authorization = request.headers.get("Authorization", "")
        scheme, separator, supplied = authorization.partition(" ")
        valid = (
            bool(separator)
            and scheme.lower() == "bearer"
            and bool(supplied)
            and hmac.compare_digest(supplied, expected_token)
        )
        if not valid:
            raise HTTPException(
                status_code=401,
                detail="Private API authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def reject_private_query(request: Request) -> None:
        if request.query_params:
            raise HTTPException(
                status_code=400,
                detail="Private API query parameters are not supported",
            )

    @app.middleware("http")
    async def private_no_store(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "private_data_api",
            "version": API_VERSION,
            "boundary": "private-auth-required",
        }

    private_dependencies = [
        Depends(require_private_auth),
        Depends(reject_private_query),
    ]

    @app.get("/v1/private/status", dependencies=private_dependencies)
    async def private_status() -> dict[str, object]:
        capabilities = [
            "watchlist:read",
            "schedule:read",
            "paper:portfolio:read",
            "paper:positions:read",
            "paper:slots:read",
            "paper:trades:read",
            "paper:ipo:read",
            "paper:performance:read",
            "paper:myquant-tags:read",
        ]
        if watchlist_writes_active:
            capabilities.append("watchlist:write-contract")
        if schedule_writes_active:
            capabilities.append("schedule:write-contract")
        if paper_trade_writes_active:
            capabilities.append("paper:trades:write-contract")
        return {
            "status": "ok",
            "boundary": "private-only",
            "storage_layout": PATHS.layout,
            "capabilities": capabilities,
            "writes_enabled": writes_active,
        }

    @app.get("/v1/private/watchlist", dependencies=private_dependencies)
    def private_watchlist() -> dict[str, object]:
        try:
            items = read_watchlist()
        except PrivateReadStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="Private watchlist is unavailable",
            ) from exc
        return {"items": items, "count": len(items)}

    def _schedule_response(request: Request, *, upcoming_only: bool) -> dict[str, object]:
        try:
            chat_id = normalize_chat_id(request.headers.get("X-AI-Agent-Chat-ID", ""))
        except PrivateReadStoreError as exc:
            raise HTTPException(
                status_code=400,
                detail="Private schedule scope is required",
            ) from exc
        try:
            events = read_schedule(chat_id, upcoming_only=upcoming_only)
        except PrivateReadStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="Private schedule is unavailable",
            ) from exc
        return {
            "events": events,
            "count": len(events),
            "scope": "upcoming" if upcoming_only else "all",
        }

    @app.get("/v1/private/schedule/events", dependencies=private_dependencies)
    def private_schedule_events(request: Request) -> dict[str, object]:
        return _schedule_response(request, upcoming_only=False)

    @app.get("/v1/private/schedule/events/upcoming", dependencies=private_dependencies)
    def private_schedule_upcoming(request: Request) -> dict[str, object]:
        return _schedule_response(request, upcoming_only=True)

    @app.get("/v1/private/paper/portfolios", dependencies=private_dependencies)
    def private_paper_portfolios() -> dict[str, object]:
        try:
            portfolios = read_paper_portfolios()
        except PrivateReadStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="Private paper portfolios are unavailable",
            ) from exc
        return {"portfolios": portfolios, "count": len(portfolios)}

    @app.get("/v1/private/paper/positions", dependencies=private_dependencies)
    def private_paper_positions() -> dict[str, object]:
        try:
            positions = read_paper_positions()
        except PrivateReadStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="Private paper positions are unavailable",
            ) from exc
        return {"positions": positions, "count": len(positions)}

    @app.get("/v1/private/paper/slots", dependencies=private_dependencies)
    def private_paper_slots() -> dict[str, object]:
        try:
            slots = read_paper_slots()
        except PrivateReadStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="Private paper slots are unavailable",
            ) from exc
        return {"slots": slots, "count": len(slots)}

    @app.get("/v1/private/paper/trades", dependencies=private_dependencies)
    def private_paper_trades() -> dict[str, object]:
        try:
            trades = read_paper_trades()
        except PrivateReadStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="Private paper trades are unavailable",
            ) from exc
        return {"trades": trades, "count": len(trades)}

    @app.get("/v1/private/paper/ipo/records", dependencies=private_dependencies)
    def private_paper_ipo_records() -> dict[str, object]:
        try:
            records = read_paper_ipo_records()
        except PrivateReadStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="Private paper IPO records are unavailable",
            ) from exc
        return {"records": records, "count": len(records)}

    @app.get("/v1/private/paper/ipo/stats", dependencies=private_dependencies)
    def private_paper_ipo_stats() -> dict[str, object]:
        try:
            stats = read_paper_ipo_stats()
        except PrivateReadStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="Private paper IPO stats are unavailable",
            ) from exc
        return {"stats": stats, "count": len(stats)}

    @app.get("/v1/private/paper/performance", dependencies=private_dependencies)
    def private_paper_performance() -> dict[str, object]:
        try:
            performance = read_paper_performance()
        except PrivateReadStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="Private paper performance is unavailable",
            ) from exc
        return {"performance": performance, "count": len(performance)}

    @app.get("/v1/private/paper/myquant-tags", dependencies=private_dependencies)
    def private_paper_myquant_tags() -> dict[str, object]:
        try:
            return read_paper_myquant_tags()
        except PrivateReadStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="Private paper MyQuant tags are unavailable",
            ) from exc

    if watchlist_writes_active:
        @app.post(
            "/v1/private/watchlist/write/intents",
            dependencies=private_dependencies,
        )
        async def private_watchlist_write_intent(request: Request):
            body = await _write_json(request)
            if set(body) != {"operation", "expected_version", "payload"}:
                raise HTTPException(status_code=400, detail="Invalid private write envelope")
            try:
                intent = validate_write_intent(
                    operation=body["operation"],
                    request_id=request.headers.get("X-Request-ID", ""),
                    idempotency_key=request.headers.get("Idempotency-Key", ""),
                    approval_id=request.headers.get("X-Approval-ID", ""),
                    expected_version=body["expected_version"],
                    payload=body["payload"],
                )
                record = watchlist_writer.submit(intent)
            except WriteContractError as exc:
                raise _write_error(exc) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Private watchlist write store is unavailable",
                ) from exc
            response = _record_response(record)
            return JSONResponse(response, status_code=200 if record.replayed else 202)

        @app.post(
            "/v1/private/watchlist/write/preflight",
            dependencies=private_dependencies,
        )
        async def private_watchlist_write_preflight(request: Request) -> dict[str, object]:
            await _require_empty_write_body(request)
            operation = request.headers.get("X-Write-Operation", "")
            idempotency_key = request.headers.get("Idempotency-Key", "")
            request_id = _uuid_header(request, "X-Request-ID")
            approval_id = _uuid_header(request, "X-Approval-ID")
            try:
                record = watchlist_writer.preflight(
                    operation=operation,
                    request_id=request_id,
                    approval_id=approval_id,
                    idempotency_key=idempotency_key,
                )
                return _preflight_response(record)
            except WriteContractError as exc:
                raise _write_error(exc) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Private watchlist write store is unavailable",
                ) from exc

        async def _transition(request: Request, action: str) -> dict[str, object]:
            await _require_empty_write_body(request)
            approval_id = _uuid_header(request, "X-Approval-ID")
            request_id = _uuid_header(request, "X-Request-ID")
            try:
                method = getattr(watchlist_writer, action)
                return _record_response(method(approval_id, request_id=request_id))
            except WriteContractError as exc:
                raise _write_error(exc) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Private watchlist write store is unavailable",
                ) from exc

        @app.post(
            "/v1/private/watchlist/write/approvals/approve",
            dependencies=private_dependencies,
        )
        async def private_watchlist_write_approve(request: Request) -> dict[str, object]:
            return await _transition(request, "approve")

        @app.post(
            "/v1/private/watchlist/write/approvals/reject",
            dependencies=private_dependencies,
        )
        async def private_watchlist_write_reject(request: Request) -> dict[str, object]:
            return await _transition(request, "reject")

        @app.post(
            "/v1/private/watchlist/write/approvals/apply",
            dependencies=private_dependencies,
        )
        async def private_watchlist_write_apply(request: Request) -> dict[str, object]:
            return await _transition(request, "apply")

    if schedule_writes_active:
        @app.post(
            "/v1/private/schedule/write/intents",
            dependencies=private_dependencies,
        )
        async def private_schedule_write_intent(request: Request):
            body = await _write_json(request)
            if set(body) != {"operation", "expected_version", "payload"}:
                raise HTTPException(status_code=400, detail="Invalid private write envelope")
            chat_id = _schedule_scope_header(request)
            try:
                intent = validate_schedule_write_intent(
                    operation=body["operation"],
                    chat_id=chat_id,
                    request_id=request.headers.get("X-Request-ID", ""),
                    idempotency_key=request.headers.get("Idempotency-Key", ""),
                    approval_id=request.headers.get("X-Approval-ID", ""),
                    expected_version=body["expected_version"],
                    payload=body["payload"],
                )
                record = schedule_writer.submit(intent)
            except WriteContractError as exc:
                raise _write_error(exc) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Private schedule write store is unavailable",
                ) from exc
            response = _record_response(record)
            return JSONResponse(response, status_code=200 if record.replayed else 202)

        @app.post(
            "/v1/private/schedule/write/preflight",
            dependencies=private_dependencies,
        )
        async def private_schedule_write_preflight(request: Request) -> dict[str, object]:
            await _require_empty_write_body(request)
            chat_id = _schedule_scope_header(request)
            operation = request.headers.get("X-Write-Operation", "")
            request_id = _uuid_header(request, "X-Request-ID")
            approval_id = _uuid_header(request, "X-Approval-ID")
            try:
                record = schedule_writer.preflight(
                    operation=operation,
                    scope_value=chat_id,
                    request_id=request_id,
                    approval_id=approval_id,
                    idempotency_key=request.headers.get("Idempotency-Key", ""),
                )
                return _preflight_response(record)
            except WriteContractError as exc:
                raise _write_error(exc) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Private schedule write store is unavailable",
                ) from exc

        async def _schedule_transition(request: Request, action: str) -> dict[str, object]:
            await _require_empty_write_body(request)
            chat_id = _schedule_scope_header(request)
            approval_id = _uuid_header(request, "X-Approval-ID")
            request_id = _uuid_header(request, "X-Request-ID")
            try:
                method = getattr(schedule_writer, action)
                return _record_response(
                    method(
                        approval_id,
                        request_id=request_id,
                        scope_value=chat_id,
                    )
                )
            except WriteContractError as exc:
                raise _write_error(exc) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Private schedule write store is unavailable",
                ) from exc

        @app.post(
            "/v1/private/schedule/write/approvals/approve",
            dependencies=private_dependencies,
        )
        async def private_schedule_write_approve(request: Request) -> dict[str, object]:
            return await _schedule_transition(request, "approve")

        @app.post(
            "/v1/private/schedule/write/approvals/reject",
            dependencies=private_dependencies,
        )
        async def private_schedule_write_reject(request: Request) -> dict[str, object]:
            return await _schedule_transition(request, "reject")

        @app.post(
            "/v1/private/schedule/write/approvals/apply",
            dependencies=private_dependencies,
        )
        async def private_schedule_write_apply(request: Request) -> dict[str, object]:
            return await _schedule_transition(request, "apply")

    if paper_trade_writes_active:
        @app.post(
            "/v1/private/paper/write/intents",
            dependencies=private_dependencies,
        )
        async def private_paper_write_intent(request: Request):
            body = await _write_json(request)
            if set(body) != {"operation", "expected_version", "payload"}:
                raise HTTPException(status_code=400, detail="Invalid private write envelope")
            slot_id = _paper_scope_header(request)
            try:
                intent = validate_paper_trade_write_intent(
                    operation=body["operation"],
                    request_id=request.headers.get("X-Request-ID", ""),
                    idempotency_key=request.headers.get("Idempotency-Key", ""),
                    approval_id=request.headers.get("X-Approval-ID", ""),
                    expected_version=body["expected_version"],
                    payload=body["payload"],
                )
                if intent.slot_id != slot_id:
                    raise WriteContractError(
                        "scope_conflict", "Paper payload slot differs from request scope"
                    )
                record = paper_trade_writer.submit(intent)
            except WriteContractError as exc:
                raise _write_error(exc) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Private Paper write store is unavailable",
                ) from exc
            response = _record_response(record)
            return JSONResponse(response, status_code=200 if record.replayed else 202)

        @app.post(
            "/v1/private/paper/write/preflight",
            dependencies=private_dependencies,
        )
        async def private_paper_write_preflight(request: Request) -> dict[str, object]:
            await _require_empty_write_body(request)
            slot_id = _paper_scope_header(request)
            request_id = _uuid_header(request, "X-Request-ID")
            approval_id = _uuid_header(request, "X-Approval-ID")
            try:
                record = paper_trade_writer.preflight(
                    operation=request.headers.get("X-Write-Operation", ""),
                    scope_value=slot_id,
                    request_id=request_id,
                    approval_id=approval_id,
                    idempotency_key=request.headers.get("Idempotency-Key", ""),
                )
                return _preflight_response(record)
            except WriteContractError as exc:
                raise _write_error(exc) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Private Paper write store is unavailable",
                ) from exc

        async def _paper_transition(request: Request, action: str) -> dict[str, object]:
            await _require_empty_write_body(request)
            slot_id = _paper_scope_header(request)
            approval_id = _uuid_header(request, "X-Approval-ID")
            request_id = _uuid_header(request, "X-Request-ID")
            try:
                method = getattr(paper_trade_writer, action)
                return _record_response(
                    method(
                        approval_id,
                        request_id=request_id,
                        scope_value=slot_id,
                    )
                )
            except WriteContractError as exc:
                raise _write_error(exc) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Private Paper write store is unavailable",
                ) from exc

        @app.post(
            "/v1/private/paper/write/approvals/approve",
            dependencies=private_dependencies,
        )
        async def private_paper_write_approve(request: Request) -> dict[str, object]:
            return await _paper_transition(request, "approve")

        @app.post(
            "/v1/private/paper/write/approvals/reject",
            dependencies=private_dependencies,
        )
        async def private_paper_write_reject(request: Request) -> dict[str, object]:
            return await _paper_transition(request, "reject")

        @app.post(
            "/v1/private/paper/write/approvals/apply",
            dependencies=private_dependencies,
        )
        async def private_paper_write_apply(request: Request) -> dict[str, object]:
            return await _paper_transition(request, "apply")

    return app


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(PATHS.project / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    try:
        host = validate_bind_host(args.host)
        port = validate_private_port(args.port)
        token = resolve_private_token()
        from private_write_runtime import load_private_write_runtime_bundle

        runtime_bundle = load_private_write_runtime_bundle()
    except ValueError as exc:
        parser.error(str(exc))
    except RuntimeError as exc:
        parser.error(str(exc))
    if runtime_bundle is None:
        app = create_app(token)
    else:
        writer = runtime_bundle.build_api_writer()
        app_kwargs = {
            "watchlist_writer": writer,
            "enable_watchlist_writes": True,
            "write_activation_permit": runtime_bundle.activation_permit,
        }
        if runtime_bundle.schedule_writes_enabled:
            app_kwargs.update(
                schedule_writer=runtime_bundle.build_schedule_api_writer(),
                enable_schedule_writes=True,
            )
        app = create_app(
            token,
            **app_kwargs,
        )
    # 인증 헤더는 기본 access log에 나오지 않지만 query string은 그대로 기록된다.
    # 잘못된 query-token 요청도 비밀값을 남기지 않도록 access log를 끈다.
    uvicorn.run(app, host=host, port=port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
