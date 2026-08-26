#!/usr/bin/env python3
"""Non-executable dry-run contract for a future Private watchlist cutover.

The module produces an immutable, sanitized plan.  It has no subprocess,
launchd, environment, network, backup creation, database mutation, or restore
capability.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from private_write_readiness import (
    DEFAULT_MAX_BACKUP_AGE,
    PrivateWriteReadinessEvidence,
    PrivateWriteReadinessReport,
    assess_private_write_readiness,
    issue_private_write_activation_permit,
)


LEGACY_WRITER_OWNER = "telegram-direct"
TARGET_WRITER_OWNER = "private-data-api"

CUTOVER_STEPS = (
    "verify_fresh_backup",
    "stop_telegram_direct_writer",
    "start_private_api_writer",
    "verify_private_api_writer",
    "start_telegram_api_consumer",
    "verify_no_direct_db_fallback",
)

ROLLBACK_STEPS = (
    "stop_telegram_api_consumer",
    "stop_private_api_writer",
    "start_private_api_read_only",
    "start_telegram_direct_writer",
    "verify_watchlist_consistency",
)


class PrivateWriteCutoverError(RuntimeError):
    def __init__(self, failed_codes: tuple[str, ...]) -> None:
        super().__init__("Private write cutover dry-run failed: " + ", ".join(failed_codes))
        self.failed_codes = failed_codes


@dataclass(frozen=True)
class PrivateWriteCutoverConfig:
    """Current baseline plus immutable intended service order."""

    readiness_evidence: PrivateWriteReadinessEvidence
    current_api_writer_enabled: bool
    current_client_writes_enabled: bool
    current_consumer_executor_enabled: bool
    current_writer_owners: tuple[str, ...]
    cutover_steps: tuple[str, ...] = CUTOVER_STEPS
    rollback_steps: tuple[str, ...] = ROLLBACK_STEPS
    automatic_backup_restore: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.readiness_evidence, PrivateWriteReadinessEvidence):
            raise TypeError("readiness_evidence must use the Private write contract")
        flags = (
            self.current_api_writer_enabled,
            self.current_client_writes_enabled,
            self.current_consumer_executor_enabled,
            self.automatic_backup_restore,
        )
        if any(type(value) is not bool for value in flags):
            raise TypeError("cutover flags must be bool")
        tuple_fields = (
            self.current_writer_owners,
            self.cutover_steps,
            self.rollback_steps,
        )
        if any(
            not isinstance(values, tuple)
            or any(not isinstance(value, str) for value in values)
            for values in tuple_fields
        ):
            raise TypeError("cutover owners and steps must be tuples of strings")


@dataclass(frozen=True)
class PrivateWriteCutoverCheck:
    code: str
    ready: bool


@dataclass(frozen=True)
class PrivateWriteCutoverReport:
    ready: bool
    checks: tuple[PrivateWriteCutoverCheck, ...]
    readiness: PrivateWriteReadinessReport

    @property
    def failed_codes(self) -> tuple[str, ...]:
        readiness_codes = tuple(
            f"readiness:{code}" for code in self.readiness.failed_codes
        )
        cutover_codes = tuple(check.code for check in self.checks if not check.ready)
        return readiness_codes + cutover_codes

    def require_ready(self) -> None:
        if not self.ready:
            raise PrivateWriteCutoverError(self.failed_codes)


@dataclass(frozen=True)
class PrivateWriteCutoverDryRun:
    """Sanitized plan only; deliberately has no execute/apply method."""

    bundle_id: str
    activation_fingerprint: str
    cutover_steps: tuple[str, ...]
    rollback_steps: tuple[str, ...]
    dry_run: bool = True
    automatic_backup_restore: bool = False


def _normalized_owners(owners: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(owner.strip() for owner in owners if owner.strip())


def assess_private_write_cutover(
    config: PrivateWriteCutoverConfig,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PrivateWriteCutoverReport:
    """Assess desired cutover and current inactive baseline without side effects."""
    readiness = assess_private_write_readiness(
        config.readiness_evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    baseline_disabled = not any(
        (
            config.current_api_writer_enabled,
            config.current_client_writes_enabled,
            config.current_consumer_executor_enabled,
        )
    )
    current_owners = _normalized_owners(config.current_writer_owners)
    legacy_owner_safe = current_owners in {(), (LEGACY_WRITER_OWNER,)}
    target_owner_safe = _normalized_owners(
        config.readiness_evidence.writer_owners
    ) == (TARGET_WRITER_OWNER,)
    checks = (
        PrivateWriteCutoverCheck("current_write_stack_disabled", baseline_disabled),
        PrivateWriteCutoverCheck("current_single_legacy_writer", legacy_owner_safe),
        PrivateWriteCutoverCheck("target_single_private_api_writer", target_owner_safe),
        PrivateWriteCutoverCheck(
            "cutover_order_fixed", config.cutover_steps == CUTOVER_STEPS
        ),
        PrivateWriteCutoverCheck(
            "rollback_order_fixed", config.rollback_steps == ROLLBACK_STEPS
        ),
        PrivateWriteCutoverCheck(
            "automatic_backup_restore_disabled", not config.automatic_backup_restore
        ),
    )
    return PrivateWriteCutoverReport(
        ready=readiness.ready and all(check.ready for check in checks),
        checks=checks,
        readiness=readiness,
    )


def build_private_write_cutover_dry_run(
    config: PrivateWriteCutoverConfig,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PrivateWriteCutoverDryRun:
    """Build a permit-bound plan only when every dry-run check passes."""
    report = assess_private_write_cutover(
        config,
        now=now,
        max_backup_age=max_backup_age,
    )
    report.require_ready()
    permit = issue_private_write_activation_permit(
        config.readiness_evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    bundle_payload = "\0".join(
        (
            "private-write-cutover-v1",
            permit.fingerprint,
            *config.cutover_steps,
            *config.rollback_steps,
        )
    )
    return PrivateWriteCutoverDryRun(
        bundle_id=hashlib.sha256(bundle_payload.encode("utf-8")).hexdigest(),
        activation_fingerprint=permit.fingerprint,
        cutover_steps=config.cutover_steps,
        rollback_steps=config.rollback_steps,
    )
