#!/usr/bin/env python3
"""Generate and verify a non-installed Private write activation candidate.

The candidate is written exclusively to an explicitly supplied empty staging
directory that must differ from the operational Private data root.  This
module has no CLI, environment editing, service control, installation, or
cleanup capability.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from private_write_cutover import (
    LEGACY_WRITER_OWNER,
    PrivateWriteCutoverConfig,
    build_private_write_cutover_dry_run,
)
from private_write_readiness import (
    DEFAULT_MAX_BACKUP_AGE,
    PrivateWriteReadinessEvidence,
)
from private_write_runtime import (
    BUNDLE_FILENAME,
    BUNDLE_PATH_ENV,
    BUNDLE_VERSION,
    load_private_write_runtime_bundle,
)


class PrivateWriteCandidateError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Private write activation candidate rejected: {code}")
        self.code = code


@dataclass(frozen=True)
class PrivateWriteActivationCandidate:
    bundle_id: str
    activation_fingerprint: str
    runtime_validated: bool
    installed: bool = False
    _path: Path = field(repr=False, default=Path())

    @property
    def path(self) -> Path:
        return self._path


def _require_empty_staging(staging_dir: Path, database_path: Path) -> Path:
    try:
        valid = (
            staging_dir.is_absolute()
            and staging_dir.resolve() != database_path.parent.resolve()
            and not staging_dir.is_symlink()
            and staging_dir.is_dir()
            and staging_dir.stat().st_mode & 0o077 == 0
            and not any(staging_dir.iterdir())
        )
    except OSError:
        valid = False
    if not valid:
        raise PrivateWriteCandidateError("invalid_noninstall_staging")
    return staging_dir


def _write_exclusive_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PrivateWriteCandidateError("candidate_already_exists") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
    except Exception:
        # Do not delete or overwrite a partially written artifact automatically.
        raise


def generate_private_write_activation_candidate(
    evidence: PrivateWriteReadinessEvidence,
    staging_dir: Path,
    *,
    current_writer_owner: str = LEGACY_WRITER_OWNER,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PrivateWriteActivationCandidate:
    """Create one validated candidate without installing or activating it."""
    if not isinstance(evidence, PrivateWriteReadinessEvidence):
        raise TypeError("evidence must use the Private write readiness contract")
    database = evidence.database_path.resolve()
    staging = _require_empty_staging(Path(staging_dir).resolve(), database)
    owner = str(current_writer_owner or "").strip()
    if owner not in {"", LEGACY_WRITER_OWNER}:
        raise PrivateWriteCandidateError("invalid_current_writer_owner")
    plan = build_private_write_cutover_dry_run(
        PrivateWriteCutoverConfig(
            readiness_evidence=evidence,
            current_api_writer_enabled=False,
            current_client_writes_enabled=False,
            current_consumer_executor_enabled=False,
            current_writer_owners=(owner,) if owner else (),
        ),
        now=now,
        max_backup_age=max_backup_age,
    )
    payload: dict[str, object] = {
        "version": BUNDLE_VERSION,
        "enabled": True,
        "bundle_id": plan.bundle_id,
        "api_writer_enabled": True,
        "client_writes_enabled": True,
        "consumer_executor_enabled": True,
        "writer_owner": "private-data-api",
        "current_writer_owner": owner,
        "backup_manifest": str(evidence.backup_manifest_path.resolve()),
        "user_approval_required": True,
        "direct_db_fallback_disabled": True,
        "rollback_verified": True,
        "automatic_backup_restore": False,
        "schedule": {"enabled": False},
    }
    candidate_path = staging / BUNDLE_FILENAME
    _write_exclusive_json(candidate_path, payload)

    runtime = load_private_write_runtime_bundle(
        {BUNDLE_PATH_ENV: str(candidate_path)},
        database_path=database,
        private_root=staging,
        backup_root=evidence.backup_manifest_path.parent.parent,
        now=now,
        max_backup_age=max_backup_age,
    )
    if runtime is None:
        raise PrivateWriteCandidateError("runtime_validation_missing")
    if (
        runtime.bundle_id != plan.bundle_id
        or runtime.activation_fingerprint != plan.activation_fingerprint
    ):
        raise PrivateWriteCandidateError("runtime_validation_mismatch")
    return PrivateWriteActivationCandidate(
        bundle_id=runtime.bundle_id,
        activation_fingerprint=runtime.activation_fingerprint,
        runtime_validated=True,
        _path=candidate_path,
    )
