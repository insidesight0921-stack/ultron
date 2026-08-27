#!/usr/bin/env python3
"""Non-executable dry-run contract for a future Paper writer cutover."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from private_paper_write_readiness import (
    PAPER_API_OWNER,
    PAPER_TARGET_CALLER_POLICIES,
    PaperCallerPolicy,
    PaperWriteReadinessEvidence,
    PaperWriteReadinessReport,
    assess_paper_write_readiness,
)
from private_write_readiness import (
    DEFAULT_MAX_BACKUP_AGE,
    issue_private_write_activation_permit,
)


PAPER_LEGACY_RUNTIME_WRITERS = (
    "paper-ui-direct",
    "telegram-intraday-direct",
    "telegram-kium-direct",
    "telegram-quant-direct",
)
PAPER_OPERATOR_ROLLBACK_OWNER = "operator-cli-rollback-only"

PAPER_CUTOVER_STEPS = (
    "verify_fresh_paper_backup",
    "verify_isolated_paper_rollback_rehearsal",
    "install_atomic_paper_bundle",
    "restart_paper_ui_api_consumer_fail_closed",
    "restart_telegram_paper_api_consumer_fail_closed",
    "verify_all_direct_paper_runtime_writers_disabled",
    "restart_private_api_paper_writer",
    "verify_private_api_paper_writer_and_five_routes",
    "verify_paper_caller_approval_policies",
    "verify_no_direct_paper_db_fallback",
    "verify_paper_logical_consistency",
)

PAPER_ROLLBACK_STEPS = (
    "install_previous_paper_direct_bundle",
    "restart_private_api_paper_read_only",
    "verify_private_api_paper_writer_disabled",
    "restart_paper_ui_direct_writer",
    "restart_telegram_direct_paper_writers",
    "verify_operator_cli_remains_rollback_only",
    "verify_paper_logical_consistency",
)


class PaperWriteCutoverError(RuntimeError):
    def __init__(self, failed_codes: tuple[str, ...]) -> None:
        super().__init__("Paper write cutover dry-run failed: " + ", ".join(failed_codes))
        self.failed_codes = failed_codes


@dataclass(frozen=True)
class PaperWriteCutoverConfig:
    readiness_evidence: PaperWriteReadinessEvidence
    current_api_paper_writer_enabled: bool
    current_paper_client_enabled: bool
    current_paper_executor_enabled: bool
    current_runtime_writer_owners: tuple[str, ...]
    current_operator_cli_rollback_only: bool
    cutover_steps: tuple[str, ...] = PAPER_CUTOVER_STEPS
    rollback_steps: tuple[str, ...] = PAPER_ROLLBACK_STEPS
    automatic_backup_restore: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.readiness_evidence, PaperWriteReadinessEvidence):
            raise TypeError("readiness_evidence must use the Paper write contract")
        flags = (
            self.current_api_paper_writer_enabled,
            self.current_paper_client_enabled,
            self.current_paper_executor_enabled,
            self.current_operator_cli_rollback_only,
            self.automatic_backup_restore,
        )
        if any(type(value) is not bool for value in flags):
            raise TypeError("Paper cutover flags must be bool")
        tuple_fields = (
            self.current_runtime_writer_owners,
            self.cutover_steps,
            self.rollback_steps,
        )
        if any(
            not isinstance(values, tuple)
            or any(not isinstance(value, str) for value in values)
            for values in tuple_fields
        ):
            raise TypeError("Paper cutover fields must be tuples of strings")


@dataclass(frozen=True)
class PaperWriteCutoverCheck:
    code: str
    ready: bool


@dataclass(frozen=True)
class PaperWriteCutoverReport:
    ready: bool
    checks: tuple[PaperWriteCutoverCheck, ...]
    readiness: PaperWriteReadinessReport

    @property
    def failed_codes(self) -> tuple[str, ...]:
        readiness_codes = tuple(
            f"readiness:{code}" for code in self.readiness.failed_codes
        )
        cutover_codes = tuple(check.code for check in self.checks if not check.ready)
        return readiness_codes + cutover_codes

    def require_ready(self) -> None:
        if not self.ready:
            raise PaperWriteCutoverError(self.failed_codes)


@dataclass(frozen=True)
class PaperWriteCutoverDryRun:
    """Sanitized plan only; deliberately has no execute/apply capability."""

    bundle_id: str
    activation_fingerprint: str
    writer_owner: str
    caller_policies: tuple[PaperCallerPolicy, ...]
    cutover_steps: tuple[str, ...]
    rollback_steps: tuple[str, ...]
    dry_run: bool = True
    automatic_backup_restore: bool = False


def _normalized(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value.strip())


def assess_paper_write_cutover(
    config: PaperWriteCutoverConfig,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PaperWriteCutoverReport:
    readiness = assess_paper_write_readiness(
        config.readiness_evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    current_stack_disabled = not any(
        (
            config.current_api_paper_writer_enabled,
            config.current_paper_client_enabled,
            config.current_paper_executor_enabled,
        )
    )
    current_owners = _normalized(config.current_runtime_writer_owners)
    target_owners = _normalized(
        config.readiness_evidence.private_write_evidence.writer_owners
    )
    checks = (
        PaperWriteCutoverCheck(
            "current_paper_write_stack_disabled",
            current_stack_disabled,
        ),
        PaperWriteCutoverCheck(
            "current_legacy_runtime_writers_fixed",
            current_owners == PAPER_LEGACY_RUNTIME_WRITERS,
        ),
        PaperWriteCutoverCheck(
            "current_operator_cli_rollback_only",
            config.current_operator_cli_rollback_only,
        ),
        PaperWriteCutoverCheck(
            "target_single_private_api_writer",
            target_owners == (PAPER_API_OWNER,),
        ),
        PaperWriteCutoverCheck(
            "cutover_order_fixed",
            config.cutover_steps == PAPER_CUTOVER_STEPS,
        ),
        PaperWriteCutoverCheck(
            "rollback_order_fixed",
            config.rollback_steps == PAPER_ROLLBACK_STEPS,
        ),
        PaperWriteCutoverCheck(
            "automatic_backup_restore_disabled",
            not config.automatic_backup_restore,
        ),
    )
    return PaperWriteCutoverReport(
        ready=readiness.ready and all(check.ready for check in checks),
        checks=checks,
        readiness=readiness,
    )


def build_paper_write_cutover_dry_run(
    config: PaperWriteCutoverConfig,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PaperWriteCutoverDryRun:
    report = assess_paper_write_cutover(
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
    policy_payload = tuple(
        f"{policy.caller}:{','.join(policy.operations)}:{policy.approval_mode}"
        for policy in PAPER_TARGET_CALLER_POLICIES
    )
    bundle_payload = "\0".join(
        (
            "private-paper-write-cutover-v1",
            permit.fingerprint,
            PAPER_API_OWNER,
            *policy_payload,
            *config.cutover_steps,
            *config.rollback_steps,
        )
    )
    return PaperWriteCutoverDryRun(
        bundle_id=hashlib.sha256(bundle_payload.encode("utf-8")).hexdigest(),
        activation_fingerprint=permit.fingerprint,
        writer_owner=PAPER_API_OWNER,
        caller_policies=PAPER_TARGET_CALLER_POLICIES,
        cutover_steps=config.cutover_steps,
        rollback_steps=config.rollback_steps,
    )
