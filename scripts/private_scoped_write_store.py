#!/usr/bin/env python3
"""Reusable approval state machine for private, scope-bound mutations.

The store has no default database and is disabled unless explicitly enabled.
Domain subclasses provide payload validation and the final SQL mutation.  Raw
scope values are kept out of audit rows; only a one-way scope hash is logged.
"""
from __future__ import annotations

import hashlib
import hmac
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
)


@dataclass(frozen=True)
class ScopedWriteIntent:
    intent: WriteIntent
    scope_hash: str
    scope_value: str
    fingerprint: str


@dataclass(frozen=True)
class ScopedWriteRecord:
    request_id: str
    approval_id: str
    operation: str
    state: str
    expected_version: int
    resource_version: int
    result: dict[str, object] | None
    replayed: bool = False


@dataclass(frozen=True)
class ScopedWritePreflight:
    exists: bool
    request_id: str
    approval_id: str
    operation: str
    state: str | None
    expected_version: int
    current_version: int
    result: dict[str, object] | None


class ScopedApprovalWriteStore:
    """Common pending→approved→applied lifecycle with per-scope versions."""

    resource_name = ""
    allowed_operations: frozenset[str] = frozenset()

    def __init__(self, db_path: Path | str, *, writes_enabled: bool = False) -> None:
        value = str(db_path or "").strip()
        if not value:
            raise ValueError("db_path is required")
        if not self.resource_name or not self.allowed_operations:
            raise TypeError("scoped store domain configuration is required")
        self.db_path = Path(value)
        self.writes_enabled = bool(writes_enabled)

    def _require_enabled(self) -> None:
        if not self.writes_enabled:
            raise WriteContractError("writes_disabled", f"{self.resource_name} writes are disabled")

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

    def _ensure_schema(self, con: sqlite3.Connection) -> None:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS private_scoped_resource_versions (
                resource_key TEXT PRIMARY KEY,
                version      INTEGER NOT NULL CHECK(version >= 0)
            );

            CREATE TABLE IF NOT EXISTS private_scoped_write_intents (
                approval_id          TEXT PRIMARY KEY,
                request_id           TEXT NOT NULL UNIQUE,
                idempotency_key_hash TEXT NOT NULL UNIQUE,
                fingerprint          TEXT NOT NULL,
                resource_name        TEXT NOT NULL,
                scope_hash           TEXT NOT NULL,
                scope_value          TEXT NOT NULL,
                operation            TEXT NOT NULL,
                expected_version     INTEGER NOT NULL CHECK(expected_version >= 0),
                payload_json         TEXT NOT NULL,
                state                TEXT NOT NULL CHECK(
                    state IN ('pending','approved','applied','rejected','expired')
                ),
                result_json          TEXT,
                resource_version     INTEGER NOT NULL CHECK(resource_version >= 0),
                created_at           TEXT NOT NULL,
                approved_at          TEXT,
                applied_at           TEXT
            );

            CREATE TABLE IF NOT EXISTS private_scoped_write_audit (
                event_id             INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id           TEXT NOT NULL,
                approval_id          TEXT NOT NULL,
                idempotency_key_hash TEXT NOT NULL,
                domain               TEXT NOT NULL,
                action               TEXT NOT NULL,
                scope_hash           TEXT NOT NULL,
                result               TEXT NOT NULL,
                occurred_at          TEXT NOT NULL,
                resource_version     INTEGER,
                error_code           TEXT
            );
            """
        )
        self._ensure_domain_schema(con)

    def _ensure_domain_schema(self, con: sqlite3.Connection) -> None:
        raise NotImplementedError

    def _normalize_bound_intent(self, intent: object) -> ScopedWriteIntent:
        raise NotImplementedError

    def _normalize_scope(self, scope_value: object) -> tuple[str, str]:
        raise NotImplementedError

    def _apply_domain(
        self,
        con: sqlite3.Connection,
        intent: WriteIntent,
        scope_value: str,
    ) -> tuple[dict[str, object], bool]:
        raise NotImplementedError

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _uuid(value: object, *, field: str) -> str:
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise WriteContractError("invalid_identifier", f"{field} must be a UUID") from exc

    @staticmethod
    def _key_hash(idempotency_key: str) -> str:
        return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()

    def _resource_key(self, scope_hash: str) -> str:
        return f"{self.resource_name}:{scope_hash}"

    @staticmethod
    def _row_to_record(row: sqlite3.Row, *, replayed: bool = False) -> ScopedWriteRecord:
        result = json.loads(row["result_json"]) if row["result_json"] is not None else None
        return ScopedWriteRecord(
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
    def _intent_from_row(row: sqlite3.Row) -> WriteIntent:
        return WriteIntent(
            operation=str(row["operation"]),
            request_id=str(row["request_id"]),
            idempotency_key="stored-key-not-exported",
            approval_id=str(row["approval_id"]),
            expected_version=int(row["expected_version"]),
            payload=json.loads(row["payload_json"]),
        )

    @staticmethod
    def _insert_audit(
        con: sqlite3.Connection,
        intent: WriteIntent,
        *,
        scope_hash: str,
        result: str,
        resource_version: int,
        idempotency_key_hash: str,
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
            """INSERT INTO private_scoped_write_audit(
                   request_id, approval_id, idempotency_key_hash, domain, action,
                   scope_hash, result, occurred_at, resource_version, error_code
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["request_id"],
                event["approval_id"],
                idempotency_key_hash,
                event["domain"],
                event["action"],
                scope_hash,
                event["result"],
                event["occurred_at"],
                event["resource_version"],
                event.get("error_code"),
            ),
        )

    def _version(self, con: sqlite3.Connection, scope_hash: str) -> int:
        resource_key = self._resource_key(scope_hash)
        con.execute(
            "INSERT OR IGNORE INTO private_scoped_resource_versions(resource_key, version) "
            "VALUES (?, 0)",
            (resource_key,),
        )
        row = con.execute(
            "SELECT version FROM private_scoped_resource_versions WHERE resource_key = ?",
            (resource_key,),
        ).fetchone()
        return int(row["version"])

    def submit(self, intent: object) -> ScopedWriteRecord:
        bound = self._normalize_bound_intent(intent)
        generic = bound.intent
        key_hash = self._key_hash(generic.idempotency_key)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM private_scoped_write_intents WHERE idempotency_key_hash = ?",
                (key_hash,),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["fingerprint"]), bound.fingerprint):
                    conflict = WriteContractError(
                        "idempotency_conflict",
                        "Idempotency-Key was already used for a different request or scope",
                    )
                    self._insert_audit(
                        con,
                        generic,
                        scope_hash=bound.scope_hash,
                        result="conflict",
                        resource_version=int(existing["resource_version"]),
                        idempotency_key_hash=key_hash,
                        error_code=conflict.code,
                    )
                    con.commit()
                    raise conflict
                self._insert_audit(
                    con,
                    generic,
                    scope_hash=bound.scope_hash,
                    result="replayed",
                    resource_version=int(existing["resource_version"]),
                    idempotency_key_hash=key_hash,
                )
                con.commit()
                return self._row_to_record(existing, replayed=True)

            duplicate = con.execute(
                "SELECT 1 FROM private_scoped_write_intents "
                "WHERE request_id = ? OR approval_id = ?",
                (generic.request_id, generic.approval_id),
            ).fetchone()
            if duplicate is not None:
                conflict = WriteContractError(
                    "identifier_conflict", "request_id or approval_id was already used"
                )
                self._insert_audit(
                    con,
                    generic,
                    scope_hash=bound.scope_hash,
                    result="conflict",
                    resource_version=self._version(con, bound.scope_hash),
                    idempotency_key_hash=key_hash,
                    error_code=conflict.code,
                )
                con.commit()
                raise conflict

            version = self._version(con, bound.scope_hash)
            now = self._now()
            con.execute(
                """INSERT INTO private_scoped_write_intents(
                       approval_id, request_id, idempotency_key_hash, fingerprint,
                       resource_name, scope_hash, scope_value, operation,
                       expected_version, payload_json, state, resource_version, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    generic.approval_id,
                    generic.request_id,
                    key_hash,
                    bound.fingerprint,
                    self.resource_name,
                    bound.scope_hash,
                    bound.scope_value,
                    generic.operation,
                    generic.expected_version,
                    json.dumps(
                        generic.payload,
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
                generic,
                scope_hash=bound.scope_hash,
                result="pending",
                resource_version=version,
                idempotency_key_hash=key_hash,
                occurred_at=now,
            )
            row = con.execute(
                "SELECT * FROM private_scoped_write_intents WHERE approval_id = ?",
                (generic.approval_id,),
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
        scope_value: object,
        request_id: str,
        approval_id: str,
        idempotency_key: str,
    ) -> ScopedWritePreflight:
        operation_n = str(operation or "").strip()
        if operation_n not in self.allowed_operations:
            raise WriteContractError("operation_not_allowed", "operation is not supported")
        request_id_n = self._uuid(request_id, field="request_id")
        approval_id_n = self._uuid(approval_id, field="approval_id")
        key_n = normalize_idempotency_key(idempotency_key)
        key_hash = self._key_hash(key_n)
        scope_n, scope_hash = self._normalize_scope(scope_value)
        con = self._connect()
        try:
            current_version = self._version(con, scope_hash)
            row = con.execute(
                "SELECT * FROM private_scoped_write_intents WHERE idempotency_key_hash = ?",
                (key_hash,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return ScopedWritePreflight(
                exists=False,
                request_id=request_id_n,
                approval_id=approval_id_n,
                operation=operation_n,
                state=None,
                expected_version=current_version,
                current_version=current_version,
                result=None,
            )
        if str(row["scope_hash"]) != scope_hash or str(row["scope_value"]) != scope_n:
            raise WriteContractError("scope_conflict", "write intent belongs to another scope")
        if str(row["request_id"]) != request_id_n or str(row["approval_id"]) != approval_id_n:
            raise WriteContractError(
                "identifier_conflict", "request_id or approval_id does not match the intent"
            )
        if str(row["operation"]) != operation_n:
            raise WriteContractError(
                "idempotency_conflict", "Idempotency-Key was used for another operation"
            )
        result = json.loads(row["result_json"]) if row["result_json"] is not None else None
        return ScopedWritePreflight(
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
        request_id: str | None,
        scope_value: object,
    ) -> tuple[sqlite3.Row, WriteIntent]:
        row = con.execute(
            "SELECT * FROM private_scoped_write_intents WHERE approval_id = ?",
            (self._uuid(approval_id, field="approval_id"),),
        ).fetchone()
        if row is None:
            raise WriteContractError("approval_not_found", "approval was not found")
        if request_id is not None and str(row["request_id"]) != self._uuid(
            request_id, field="request_id"
        ):
            raise WriteContractError("identifier_conflict", "request_id does not match approval")
        scope_n, scope_hash = self._normalize_scope(scope_value)
        if str(row["scope_hash"]) != scope_hash or str(row["scope_value"]) != scope_n:
            raise WriteContractError("scope_conflict", "approval belongs to another scope")
        return row, self._intent_from_row(row)

    def approve(
        self,
        approval_id: str,
        *,
        scope_value: object,
        request_id: str | None = None,
    ) -> ScopedWriteRecord:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row, intent = self._load_for_update(con, approval_id, request_id, scope_value)
            if row["state"] in {"approved", "applied"}:
                con.commit()
                return self._row_to_record(row, replayed=True)
            if row["state"] != "pending":
                raise WriteContractError("approval_terminal", "approval is already terminal")
            now = self._now()
            con.execute(
                "UPDATE private_scoped_write_intents SET state='approved', approved_at=? "
                "WHERE approval_id=? AND state='pending'",
                (now.isoformat(), row["approval_id"]),
            )
            self._insert_audit(
                con,
                intent,
                scope_hash=str(row["scope_hash"]),
                result="approved",
                resource_version=int(row["resource_version"]),
                idempotency_key_hash=str(row["idempotency_key_hash"]),
                occurred_at=now,
            )
            updated = con.execute(
                "SELECT * FROM private_scoped_write_intents WHERE approval_id=?",
                (row["approval_id"],),
            ).fetchone()
            con.commit()
            return self._row_to_record(updated)
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def reject(
        self,
        approval_id: str,
        *,
        scope_value: object,
        request_id: str | None = None,
    ) -> ScopedWriteRecord:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row, intent = self._load_for_update(con, approval_id, request_id, scope_value)
            if row["state"] == "rejected":
                con.commit()
                return self._row_to_record(row, replayed=True)
            if row["state"] != "pending":
                raise WriteContractError("approval_terminal", "approval cannot be rejected")
            con.execute(
                "UPDATE private_scoped_write_intents SET state='rejected' WHERE approval_id=?",
                (row["approval_id"],),
            )
            self._insert_audit(
                con,
                intent,
                scope_hash=str(row["scope_hash"]),
                result="rejected",
                resource_version=int(row["resource_version"]),
                idempotency_key_hash=str(row["idempotency_key_hash"]),
            )
            updated = con.execute(
                "SELECT * FROM private_scoped_write_intents WHERE approval_id=?",
                (row["approval_id"],),
            ).fetchone()
            con.commit()
            return self._row_to_record(updated)
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def apply(
        self,
        approval_id: str,
        *,
        scope_value: object,
        request_id: str | None = None,
    ) -> ScopedWriteRecord:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row, intent = self._load_for_update(con, approval_id, request_id, scope_value)
            if row["state"] == "applied":
                self._insert_audit(
                    con,
                    intent,
                    scope_hash=str(row["scope_hash"]),
                    result="replayed",
                    resource_version=int(row["resource_version"]),
                    idempotency_key_hash=str(row["idempotency_key_hash"]),
                )
                con.commit()
                return self._row_to_record(row, replayed=True)
            if row["state"] != "approved":
                conflict = WriteContractError("approval_required", "approval is not ready")
                self._insert_audit(
                    con,
                    intent,
                    scope_hash=str(row["scope_hash"]),
                    result="conflict",
                    resource_version=int(row["resource_version"]),
                    idempotency_key_hash=str(row["idempotency_key_hash"]),
                    error_code=conflict.code,
                )
                con.commit()
                raise conflict

            current_version = self._version(con, str(row["scope_hash"]))
            if int(row["expected_version"]) != current_version:
                con.execute(
                    "UPDATE private_scoped_write_intents SET state='expired', resource_version=? "
                    "WHERE approval_id=?",
                    (current_version, row["approval_id"]),
                )
                self._insert_audit(
                    con,
                    intent,
                    scope_hash=str(row["scope_hash"]),
                    result="conflict",
                    resource_version=current_version,
                    idempotency_key_hash=str(row["idempotency_key_hash"]),
                    error_code="version_conflict",
                )
                con.commit()
                raise WriteContractError(
                    "version_conflict", "expected_version does not match current version"
                )
            try:
                result, changed = self._apply_domain(con, intent, str(row["scope_value"]))
            except WriteContractError as exc:
                con.execute(
                    "UPDATE private_scoped_write_intents SET state='expired', resource_version=? "
                    "WHERE approval_id=?",
                    (current_version, row["approval_id"]),
                )
                self._insert_audit(
                    con,
                    intent,
                    scope_hash=str(row["scope_hash"]),
                    result="failed",
                    resource_version=current_version,
                    idempotency_key_hash=str(row["idempotency_key_hash"]),
                    error_code=exc.code,
                )
                con.commit()
                raise
            new_version = current_version + 1 if changed else current_version
            if changed:
                con.execute(
                    "UPDATE private_scoped_resource_versions SET version=? WHERE resource_key=?",
                    (new_version, self._resource_key(str(row["scope_hash"]))),
                )
            now = self._now()
            con.execute(
                """UPDATE private_scoped_write_intents
                   SET state='applied', result_json=?, resource_version=?, applied_at=?
                   WHERE approval_id=? AND state='approved'""",
                (
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    new_version,
                    now.isoformat(),
                    row["approval_id"],
                ),
            )
            self._insert_audit(
                con,
                intent,
                scope_hash=str(row["scope_hash"]),
                result="applied",
                resource_version=new_version,
                idempotency_key_hash=str(row["idempotency_key_hash"]),
                occurred_at=now,
            )
            updated = con.execute(
                "SELECT * FROM private_scoped_write_intents WHERE approval_id=?",
                (row["approval_id"],),
            ).fetchone()
            con.commit()
            return self._row_to_record(updated)
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def current_version(self, scope_value: object) -> int:
        _, scope_hash = self._normalize_scope(scope_value)
        con = self._connect()
        try:
            return self._version(con, scope_hash)
        finally:
            con.close()

    def list_audit_events(self) -> list[dict[str, object]]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT request_id, approval_id, idempotency_key_hash, domain,
                          action, scope_hash, result, occurred_at, resource_version, error_code
                   FROM private_scoped_write_audit ORDER BY event_id"""
            ).fetchall()
        finally:
            con.close()
        return [{key: row[key] for key in row.keys() if row[key] is not None} for row in rows]

    def get_record(self, approval_id: str, *, scope_value: object) -> ScopedWriteRecord:
        con = self._connect()
        try:
            row, _ = self._load_for_update(con, approval_id, None, scope_value)
            return self._row_to_record(row)
        finally:
            con.close()
