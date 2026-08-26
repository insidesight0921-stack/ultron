#!/usr/bin/env python3
"""Fail-closed readiness contract for a future Private watchlist cutover.

This module only inspects explicitly supplied evidence.  It does not read
environment variables, change service configuration, create backups, mutate
the operational database, or enable any write path.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from private_data_security import verify_database


EXPECTED_WRITER_OWNER = "private-data-api"
DEFAULT_MAX_BACKUP_AGE = timedelta(hours=24)
SUPPORTED_PRIVATE_WRITE_DATABASE_NAMES = frozenset({"assistant.db", "paper.db"})


class PrivateWriteReadinessError(RuntimeError):
    """Raised with non-sensitive check codes when readiness is incomplete."""

    def __init__(self, failed_codes: tuple[str, ...]) -> None:
        super().__init__("Private write readiness failed: " + ", ".join(failed_codes))
        self.failed_codes = failed_codes


class PrivateWriteActivationError(ValueError):
    """Raised when a write component receives no valid common permit."""


_PERMIT_SEAL = object()


def _canonical_path_text(path: Path) -> str:
    """Use a stable path spelling across macOS NFC/NFD process contexts."""
    return unicodedata.normalize("NFD", os.fspath(path.resolve()))


class PrivateWriteActivationPermit:
    """Opaque permit issued only after the complete readiness contract passes."""

    __slots__ = ("_database_path", "_fingerprint", "_seal")

    def __init__(self, database_path: Path, fingerprint: str, seal: object) -> None:
        if seal is not _PERMIT_SEAL:
            raise PrivateWriteActivationError("activation permit cannot be constructed directly")
        self._database_path = database_path.resolve()
        self._fingerprint = fingerprint
        self._seal = seal

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def __repr__(self) -> str:
        return "PrivateWriteActivationPermit(validated=True)"


@dataclass(frozen=True)
class PrivateWriteReadinessEvidence:
    """Explicit evidence required before any operational write activation."""

    api_writer_enabled: bool
    client_writes_enabled: bool
    consumer_executor_enabled: bool
    writer_owners: tuple[str, ...]
    database_path: Path
    backup_manifest_path: Path
    user_approval_required: bool
    direct_db_fallback_disabled: bool
    rollback_verified: bool
    expected_writer_owner: str = EXPECTED_WRITER_OWNER

    def __post_init__(self) -> None:
        boolean_fields = (
            self.api_writer_enabled,
            self.client_writes_enabled,
            self.consumer_executor_enabled,
            self.user_approval_required,
            self.direct_db_fallback_disabled,
            self.rollback_verified,
        )
        if any(type(value) is not bool for value in boolean_fields):
            raise TypeError("readiness flags must be bool")
        if not isinstance(self.database_path, Path) or not isinstance(
            self.backup_manifest_path, Path
        ):
            raise TypeError("readiness paths must be pathlib.Path")
        if not isinstance(self.writer_owners, tuple) or any(
            not isinstance(owner, str) for owner in self.writer_owners
        ):
            raise TypeError("writer_owners must be a tuple of strings")


@dataclass(frozen=True)
class PrivateWriteReadinessCheck:
    code: str
    ready: bool


@dataclass(frozen=True)
class PrivateWriteReadinessReport:
    ready: bool
    checks: tuple[PrivateWriteReadinessCheck, ...]

    @property
    def failed_codes(self) -> tuple[str, ...]:
        return tuple(check.code for check in self.checks if not check.ready)

    def require_ready(self) -> None:
        if not self.ready:
            raise PrivateWriteReadinessError(self.failed_codes)


def _is_private_regular_file(path: Path, *, expected_name: str | None = None) -> bool:
    try:
        return (
            path.is_absolute()
            and (expected_name is None or path.name == expected_name)
            and not path.is_symlink()
            and path.is_file()
            and path.stat().st_mode & 0o077 == 0
        )
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_integrity_ok(path: Path) -> bool:
    try:
        result = verify_database(path)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return False
    return result.get("integrity") == "ok" and result.get("restore_verified") is True


def _backup_checks(
    manifest_path: Path,
    *,
    database_name: str,
    now: datetime,
    max_backup_age: timedelta,
) -> tuple[bool, bool, bool]:
    """Return manifest-valid, database-valid, fresh without exposing paths."""
    manifest_valid = _is_private_regular_file(manifest_path, expected_name="manifest.json")
    if not manifest_valid:
        return False, False, False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return False, False, False
    if not isinstance(payload, dict) or payload.get("all_restore_verified") is not True:
        return False, False, False

    fresh = False
    try:
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is not None and now.tzinfo is not None:
            age = now.astimezone(created_at.tzinfo) - created_at
            fresh = timedelta(minutes=-5) <= age <= max_backup_age
    except (KeyError, TypeError, ValueError, OverflowError):
        fresh = False

    databases = payload.get("databases")
    if not isinstance(databases, list):
        return False, False, fresh
    matches = [
        item
        for item in databases
        if isinstance(item, dict) and item.get("database") == database_name
    ]
    if len(matches) != 1:
        return False, False, fresh
    item = matches[0]
    if item.get("restore_verified") is not True or item.get("integrity") != "ok":
        return False, False, fresh

    backup_name = item.get("backup_file")
    if not isinstance(backup_name, str) or not backup_name or Path(backup_name).name != backup_name:
        return False, False, fresh
    backup_path = manifest_path.parent / backup_name
    if not _is_private_regular_file(backup_path, expected_name=backup_name):
        return True, False, fresh
    try:
        if _sha256(backup_path) != item.get("sha256"):
            return True, False, fresh
        verification = verify_database(backup_path)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return True, False, fresh
    database_valid = (
        verification.get("integrity") == "ok"
        and verification.get("restore_verified") is True
        and verification.get("schema_sha256") == item.get("schema_sha256")
        and verification.get("table_counts") == item.get("table_counts")
    )
    return True, database_valid, fresh


def assess_private_write_readiness(
    evidence: PrivateWriteReadinessEvidence,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PrivateWriteReadinessReport:
    """Assess a prospective cutover without changing files or runtime state."""
    checked_at = now or datetime.now().astimezone()
    if checked_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if max_backup_age <= timedelta(0):
        raise ValueError("max_backup_age must be positive")

    database_private = _is_private_regular_file(
        evidence.database_path,
        expected_name=(
            evidence.database_path.name
            if evidence.database_path.name in SUPPORTED_PRIVATE_WRITE_DATABASE_NAMES
            else ""
        ),
    )
    source_integrity = database_private and _source_integrity_ok(evidence.database_path)
    manifest_valid, backup_valid, backup_fresh = _backup_checks(
        evidence.backup_manifest_path,
        database_name=evidence.database_path.name,
        now=checked_at,
        max_backup_age=max_backup_age,
    )
    owners = tuple(owner.strip() for owner in evidence.writer_owners if owner.strip())
    single_writer = owners == (evidence.expected_writer_owner,)

    checks = (
        PrivateWriteReadinessCheck("api_writer_enabled", evidence.api_writer_enabled),
        PrivateWriteReadinessCheck("client_writes_enabled", evidence.client_writes_enabled),
        PrivateWriteReadinessCheck(
            "consumer_executor_enabled", evidence.consumer_executor_enabled
        ),
        PrivateWriteReadinessCheck("single_writer_owner", single_writer),
        PrivateWriteReadinessCheck("source_database_private", database_private),
        PrivateWriteReadinessCheck("source_database_integrity", source_integrity),
        PrivateWriteReadinessCheck("verified_backup_manifest", manifest_valid),
        PrivateWriteReadinessCheck("verified_backup_database", backup_valid),
        PrivateWriteReadinessCheck("backup_fresh", backup_fresh),
        PrivateWriteReadinessCheck(
            "user_approval_required", evidence.user_approval_required
        ),
        PrivateWriteReadinessCheck(
            "direct_db_fallback_disabled", evidence.direct_db_fallback_disabled
        ),
        PrivateWriteReadinessCheck("rollback_verified", evidence.rollback_verified),
    )
    return PrivateWriteReadinessReport(
        ready=all(check.ready for check in checks),
        checks=checks,
    )


def require_private_write_readiness(
    evidence: PrivateWriteReadinessEvidence,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PrivateWriteReadinessReport:
    report = assess_private_write_readiness(
        evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    report.require_ready()
    return report


def issue_private_write_activation_permit(
    evidence: PrivateWriteReadinessEvidence,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PrivateWriteActivationPermit:
    """Issue an opaque permit after the same evidence passes every check."""
    require_private_write_readiness(
        evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    database_path = evidence.database_path.resolve()
    fingerprint_payload = "\0".join(
        (
            "private-write-activation-v1",
            hashlib.sha256(_canonical_path_text(database_path).encode("utf-8")).hexdigest(),
            _sha256(evidence.backup_manifest_path),
            evidence.expected_writer_owner,
        )
    )
    fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
    return PrivateWriteActivationPermit(database_path, fingerprint, _PERMIT_SEAL)


def require_private_write_activation_permit(
    permit: object,
    *,
    database_path: Path | str | None = None,
    matching_permit: object | None = None,
) -> PrivateWriteActivationPermit:
    """Validate permit authenticity, database scope, and optional peer match."""
    if (
        type(permit) is not PrivateWriteActivationPermit
        or getattr(permit, "_seal", None) is not _PERMIT_SEAL
    ):
        raise PrivateWriteActivationError("validated activation permit is required")
    validated = permit
    if database_path is not None:
        try:
            candidate = Path(database_path).resolve()
        except (OSError, TypeError, ValueError) as exc:
            raise PrivateWriteActivationError("activation permit database scope mismatch") from exc
        if _canonical_path_text(candidate) != _canonical_path_text(validated._database_path):
            raise PrivateWriteActivationError("activation permit database scope mismatch")
    if matching_permit is not None:
        peer = require_private_write_activation_permit(matching_permit)
        if not hmac.compare_digest(validated.fingerprint, peer.fingerprint):
            raise PrivateWriteActivationError("activation permits do not match")
    return validated
