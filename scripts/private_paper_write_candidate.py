#!/usr/bin/env python3
"""Generate a non-installed v3 watchlist+schedule+Paper activation candidate."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from private_paper_write_cutover import (
    PAPER_LEGACY_RUNTIME_WRITERS,
    PaperWriteCutoverConfig,
    build_paper_write_cutover_dry_run,
)
from private_paper_write_readiness import PaperWriteReadinessEvidence
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
    PAPER_BUNDLE_VERSION,
    _combined_bundle_id,
    _paper_combined_bundle_id,
    _paper_policy_payload,
    load_private_write_runtime_bundle,
)

PAPER_RUNTIME_ROLES = ("private-data-api", "paper-ui", "telegram")


class PaperWriteCandidateError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Paper write activation candidate rejected: {code}")
        self.code = code


@dataclass(frozen=True)
class PaperWriteActivationCandidate:
    bundle_id: str
    assistant_activation_fingerprint: str
    paper_activation_fingerprint: str
    schedule_enabled: bool
    paper_enabled: bool
    runtime_roles_validated: tuple[str, ...]
    installed: bool = False
    _path: Path = field(repr=False, default=Path())

    @property
    def path(self) -> Path:
        return self._path


def generate_paper_write_activation_candidate(
    assistant_evidence: ScheduleWriteReadinessEvidence,
    paper_evidence: PaperWriteReadinessEvidence,
    staging_dir: Path,
    *,
    current_watchlist_writer_owner: str = LEGACY_WRITER_OWNER,
    current_schedule_user_writer_owner: str = LEGACY_SCHEDULE_USER_WRITER,
    current_paper_runtime_writer_owners: tuple[str, ...] = PAPER_LEGACY_RUNTIME_WRITERS,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PaperWriteActivationCandidate:
    """Create and cross-process validate one v3 bundle without installing it."""
    if not isinstance(assistant_evidence, ScheduleWriteReadinessEvidence):
        raise TypeError("assistant_evidence must use the schedule write contract")
    if not isinstance(paper_evidence, PaperWriteReadinessEvidence):
        raise TypeError("paper_evidence must use the Paper write contract")
    assistant_private = assistant_evidence.private_write_evidence
    paper_private = paper_evidence.private_write_evidence
    assistant_database = assistant_private.database_path.resolve()
    paper_database = paper_private.database_path.resolve()
    if assistant_database == paper_database:
        raise PaperWriteCandidateError("paper_database_not_separate")
    if (
        assistant_private.backup_manifest_path.resolve()
        != paper_private.backup_manifest_path.resolve()
    ):
        raise PaperWriteCandidateError("backup_manifest_not_shared")
    try:
        staging = _require_empty_staging(
            Path(staging_dir).resolve(), assistant_database
        )
        _require_empty_staging(staging, paper_database)
    except PrivateWriteCandidateError as exc:
        raise PaperWriteCandidateError(exc.code) from exc

    watchlist_owner = str(current_watchlist_writer_owner or "").strip()
    if watchlist_owner not in {"", LEGACY_WRITER_OWNER}:
        raise PaperWriteCandidateError("invalid_current_watchlist_writer_owner")
    schedule_owner = str(current_schedule_user_writer_owner or "").strip()
    if schedule_owner != LEGACY_SCHEDULE_USER_WRITER:
        raise PaperWriteCandidateError("invalid_current_schedule_writer_owner")
    paper_owners = tuple(current_paper_runtime_writer_owners)
    if paper_owners != PAPER_LEGACY_RUNTIME_WRITERS:
        raise PaperWriteCandidateError("invalid_current_paper_writer_owners")

    watchlist_plan = build_private_write_cutover_dry_run(
        PrivateWriteCutoverConfig(
            readiness_evidence=assistant_private,
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
            readiness_evidence=assistant_evidence,
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
    paper_plan = build_paper_write_cutover_dry_run(
        PaperWriteCutoverConfig(
            readiness_evidence=paper_evidence,
            current_api_paper_writer_enabled=False,
            current_paper_client_enabled=False,
            current_paper_executor_enabled=False,
            current_runtime_writer_owners=paper_owners,
            current_operator_cli_rollback_only=True,
        ),
        now=now,
        max_backup_age=max_backup_age,
    )
    assistant_bundle_id = _combined_bundle_id(
        watchlist_plan.bundle_id, schedule_plan.bundle_id
    )
    bundle_id = _paper_combined_bundle_id(assistant_bundle_id, paper_plan.bundle_id)
    payload: dict[str, object] = {
        "version": PAPER_BUNDLE_VERSION,
        "enabled": True,
        "bundle_id": bundle_id,
        "api_writer_enabled": True,
        "client_writes_enabled": True,
        "consumer_executor_enabled": True,
        "writer_owner": "private-data-api",
        "current_writer_owner": watchlist_owner,
        "backup_manifest": str(assistant_private.backup_manifest_path.resolve()),
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
        "paper": {
            "enabled": True,
            "api_writer_enabled": True,
            "client_writes_enabled": True,
            "consumer_executor_enabled": True,
            "writer_owner": "private-data-api",
            "current_runtime_writer_owners": list(paper_owners),
            "current_operator_cli_rollback_only": True,
            "caller_policies": _paper_policy_payload(),
            "consumers_use_private_api": True,
            "consumers_use_same_database": True,
            "paper_ui_direct_writes_disabled": True,
            "telegram_direct_writes_disabled": True,
            "operator_cli_rollback_only": True,
        },
    }
    candidate_path = staging / BUNDLE_FILENAME
    try:
        _write_exclusive_json(candidate_path, payload)
    except PrivateWriteCandidateError as exc:
        raise PaperWriteCandidateError(exc.code) from exc

    runtimes = tuple(
        load_private_write_runtime_bundle(
            {BUNDLE_PATH_ENV: str(candidate_path)},
            database_path=assistant_database,
            paper_database_path=paper_database,
            private_root=staging,
            backup_root=assistant_private.backup_manifest_path.parent.parent,
            now=now,
            max_backup_age=max_backup_age,
        )
        for _role in PAPER_RUNTIME_ROLES
    )
    if any(
        runtime is None
        or not runtime.schedule_writes_enabled
        or not runtime.paper_writes_enabled
        for runtime in runtimes
    ):
        raise PaperWriteCandidateError("runtime_capability_validation_missing")
    runtime_ids = {runtime.bundle_id for runtime in runtimes if runtime is not None}
    assistant_fingerprints = {
        runtime.activation_fingerprint for runtime in runtimes if runtime is not None
    }
    paper_fingerprints = {
        runtime.paper_activation_permit.fingerprint
        for runtime in runtimes
        if runtime is not None
    }
    if (
        runtime_ids != {bundle_id}
        or assistant_fingerprints != {schedule_plan.activation_fingerprint}
        or paper_fingerprints != {paper_plan.activation_fingerprint}
    ):
        raise PaperWriteCandidateError("cross_process_runtime_validation_mismatch")
    return PaperWriteActivationCandidate(
        bundle_id=bundle_id,
        assistant_activation_fingerprint=schedule_plan.activation_fingerprint,
        paper_activation_fingerprint=paper_plan.activation_fingerprint,
        schedule_enabled=True,
        paper_enabled=True,
        runtime_roles_validated=PAPER_RUNTIME_ROLES,
        _path=candidate_path,
    )
