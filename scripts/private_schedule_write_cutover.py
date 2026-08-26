#!/usr/bin/env python3
"""Non-executable dry-run for schedule user-mutation writer cutover."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from private_schedule_write_readiness import (
    SCHEDULE_API_OWNER,
    SCHEDULE_NOTIFIER_OPERATIONS,
    SCHEDULE_NOTIFIER_OWNER,
    ScheduleWriteReadinessEvidence,
    ScheduleWriteReadinessReport,
    assess_schedule_write_readiness,
)
from private_write_readiness import (
    DEFAULT_MAX_BACKUP_AGE,
    issue_private_write_activation_permit,
)


LEGACY_SCHEDULE_USER_WRITER = "telegram-schedule-direct"

SCHEDULE_CUTOVER_STEPS = (
    "verify_fresh_assistant_backup",
    "install_atomic_schedule_bundle",
    "restart_telegram_schedule_api_consumer_fail_closed",
    "verify_telegram_direct_schedule_writer_disabled",
    "verify_notifier_internal_writer_resumed",
    "restart_private_api_schedule_writer",
    "verify_private_api_schedule_writer",
    "verify_no_direct_schedule_user_fallback",
    "verify_schedule_and_notification_consistency",
)

SCHEDULE_ROLLBACK_STEPS = (
    "install_previous_watchlist_only_bundle",
    "restart_private_api_schedule_read_only",
    "verify_private_api_schedule_writer_disabled",
    "restart_telegram_schedule_direct_user_writer",
    "verify_notifier_internal_writer_resumed",
    "verify_schedule_and_notification_consistency",
)


class ScheduleWriteCutoverError(RuntimeError):
    def __init__(self, failed_codes: tuple[str, ...]) -> None:
        super().__init__("Schedule write cutover dry-run failed: " + ", ".join(failed_codes))
        self.failed_codes = failed_codes


@dataclass(frozen=True)
class ScheduleWriteCutoverConfig:
    readiness_evidence: ScheduleWriteReadinessEvidence
    current_api_schedule_writer_enabled: bool
    current_schedule_client_enabled: bool
    current_schedule_executor_enabled: bool
    current_user_mutation_writer_owners: tuple[str, ...]
    current_notifier_enabled: bool
    current_notifier_writer_owners: tuple[str, ...]
    current_notifier_operations: tuple[str, ...]
    cutover_steps: tuple[str, ...] = SCHEDULE_CUTOVER_STEPS
    rollback_steps: tuple[str, ...] = SCHEDULE_ROLLBACK_STEPS
    automatic_backup_restore: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.readiness_evidence, ScheduleWriteReadinessEvidence):
            raise TypeError("readiness_evidence must use the schedule write contract")
        flags = (
            self.current_api_schedule_writer_enabled,
            self.current_schedule_client_enabled,
            self.current_schedule_executor_enabled,
            self.current_notifier_enabled,
            self.automatic_backup_restore,
        )
        if any(type(value) is not bool for value in flags):
            raise TypeError("schedule cutover flags must be bool")
        tuple_fields = (
            self.current_user_mutation_writer_owners,
            self.current_notifier_writer_owners,
            self.current_notifier_operations,
            self.cutover_steps,
            self.rollback_steps,
        )
        if any(
            not isinstance(values, tuple)
            or any(not isinstance(value, str) for value in values)
            for values in tuple_fields
        ):
            raise TypeError("schedule cutover fields must be tuples of strings")


@dataclass(frozen=True)
class ScheduleWriteCutoverCheck:
    code: str
    ready: bool


@dataclass(frozen=True)
class ScheduleWriteCutoverReport:
    ready: bool
    checks: tuple[ScheduleWriteCutoverCheck, ...]
    readiness: ScheduleWriteReadinessReport

    @property
    def failed_codes(self) -> tuple[str, ...]:
        readiness_codes = tuple(
            f"readiness:{code}" for code in self.readiness.failed_codes
        )
        cutover_codes = tuple(check.code for check in self.checks if not check.ready)
        return readiness_codes + cutover_codes

    def require_ready(self) -> None:
        if not self.ready:
            raise ScheduleWriteCutoverError(self.failed_codes)


@dataclass(frozen=True)
class ScheduleWriteCutoverDryRun:
    """Sanitized plan only; deliberately has no execute/apply method."""

    bundle_id: str
    activation_fingerprint: str
    user_mutation_owner: str
    notifier_owner: str
    notifier_operations: tuple[str, ...]
    cutover_steps: tuple[str, ...]
    rollback_steps: tuple[str, ...]
    dry_run: bool = True
    automatic_backup_restore: bool = False


def _normalized(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value.strip())


def assess_schedule_write_cutover(
    config: ScheduleWriteCutoverConfig,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> ScheduleWriteCutoverReport:
    readiness = assess_schedule_write_readiness(
        config.readiness_evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    schedule_stack_disabled = not any(
        (
            config.current_api_schedule_writer_enabled,
            config.current_schedule_client_enabled,
            config.current_schedule_executor_enabled,
        )
    )
    current_user_owners = _normalized(config.current_user_mutation_writer_owners)
    current_notifier_owners = _normalized(config.current_notifier_writer_owners)
    current_notifier_operations = _normalized(config.current_notifier_operations)
    target_user_owners = _normalized(
        config.readiness_evidence.private_write_evidence.writer_owners
    )
    checks = (
        ScheduleWriteCutoverCheck(
            "current_schedule_write_stack_disabled",
            schedule_stack_disabled,
        ),
        ScheduleWriteCutoverCheck(
            "current_single_legacy_user_writer",
            current_user_owners == (LEGACY_SCHEDULE_USER_WRITER,),
        ),
        ScheduleWriteCutoverCheck(
            "current_notifier_preserved",
            config.current_notifier_enabled
            and current_notifier_owners == (SCHEDULE_NOTIFIER_OWNER,)
            and current_notifier_operations == SCHEDULE_NOTIFIER_OPERATIONS,
        ),
        ScheduleWriteCutoverCheck(
            "target_private_api_user_writer",
            target_user_owners == (SCHEDULE_API_OWNER,),
        ),
        ScheduleWriteCutoverCheck(
            "target_notifier_preserved",
            config.readiness_evidence.notifier_enabled
            and _normalized(config.readiness_evidence.notifier_writer_owners)
            == (SCHEDULE_NOTIFIER_OWNER,)
            and _normalized(config.readiness_evidence.notifier_operations)
            == SCHEDULE_NOTIFIER_OPERATIONS,
        ),
        ScheduleWriteCutoverCheck(
            "cutover_order_fixed",
            config.cutover_steps == SCHEDULE_CUTOVER_STEPS,
        ),
        ScheduleWriteCutoverCheck(
            "rollback_order_fixed",
            config.rollback_steps == SCHEDULE_ROLLBACK_STEPS,
        ),
        ScheduleWriteCutoverCheck(
            "automatic_backup_restore_disabled",
            not config.automatic_backup_restore,
        ),
    )
    return ScheduleWriteCutoverReport(
        ready=readiness.ready and all(check.ready for check in checks),
        checks=checks,
        readiness=readiness,
    )


def build_schedule_write_cutover_dry_run(
    config: ScheduleWriteCutoverConfig,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> ScheduleWriteCutoverDryRun:
    report = assess_schedule_write_cutover(
        config,
        now=now,
        max_backup_age=max_backup_age,
    )
    report.require_ready()
    permit = issue_private_write_activation_permit(
        config.readiness_evidence.private_write_evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    bundle_payload = "\0".join(
        (
            "private-schedule-write-cutover-v1",
            permit.fingerprint,
            SCHEDULE_API_OWNER,
            SCHEDULE_NOTIFIER_OWNER,
            *SCHEDULE_NOTIFIER_OPERATIONS,
            *config.cutover_steps,
            *config.rollback_steps,
        )
    )
    return ScheduleWriteCutoverDryRun(
        bundle_id=hashlib.sha256(bundle_payload.encode("utf-8")).hexdigest(),
        activation_fingerprint=permit.fingerprint,
        user_mutation_owner=SCHEDULE_API_OWNER,
        notifier_owner=SCHEDULE_NOTIFIER_OWNER,
        notifier_operations=SCHEDULE_NOTIFIER_OPERATIONS,
        cutover_steps=config.cutover_steps,
        rollback_steps=config.rollback_steps,
    )
