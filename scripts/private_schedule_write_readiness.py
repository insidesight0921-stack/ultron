#!/usr/bin/env python3
"""Side-effect-free readiness contract for a future schedule write cutover."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from private_schedule_write_contract import SCHEDULE_WRITE_OPERATIONS
from private_write_readiness import (
    DEFAULT_MAX_BACKUP_AGE,
    PrivateWriteReadinessEvidence,
    PrivateWriteReadinessReport,
    assess_private_write_readiness,
)


SCHEDULE_API_OWNER = "private-data-api"
SCHEDULE_NOTIFIER_OWNER = "telegram-schedule-notifier"
SCHEDULE_NOTIFIER_OPERATIONS = (
    "schedule.mark_notified",
    "schedule.mark_pre_notified",
    "schedule.advance_recurring",
)


class ScheduleWriteReadinessError(RuntimeError):
    def __init__(self, failed_codes: tuple[str, ...]) -> None:
        super().__init__("Schedule write readiness failed: " + ", ".join(failed_codes))
        self.failed_codes = failed_codes


@dataclass(frozen=True)
class ScheduleWriteReadinessEvidence:
    """Target user-mutation stack plus retained notifier ownership evidence."""

    private_write_evidence: PrivateWriteReadinessEvidence
    notifier_enabled: bool
    notifier_writer_owners: tuple[str, ...]
    notifier_operations: tuple[str, ...]
    notifier_uses_same_database: bool
    telegram_direct_user_mutations_disabled: bool
    notifier_user_mutations_disabled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.private_write_evidence, PrivateWriteReadinessEvidence):
            raise TypeError("private_write_evidence must use the Private write contract")
        flags = (
            self.notifier_enabled,
            self.notifier_uses_same_database,
            self.telegram_direct_user_mutations_disabled,
            self.notifier_user_mutations_disabled,
        )
        if any(type(value) is not bool for value in flags):
            raise TypeError("schedule readiness flags must be bool")
        tuple_fields = (self.notifier_writer_owners, self.notifier_operations)
        if any(
            not isinstance(values, tuple)
            or any(not isinstance(value, str) for value in values)
            for values in tuple_fields
        ):
            raise TypeError("schedule owners and operations must be tuples of strings")


@dataclass(frozen=True)
class ScheduleWriteReadinessCheck:
    code: str
    ready: bool


@dataclass(frozen=True)
class ScheduleWriteReadinessReport:
    ready: bool
    checks: tuple[ScheduleWriteReadinessCheck, ...]
    private_write: PrivateWriteReadinessReport

    @property
    def failed_codes(self) -> tuple[str, ...]:
        base_codes = tuple(
            f"private:{code}" for code in self.private_write.failed_codes
        )
        schedule_codes = tuple(check.code for check in self.checks if not check.ready)
        return base_codes + schedule_codes

    def require_ready(self) -> None:
        if not self.ready:
            raise ScheduleWriteReadinessError(self.failed_codes)


def _normalized(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value.strip())


def assess_schedule_write_readiness(
    evidence: ScheduleWriteReadinessEvidence,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> ScheduleWriteReadinessReport:
    """Assess disjoint user and notifier writers without changing runtime state."""
    private_write = assess_private_write_readiness(
        evidence.private_write_evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    user_owners = _normalized(evidence.private_write_evidence.writer_owners)
    notifier_owners = _normalized(evidence.notifier_writer_owners)
    notifier_operations = _normalized(evidence.notifier_operations)
    operation_sets_disjoint = not (
        set(SCHEDULE_WRITE_OPERATIONS) & set(notifier_operations)
    )
    checks = (
        ScheduleWriteReadinessCheck(
            "user_mutation_owner_private_api",
            user_owners == (SCHEDULE_API_OWNER,),
        ),
        ScheduleWriteReadinessCheck("notifier_enabled", evidence.notifier_enabled),
        ScheduleWriteReadinessCheck(
            "single_notifier_owner",
            notifier_owners == (SCHEDULE_NOTIFIER_OWNER,),
        ),
        ScheduleWriteReadinessCheck(
            "notifier_operations_fixed",
            notifier_operations == SCHEDULE_NOTIFIER_OPERATIONS,
        ),
        ScheduleWriteReadinessCheck(
            "writer_operations_disjoint",
            operation_sets_disjoint,
        ),
        ScheduleWriteReadinessCheck(
            "notifier_same_database",
            evidence.notifier_uses_same_database,
        ),
        ScheduleWriteReadinessCheck(
            "telegram_direct_user_mutations_disabled",
            evidence.telegram_direct_user_mutations_disabled,
        ),
        ScheduleWriteReadinessCheck(
            "notifier_user_mutations_disabled",
            evidence.notifier_user_mutations_disabled,
        ),
    )
    return ScheduleWriteReadinessReport(
        ready=private_write.ready and all(check.ready for check in checks),
        checks=checks,
        private_write=private_write,
    )


def require_schedule_write_readiness(
    evidence: ScheduleWriteReadinessEvidence,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> ScheduleWriteReadinessReport:
    report = assess_schedule_write_readiness(
        evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    report.require_ready()
    return report
