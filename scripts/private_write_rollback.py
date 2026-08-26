#!/usr/bin/env python3
"""Isolated rollback rehearsal using only a verified backup copy.

The operational database and services are never opened for write.  Rehearsal
artifacts are intentionally preserved in an explicitly supplied empty private
directory; this module has no cleanup or service-control capability.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from private_data_security import backup_database, verify_database
from private_watchlist_write_store import WatchlistWriteStore
from private_write_contract import validate_write_intent
import watchlist_store


DEFAULT_MAX_BACKUP_AGE = timedelta(hours=24)


class PrivateWriteRollbackError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Private write rollback rehearsal failed: {code}")
        self.code = code


@dataclass(frozen=True)
class PrivateWriteRollbackReport:
    api_write_applied: bool
    legacy_writer_resumed: bool
    service_rollback_preserved_api_write: bool
    emergency_restore_verified: bool
    backup_unchanged: bool
    artifacts_preserved: bool = True

    @property
    def verified(self) -> bool:
        return all(
            (
                self.api_write_applied,
                self.legacy_writer_resumed,
                self.service_rollback_preserved_api_write,
                self.emergency_restore_verified,
                self.backup_unchanged,
                self.artifacts_preserved,
            )
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_file(path: Path) -> bool:
    try:
        return (
            path.is_absolute()
            and not path.is_symlink()
            and path.is_file()
            and path.stat().st_mode & 0o077 == 0
        )
    except OSError:
        return False


def _load_verified_backup(
    manifest_path: Path,
    *,
    now: datetime,
    max_backup_age: timedelta,
) -> tuple[Path, dict[str, object]]:
    if not _private_file(manifest_path) or manifest_path.name != "manifest.json":
        raise PrivateWriteRollbackError("invalid_manifest")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise PrivateWriteRollbackError("invalid_manifest") from exc
    if not isinstance(payload, dict) or payload.get("all_restore_verified") is not True:
        raise PrivateWriteRollbackError("unverified_manifest")
    try:
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None or now.tzinfo is None:
            raise ValueError
        age = now.astimezone(created_at.tzinfo) - created_at
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise PrivateWriteRollbackError("invalid_backup_time") from exc
    if not timedelta(minutes=-5) <= age <= max_backup_age:
        raise PrivateWriteRollbackError("stale_backup")

    databases = payload.get("databases")
    if not isinstance(databases, list):
        raise PrivateWriteRollbackError("assistant_backup_missing")
    matches = [
        item
        for item in databases
        if isinstance(item, dict) and item.get("database") == "assistant.db"
    ]
    if len(matches) != 1:
        raise PrivateWriteRollbackError("assistant_backup_missing")
    item = matches[0]
    name = item.get("backup_file")
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise PrivateWriteRollbackError("backup_path_invalid")
    backup_path = manifest_path.parent / name
    if not _private_file(backup_path):
        raise PrivateWriteRollbackError("backup_file_invalid")
    try:
        if _sha256(backup_path) != item.get("sha256"):
            raise PrivateWriteRollbackError("backup_hash_mismatch")
        verification = verify_database(backup_path)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        if isinstance(exc, PrivateWriteRollbackError):
            raise
        raise PrivateWriteRollbackError("backup_restore_invalid") from exc
    if (
        item.get("integrity") != "ok"
        or item.get("restore_verified") is not True
        or verification.get("schema_sha256") != item.get("schema_sha256")
        or verification.get("table_counts") != item.get("table_counts")
    ):
        raise PrivateWriteRollbackError("backup_signature_mismatch")
    return backup_path, verification


def _require_empty_private_workspace(workspace: Path) -> Path:
    try:
        valid = (
            workspace.is_absolute()
            and not workspace.is_symlink()
            and workspace.is_dir()
            and workspace.stat().st_mode & 0o077 == 0
            and not any(workspace.iterdir())
        )
    except OSError:
        valid = False
    if not valid:
        raise PrivateWriteRollbackError("workspace_not_empty_private_dir")
    return workspace


def _unused_tickers(db_path: Path) -> tuple[str, str]:
    existing = {item.ticker for item in watchlist_store.list_items(db_path=db_path)}
    available = (
        f"{value:06d}" for value in range(999_999, 899_999, -1) if f"{value:06d}" not in existing
    )
    try:
        return next(available), next(available)
    except StopIteration as exc:
        raise PrivateWriteRollbackError("rehearsal_ticker_unavailable") from exc


def rehearse_private_write_rollback(
    manifest_path: Path,
    workspace: Path,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PrivateWriteRollbackReport:
    """Exercise API write, service rollback, and manual restore on copies only."""
    checked_at = now or datetime.now().astimezone()
    if checked_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if max_backup_age <= timedelta(0):
        raise ValueError("max_backup_age must be positive")
    manifest = Path(manifest_path).resolve()
    work = _require_empty_private_workspace(Path(workspace).resolve())
    backup_path, baseline_signature = _load_verified_backup(
        manifest,
        now=checked_at,
        max_backup_age=max_backup_age,
    )
    backup_before = (_sha256(backup_path), backup_path.stat().st_mtime_ns)

    active_dir = work / "active-clone"
    restored_dir = work / "emergency-restore"
    active_dir.mkdir(mode=0o700)
    restored_dir.mkdir(mode=0o700)
    active_db = active_dir / "assistant.db"
    restored_db = restored_dir / "assistant.db"
    backup_database(backup_path, active_db)

    api_ticker, legacy_ticker = _unused_tickers(active_db)
    writer = WatchlistWriteStore(active_db, writes_enabled=True)
    expected_version = writer.current_version()
    request_id = str(uuid.uuid4())
    approval_id = str(uuid.uuid4())
    intent = validate_write_intent(
        operation="watchlist.add",
        request_id=request_id,
        approval_id=approval_id,
        idempotency_key=f"rollback-rehearsal-{uuid.uuid4().hex}",
        expected_version=expected_version,
        payload={"ticker": api_ticker, "name": "API rollback rehearsal"},
    )
    writer.submit(intent)
    writer.approve(approval_id, request_id=request_id)
    applied = writer.apply(approval_id, request_id=request_id)
    api_visible = watchlist_store.contains(api_ticker, db_path=active_db)

    _, legacy_created = watchlist_store.add(
        legacy_ticker,
        "Legacy rollback rehearsal",
        source="rollback-rehearsal",
        db_path=active_db,
    )
    legacy_visible = watchlist_store.contains(legacy_ticker, db_path=active_db)
    api_still_visible = watchlist_store.contains(api_ticker, db_path=active_db)

    active_signature = verify_database(active_db)
    backup_database(backup_path, restored_db)
    restored_signature = verify_database(restored_db)
    backup_after = (_sha256(backup_path), backup_path.stat().st_mtime_ns)

    report = PrivateWriteRollbackReport(
        api_write_applied=applied.state == "applied" and api_visible,
        legacy_writer_resumed=legacy_created and legacy_visible,
        service_rollback_preserved_api_write=api_still_visible,
        emergency_restore_verified=(
            restored_signature == baseline_signature
            and active_signature != baseline_signature
        ),
        backup_unchanged=backup_before == backup_after,
    )
    if not report.verified:
        raise PrivateWriteRollbackError("rehearsal_verification_failed")
    return report
