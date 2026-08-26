#!/usr/bin/env python3
"""Generate a non-installed v2 schedule write activation candidate."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from private_schedule_write_cutover import (
    LEGACY_SCHEDULE_USER_WRITER,
    ScheduleWriteCutoverConfig,
    build_schedule_write_cutover_dry_run,
)
from private_schedule_write_readiness import (
    SCHEDULE_NOTIFIER_OPERATIONS,
    SCHEDULE_NOTIFIER_OWNER,
    ScheduleWriteReadinessEvidence,
)
from private_write_candidate import (
    PrivateWriteCandidateError,
    _require_empty_staging,
    _write_exclusive_json,
)
from private_write_cutover import (
    LEGACY_WRITER_OWNER,
    PrivateWriteCutoverConfig,
    build_private_write_cutover_dry_run,
)
from private_write_readiness import DEFAULT_MAX_BACKUP_AGE
from private_write_runtime import (
    BUNDLE_FILENAME,
    BUNDLE_PATH_ENV,
    BUNDLE_VERSION,
    _combined_bundle_id,
    load_private_write_runtime_bundle,
)


class ScheduleWriteCandidateError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Schedule write activation candidate rejected: {code}")
        self.code = code


@dataclass(frozen=True)
class ScheduleWriteActivationCandidate:
    bundle_id: str
    activation_fingerprint: str
    schedule_enabled: bool
    notifier_owner: str
    runtime_validated: bool
    installed: bool = False
    _path: Path = field(repr=False, default=Path())

    @property
    def path(self) -> Path:
        return self._path


def generate_schedule_write_activation_candidate(
    evidence: ScheduleWriteReadinessEvidence,
    staging_dir: Path,
    *,
    current_watchlist_writer_owner: str = LEGACY_WRITER_OWNER,
    current_schedule_user_writer_owner: str = LEGACY_SCHEDULE_USER_WRITER,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> ScheduleWriteActivationCandidate:
    """Create and validate a combined watchlist+schedule bundle without installing it."""
    if not isinstance(evidence, ScheduleWriteReadinessEvidence):
        raise TypeError("evidence must use the schedule write readiness contract")
    private = evidence.private_write_evidence
    database = private.database_path.resolve()
    try:
        staging = _require_empty_staging(Path(staging_dir).resolve(), database)
    except PrivateWriteCandidateError as exc:
        raise ScheduleWriteCandidateError(exc.code) from exc
    watchlist_owner = str(current_watchlist_writer_owner or "").strip()
    if watchlist_owner not in {"", LEGACY_WRITER_OWNER}:
        raise ScheduleWriteCandidateError("invalid_current_watchlist_writer_owner")
    schedule_owner = str(current_schedule_user_writer_owner or "").strip()
    if schedule_owner != LEGACY_SCHEDULE_USER_WRITER:
        raise ScheduleWriteCandidateError("invalid_current_schedule_writer_owner")

    watchlist_plan = build_private_write_cutover_dry_run(
        PrivateWriteCutoverConfig(
            readiness_evidence=private,
            current_api_writer_enabled=False,
            current_client_writes_enabled=False,
            current_consumer_executor_enabled=False,
            current_writer_owners=(watchlist_owner,) if watchlist_owner else (),
        ),
        now=now,
        max_backup_age=max_backup_age,
    )
    schedule_plan = build_schedule_write_cutover_dry_run(
        ScheduleWriteCutoverConfig(
            readiness_evidence=evidence,
            current_api_schedule_writer_enabled=False,
            current_schedule_client_enabled=False,
            current_schedule_executor_enabled=False,
            current_user_mutation_writer_owners=(schedule_owner,),
            current_notifier_enabled=True,
            current_notifier_writer_owners=(SCHEDULE_NOTIFIER_OWNER,),
            current_notifier_operations=SCHEDULE_NOTIFIER_OPERATIONS,
        ),
        now=now,
        max_backup_age=max_backup_age,
    )
    bundle_id = _combined_bundle_id(
        watchlist_plan.bundle_id,
        schedule_plan.bundle_id,
    )
    payload: dict[str, object] = {
        "version": BUNDLE_VERSION,
        "enabled": True,
        "bundle_id": bundle_id,
        "api_writer_enabled": True,
        "client_writes_enabled": True,
        "consumer_executor_enabled": True,
        "writer_owner": "private-data-api",
        "current_writer_owner": watchlist_owner,
        "backup_manifest": str(private.backup_manifest_path.resolve()),
        "user_approval_required": True,
        "direct_db_fallback_disabled": True,
        "rollback_verified": True,
        "automatic_backup_restore": False,
        "schedule": {
            "enabled": True,
            "api_writer_enabled": True,
            "client_writes_enabled": True,
            "consumer_executor_enabled": True,
            "user_writer_owner": "private-data-api",
            "current_user_writer_owner": schedule_owner,
            "notifier_enabled": True,
            "notifier_writer_owner": SCHEDULE_NOTIFIER_OWNER,
            "notifier_operations": list(SCHEDULE_NOTIFIER_OPERATIONS),
            "notifier_uses_same_database": True,
            "telegram_direct_user_mutations_disabled": True,
            "notifier_user_mutations_disabled": True,
        },
    }
    candidate_path = staging / BUNDLE_FILENAME
    try:
        _write_exclusive_json(candidate_path, payload)
    except PrivateWriteCandidateError as exc:
        raise ScheduleWriteCandidateError(exc.code) from exc

    runtime = load_private_write_runtime_bundle(
        {BUNDLE_PATH_ENV: str(candidate_path)},
        database_path=database,
        private_root=staging,
        backup_root=private.backup_manifest_path.parent.parent,
        now=now,
        max_backup_age=max_backup_age,
    )
    if runtime is None or not runtime.schedule_writes_enabled:
        raise ScheduleWriteCandidateError("runtime_schedule_validation_missing")
    if (
        runtime.bundle_id != bundle_id
        or runtime.activation_fingerprint != schedule_plan.activation_fingerprint
    ):
        raise ScheduleWriteCandidateError("runtime_validation_mismatch")
    return ScheduleWriteActivationCandidate(
        bundle_id=runtime.bundle_id,
        activation_fingerprint=runtime.activation_fingerprint,
        schedule_enabled=True,
        notifier_owner=SCHEDULE_NOTIFIER_OWNER,
        runtime_validated=True,
        _path=candidate_path,
    )
