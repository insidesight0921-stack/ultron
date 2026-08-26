#!/usr/bin/env python3
"""Approval-gated watchlist write store for isolated contract validation.

The store has no default database path and mutations are disabled unless the
caller explicitly opts in.  It is intentionally not wired to the Private API
or the operational assistant database yet.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from private_write_contract import (
    WriteContractError,
    WriteIntent,
    build_audit_event,
    normalize_idempotency_key,
    validate_idempotency_replay,
    validate_write_intent,
)


WATCHLIST_OPERATIONS = frozenset({"watchlist.add", "watchlist.remove"})


@dataclass(frozen=True)
class WriteRecord:
    request_id: str
    approval_id: str
    operation: str
    state: str
    expected_version: int
    resource_version: int
    result: dict[str, object] | None
    replayed: bool = False


@dataclass(frozen=True)
class WritePreflight:
    exists: bool
    request_id: str
    approval_id: str
    operation: str
    state: str | None
    expected_version: int
    current_version: int
    result: dict[str, object] | None


class WatchlistWriteStore:
    """SQLite-backed write state machine with an explicit safety guard."""

    def __init__(self, db_path: Path | str, *, writes_enabled: bool = False) -> None:
        value = str(db_path or "").strip()
        if not value:
            raise ValueError("db_path is required")
        self.db_path = Path(value)
        self.writes_enabled = bool(writes_enabled)

    def _require_enabled(self) -> None:
        if not self.writes_enabled:
            raise WriteContractError("writes_disabled", "watchlist writes are disabled")

    def _connect(self) -> sqlite3.Connection:
        self._require_enabled()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema(con)
        return con

    @staticmethod
    def _ensure_schema(con: sqlite3.Connection) -> None:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                ticker     TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'private-api',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS private_resource_versions (
                resource TEXT PRIMARY KEY,
                version  INTEGER NOT NULL CHECK(version >= 0)
            );

            CREATE TABLE IF NOT EXISTS private_write_intents (
                approval_id         TEXT PRIMARY KEY,
                request_id          TEXT NOT NULL UNIQUE,
                idempotency_key_hash TEXT NOT NULL UNIQUE,
                fingerprint         TEXT NOT NULL,
                operation           TEXT NOT NULL,
                expected_version    INTEGER NOT NULL CHECK(expected_version >= 0),
                payload_json        TEXT NOT NULL,
                state               TEXT NOT NULL CHECK(
                    state IN ('pending','approved','applied','rejected','expired')
                ),
                result_json         TEXT,
                resource_version    INTEGER NOT NULL CHECK(resource_version >= 0),
                created_at          TEXT NOT NULL,
                approved_at         TEXT,
                applied_at          TEXT
            );

            CREATE TABLE IF NOT EXISTS private_write_audit (
                event_id             INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id           TEXT NOT NULL,
                approval_id          TEXT NOT NULL,
                idempotency_key_hash TEXT NOT NULL,
                domain               TEXT NOT NULL,
                action               TEXT NOT NULL,
                result               TEXT NOT NULL,
                occurred_at          TEXT NOT NULL,
                resource_version     INTEGER,
                error_code           TEXT
            );

            INSERT OR IGNORE INTO private_resource_versions(resource, version)
            VALUES ('watchlist', 0);
            """
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _normalize_intent(intent: WriteIntent) -> WriteIntent:
        normalized = validate_write_intent(
            operation=intent.operation,
            request_id=intent.request_id,
            idempotency_key=intent.idempotency_key,
            approval_id=intent.approval_id,
            expected_version=intent.expected_version,
            payload=intent.payload,
        )
        if normalized.operation not in WATCHLIST_OPERATIONS:
            raise WriteContractError(
                "operation_not_allowed",
                "only watchlist operations are supported by this store",
            )
        return normalized

    @staticmethod
    def _key_hash(intent: WriteIntent) -> str:
        return hashlib.sha256(intent.idempotency_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_uuid(value: object, *, field: str) -> str:
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise WriteContractError("invalid_identifier", f"{field} must be a UUID") from exc

    @staticmethod
    def _current_version(con: sqlite3.Connection) -> int:
        row = con.execute(
            "SELECT version FROM private_resource_versions WHERE resource = 'watchlist'"
        ).fetchone()
        return int(row["version"])

    @staticmethod
    def _row_to_record(row: sqlite3.Row, *, replayed: bool = False) -> WriteRecord:
        result = json.loads(row["result_json"]) if row["result_json"] is not None else None
        return WriteRecord(
            request_id=str(row["request_id"]),
            approval_id=str(row["approval_id"]),
            operation=str(row["operation"]),
            state=str(row["state"]),
            expected_version=int(row["expected_version"]),
            resource_version=int(row["resource_version"]),
            result=result,
            replayed=replayed,
        )

    @staticmethod
    def _intent_from_row(row: sqlite3.Row, idempotency_key: str) -> WriteIntent:
        return WriteIntent(
            operation=str(row["operation"]),
            request_id=str(row["request_id"]),
            idempotency_key=idempotency_key,
            approval_id=str(row["approval_id"]),
            expected_version=int(row["expected_version"]),
            payload=json.loads(row["payload_json"]),
        )

    @staticmethod
    def _insert_audit(
        con: sqlite3.Connection,
        intent: WriteIntent,
        *,
        result: str,
        resource_version: int,
        idempotency_key_hash: str | None = None,
        error_code: str | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        event = build_audit_event(
            intent,
            result=result,
            occurred_at=occurred_at,
            resource_version=resource_version,
            error_code=error_code,
        )
        con.execute(
            """INSERT INTO private_write_audit(
                   request_id, approval_id, idempotency_key_hash, domain, action,
                   result, occurred_at, resource_version, error_code
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["request_id"],
                event["approval_id"],
                idempotency_key_hash or event["idempotency_key_hash"],
                event["domain"],
                event["action"],
                event["result"],
                event["occurred_at"],
                event["resource_version"],
                event.get("error_code"),
            ),
        )

    def submit(self, intent: WriteIntent) -> WriteRecord:
        intent = self._normalize_intent(intent)
        key_hash = self._key_hash(intent)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM private_write_intents WHERE idempotency_key_hash = ?",
                (key_hash,),
            ).fetchone()
            if existing is not None:
                try:
                    validate_idempotency_replay(existing["fingerprint"], intent)
                except WriteContractError as exc:
                    self._insert_audit(
                        con,
                        intent,
                        result="conflict",
                        resource_version=int(existing["resource_version"]),
                        error_code=exc.code,
                    )
                    con.commit()
                    raise
                self._insert_audit(
                    con,
                    intent,
                    result="replayed",
                    resource_version=int(existing["resource_version"]),
                )
                con.commit()
                return self._row_to_record(existing, replayed=True)

            duplicate = con.execute(
                "SELECT 1 FROM private_write_intents WHERE request_id = ? OR approval_id = ?",
                (intent.request_id, intent.approval_id),
            ).fetchone()
            if duplicate is not None:
                conflict = WriteContractError(
                    "identifier_conflict",
                    "request_id or approval_id was already used",
                )
                self._insert_audit(
                    con,
                    intent,
                    result="conflict",
                    resource_version=self._current_version(con),
                    error_code=conflict.code,
                )
                con.commit()
                raise conflict

            version = self._current_version(con)
            now = self._now()
            con.execute(
                """INSERT INTO private_write_intents(
                       approval_id, request_id, idempotency_key_hash, fingerprint,
                       operation, expected_version, payload_json, state,
                       resource_version, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    intent.approval_id,
                    intent.request_id,
                    key_hash,
                    intent.fingerprint,
                    intent.operation,
                    intent.expected_version,
                    json.dumps(
                        intent.payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    version,
                    now.isoformat(),
                ),
            )
            self._insert_audit(
                con,
                intent,
                result="pending",
                resource_version=version,
                occurred_at=now,
            )
            row = con.execute(
                "SELECT * FROM private_write_intents WHERE approval_id = ?",
                (intent.approval_id,),
            ).fetchone()
            con.commit()
            return self._row_to_record(row)
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def preflight(
        self,
        *,
        operation: str,
        request_id: str,
        approval_id: str,
        idempotency_key: str,
    ) -> WritePreflight:
        operation_n = str(operation or "").strip()
        if operation_n not in WATCHLIST_OPERATIONS:
            raise WriteContractError(
                "operation_not_allowed",
                "only watchlist operations are supported by this store",
            )
        request_id_n = self._normalize_uuid(request_id, field="request_id")
        approval_id_n = self._normalize_uuid(approval_id, field="approval_id")
        idempotency_key_n = normalize_idempotency_key(idempotency_key)
        key_hash = hashlib.sha256(idempotency_key_n.encode("utf-8")).hexdigest()
        con = self._connect()
        try:
            row = con.execute(
                """SELECT versions.version AS current_version, intents.*
                   FROM private_resource_versions AS versions
                   LEFT JOIN private_write_intents AS intents
                     ON intents.idempotency_key_hash = ?
                   WHERE versions.resource = 'watchlist'""",
                (key_hash,),
            ).fetchone()
        finally:
            con.close()
        current_version = int(row["current_version"])
        if row["idempotency_key_hash"] is None:
            return WritePreflight(
                exists=False,
                request_id=request_id_n,
                approval_id=approval_id_n,
                operation=operation_n,
                state=None,
                expected_version=current_version,
                current_version=current_version,
                result=None,
            )
        if (
            str(row["request_id"]) != request_id_n
            or str(row["approval_id"]) != approval_id_n
        ):
            raise WriteContractError(
                "identifier_conflict",
                "request_id or approval_id does not match the existing intent",
            )
        if str(row["operation"]) != operation_n:
            raise WriteContractError(
                "idempotency_conflict",
                "Idempotency-Key was already used for a different operation",
            )
        result = json.loads(row["result_json"]) if row["result_json"] is not None else None
        return WritePreflight(
            exists=True,
            request_id=request_id_n,
            approval_id=approval_id_n,
            operation=operation_n,
            state=str(row["state"]),
            expected_version=int(row["expected_version"]),
            current_version=current_version,
            result=result,
        )

    def _load_for_update(
        self,
        con: sqlite3.Connection,
        approval_id: str,
        request_id: str | None = None,
    ) -> tuple[sqlite3.Row, WriteIntent]:
        row = con.execute(
            "SELECT * FROM private_write_intents WHERE approval_id = ?",
            (str(approval_id),),
        ).fetchone()
        if row is None:
            raise WriteContractError("approval_not_found", "approval was not found")
        if request_id is not None and str(row["request_id"]) != str(request_id):
            raise WriteContractError(
                "identifier_conflict",
                "request_id does not match the approval",
            )
        intent = self._intent_from_row(row, idempotency_key="stored-key-not-exported")
        return row, intent

    def approve(self, approval_id: str, *, request_id: str | None = None) -> WriteRecord:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row, intent = self._load_for_update(con, approval_id, request_id)
            if row["state"] in {"approved", "applied"}:
                con.commit()
                return self._row_to_record(row, replayed=True)
            if row["state"] != "pending":
                raise WriteContractError("approval_terminal", "approval is already terminal")
            now = self._now()
            con.execute(
                "UPDATE private_write_intents SET state = 'approved', approved_at = ? "
                "WHERE approval_id = ? AND state = 'pending'",
                (now.isoformat(), approval_id),
            )
            self._insert_audit(
                con,
                intent,
                result="approved",
                resource_version=int(row["resource_version"]),
                idempotency_key_hash=str(row["idempotency_key_hash"]),
                occurred_at=now,
            )
            updated = con.execute(
                "SELECT * FROM private_write_intents WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            con.commit()
            return self._row_to_record(updated)
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def reject(self, approval_id: str, *, request_id: str | None = None) -> WriteRecord:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row, intent = self._load_for_update(con, approval_id, request_id)
            if row["state"] == "rejected":
                con.commit()
                return self._row_to_record(row, replayed=True)
            if row["state"] != "pending":
                raise WriteContractError("approval_terminal", "approval cannot be rejected")
            now = self._now()
            con.execute(
                "UPDATE private_write_intents SET state = 'rejected' WHERE approval_id = ?",
                (approval_id,),
            )
            self._insert_audit(
                con,
                intent,
                result="rejected",
                resource_version=int(row["resource_version"]),
                idempotency_key_hash=str(row["idempotency_key_hash"]),
                occurred_at=now,
            )
            updated = con.execute(
                "SELECT * FROM private_write_intents WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            con.commit()
            return self._row_to_record(updated)
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _normalize_ticker(value: object) -> str:
        ticker = str(value or "").strip()
        if len(ticker) != 6 or not ticker.isdigit():
            raise WriteContractError("invalid_ticker", "ticker must be six digits")
        return ticker

    @staticmethod
    def _normalize_name(value: object) -> str:
        name = " ".join(str(value or "").split())
        if not name or len(name) > 100:
            raise WriteContractError("invalid_name", "name must be 1-100 characters")
        return name

    def _apply_watchlist(
        self,
        con: sqlite3.Connection,
        intent: WriteIntent,
    ) -> tuple[dict[str, object], bool]:
        ticker = self._normalize_ticker(intent.payload["ticker"])
        if intent.operation == "watchlist.remove":
            removed = con.execute(
                "DELETE FROM watchlist WHERE ticker = ?",
                (ticker,),
            ).rowcount > 0
            return {"ticker": ticker, "removed": removed}, removed

        name = self._normalize_name(intent.payload["name"])
        row = con.execute(
            "SELECT ticker, name, created_at FROM watchlist WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        created = row is None
        changed = created
        if created:
            con.execute(
                "INSERT INTO watchlist(ticker, name, source) VALUES (?, ?, 'private-api')",
                (ticker, name),
            )
        elif row["name"] == ticker and name != ticker:
            con.execute("UPDATE watchlist SET name = ? WHERE ticker = ?", (name, ticker))
            changed = True
        item = con.execute(
            "SELECT ticker, name, created_at FROM watchlist WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        return {
            "ticker": str(item["ticker"]),
            "name": str(item["name"]),
            "created_at": str(item["created_at"]),
            "created": created,
        }, changed

    def apply(self, approval_id: str, *, request_id: str | None = None) -> WriteRecord:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row, intent = self._load_for_update(con, approval_id, request_id)
            if row["state"] == "applied":
                self._insert_audit(
                    con,
                    intent,
                    result="replayed",
                    resource_version=int(row["resource_version"]),
                    idempotency_key_hash=str(row["idempotency_key_hash"]),
                )
                con.commit()
                return self._row_to_record(row, replayed=True)
            if row["state"] != "approved":
                conflict = WriteContractError(
                    "approval_required",
                    "approval is not ready to apply",
                )
                self._insert_audit(
                    con,
                    intent,
                    result="conflict",
                    resource_version=int(row["resource_version"]),
                    idempotency_key_hash=str(row["idempotency_key_hash"]),
                    error_code=conflict.code,
                )
                con.commit()
                raise conflict

            current_version = self._current_version(con)
            if int(row["expected_version"]) != current_version:
                now = self._now()
                con.execute(
                    "UPDATE private_write_intents SET state = 'expired', resource_version = ? "
                    "WHERE approval_id = ?",
                    (current_version, approval_id),
                )
                self._insert_audit(
                    con,
                    intent,
                    result="conflict",
                    resource_version=current_version,
                    idempotency_key_hash=str(row["idempotency_key_hash"]),
                    error_code="version_conflict",
                    occurred_at=now,
                )
                con.commit()
                raise WriteContractError(
                    "version_conflict",
                    "expected_version does not match the current watchlist version",
                )

            try:
                result, changed = self._apply_watchlist(con, intent)
            except WriteContractError as exc:
                now = self._now()
                con.execute(
                    "UPDATE private_write_intents SET state = 'expired', resource_version = ? "
                    "WHERE approval_id = ?",
                    (current_version, approval_id),
                )
                self._insert_audit(
                    con,
                    intent,
                    result="failed",
                    resource_version=current_version,
                    idempotency_key_hash=str(row["idempotency_key_hash"]),
                    error_code=exc.code,
                    occurred_at=now,
                )
                con.commit()
                raise
            new_version = current_version + 1 if changed else current_version
            if changed:
                con.execute(
                    "UPDATE private_resource_versions SET version = ? WHERE resource = 'watchlist'",
                    (new_version,),
                )
            now = self._now()
            result_json = json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            con.execute(
                """UPDATE private_write_intents
                   SET state = 'applied', result_json = ?, resource_version = ?, applied_at = ?
                   WHERE approval_id = ? AND state = 'approved'""",
                (result_json, new_version, now.isoformat(), approval_id),
            )
            self._insert_audit(
                con,
                intent,
                result="applied",
                resource_version=new_version,
                idempotency_key_hash=str(row["idempotency_key_hash"]),
                occurred_at=now,
            )
            updated = con.execute(
                "SELECT * FROM private_write_intents WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            con.commit()
            return self._row_to_record(updated)
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def current_version(self) -> int:
        con = self._connect()
        try:
            return self._current_version(con)
        finally:
            con.close()

    def list_audit_events(self) -> list[dict[str, object]]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT request_id, approval_id, idempotency_key_hash, domain,
                          action, result, occurred_at, resource_version, error_code
                   FROM private_write_audit ORDER BY event_id"""
            ).fetchall()
        finally:
            con.close()
        return [
            {key: row[key] for key in row.keys() if row[key] is not None}
            for row in rows
        ]

    def get_record(self, approval_id: str) -> WriteRecord:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM private_write_intents WHERE approval_id = ?",
                (str(approval_id),),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise WriteContractError("approval_not_found", "approval was not found")
        return self._row_to_record(row)
