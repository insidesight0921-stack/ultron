#!/usr/bin/env python3
"""Side-effect-free readiness contract for a future Paper write cutover."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from private_write_readiness import (
    DEFAULT_MAX_BACKUP_AGE,
    PrivateWriteReadinessEvidence,
    PrivateWriteReadinessReport,
    assess_private_write_readiness,
)


PAPER_API_OWNER = "private-data-api"
PAPER_REQUIRED_TABLES = frozenset({"portfolios", "slots", "positions", "trades"})
PAPER_EXPLICIT_APPROVAL_MODE = "explicit-user"
PAPER_POLICY_APPROVAL_MODE = "policy-auto-sell-only"


@dataclass(frozen=True, order=True)
class PaperCallerPolicy:
    caller: str
    operations: tuple[str, ...]
    approval_mode: str

    def __post_init__(self) -> None:
        if not isinstance(self.caller, str) or not isinstance(
            self.approval_mode, str
        ):
            raise TypeError("Paper caller policy text fields must be strings")
        if not isinstance(self.operations, tuple) or any(
            not isinstance(operation, str) for operation in self.operations
        ):
            raise TypeError("Paper caller policy operations must be strings")


PAPER_TARGET_CALLER_POLICIES = (
    PaperCallerPolicy(
        caller="paper-ui",
        operations=("paper.buy", "paper.sell"),
        approval_mode=PAPER_EXPLICIT_APPROVAL_MODE,
    ),
    PaperCallerPolicy(
        caller="telegram-intraday",
        operations=("paper.sell",),
        approval_mode=PAPER_POLICY_APPROVAL_MODE,
    ),
    PaperCallerPolicy(
        caller="telegram-kium",
        operations=("paper.buy", "paper.sell"),
        approval_mode=PAPER_EXPLICIT_APPROVAL_MODE,
    ),
    PaperCallerPolicy(
        caller="telegram-quant",
        operations=("paper.buy", "paper.sell"),
        approval_mode=PAPER_EXPLICIT_APPROVAL_MODE,
    ),
)


class PaperWriteReadinessError(RuntimeError):
    def __init__(self, failed_codes: tuple[str, ...]) -> None:
        super().__init__("Paper write readiness failed: " + ", ".join(failed_codes))
        self.failed_codes = failed_codes


@dataclass(frozen=True)
class PaperWriteReadinessEvidence:
    """Target API writer plus exact UI/Telegram consumer policy evidence."""

    private_write_evidence: PrivateWriteReadinessEvidence
    caller_policies: tuple[PaperCallerPolicy, ...]
    consumers_use_private_api: bool
    consumers_use_same_database: bool
    paper_ui_direct_writes_disabled: bool
    telegram_direct_writes_disabled: bool
    operator_cli_rollback_only: bool

    def __post_init__(self) -> None:
        if not isinstance(self.private_write_evidence, PrivateWriteReadinessEvidence):
            raise TypeError("private_write_evidence must use the Private write contract")
        flags = (
            self.consumers_use_private_api,
            self.consumers_use_same_database,
            self.paper_ui_direct_writes_disabled,
            self.telegram_direct_writes_disabled,
            self.operator_cli_rollback_only,
        )
        if any(type(value) is not bool for value in flags):
            raise TypeError("Paper readiness flags must be bool")
        if not isinstance(self.caller_policies, tuple) or any(
            not isinstance(policy, PaperCallerPolicy)
            for policy in self.caller_policies
        ):
            raise TypeError("caller_policies must be PaperCallerPolicy tuples")


@dataclass(frozen=True)
class PaperWriteReadinessCheck:
    code: str
    ready: bool


@dataclass(frozen=True)
class PaperWriteReadinessReport:
    ready: bool
    checks: tuple[PaperWriteReadinessCheck, ...]
    private_write: PrivateWriteReadinessReport

    @property
    def failed_codes(self) -> tuple[str, ...]:
        private_codes = tuple(
            f"private:{code}" for code in self.private_write.failed_codes
        )
        paper_codes = tuple(check.code for check in self.checks if not check.ready)
        return private_codes + paper_codes

    def require_ready(self) -> None:
        if not self.ready:
            raise PaperWriteReadinessError(self.failed_codes)


def _normalized_policies(
    policies: tuple[PaperCallerPolicy, ...],
) -> tuple[PaperCallerPolicy, ...]:
    normalized = []
    for policy in policies:
        normalized.append(
            PaperCallerPolicy(
                caller=policy.caller.strip().lower(),
                operations=tuple(
                    operation.strip().lower()
                    for operation in policy.operations
                    if operation.strip()
                ),
                approval_mode=policy.approval_mode.strip().lower(),
            )
        )
    return tuple(sorted(normalized))


def _paper_schema_ready(evidence: PrivateWriteReadinessEvidence) -> bool:
    path = evidence.database_path
    if path.name != "paper.db":
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            slot_count = int(connection.execute("SELECT COUNT(*) FROM slots").fetchone()[0])
    except (OSError, TypeError, ValueError, sqlite3.Error):
        return False
    return PAPER_REQUIRED_TABLES <= tables and slot_count > 0


def assess_paper_write_readiness(
    evidence: PaperWriteReadinessEvidence,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PaperWriteReadinessReport:
    """Assess the target Paper writer/caller boundary without changing state."""
    private_write = assess_private_write_readiness(
        evidence.private_write_evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    owners = tuple(
        owner.strip()
        for owner in evidence.private_write_evidence.writer_owners
        if owner.strip()
    )
    policies = _normalized_policies(evidence.caller_policies)
    expected_policies = _normalized_policies(PAPER_TARGET_CALLER_POLICIES)
    checks = (
        PaperWriteReadinessCheck(
            "paper_database_scope",
            evidence.private_write_evidence.database_path.name == "paper.db",
        ),
        PaperWriteReadinessCheck(
            "paper_schema_ready",
            _paper_schema_ready(evidence.private_write_evidence),
        ),
        PaperWriteReadinessCheck(
            "single_paper_writer_private_api",
            owners == (PAPER_API_OWNER,),
        ),
        PaperWriteReadinessCheck(
            "caller_policy_matrix_fixed",
            policies == expected_policies,
        ),
        PaperWriteReadinessCheck(
            "consumers_use_private_api",
            evidence.consumers_use_private_api,
        ),
        PaperWriteReadinessCheck(
            "consumers_use_same_database",
            evidence.consumers_use_same_database,
        ),
        PaperWriteReadinessCheck(
            "paper_ui_direct_writes_disabled",
            evidence.paper_ui_direct_writes_disabled,
        ),
        PaperWriteReadinessCheck(
            "telegram_direct_writes_disabled",
            evidence.telegram_direct_writes_disabled,
        ),
        PaperWriteReadinessCheck(
            "operator_cli_rollback_only",
            evidence.operator_cli_rollback_only,
        ),
    )
    return PaperWriteReadinessReport(
        ready=private_write.ready and all(check.ready for check in checks),
        checks=checks,
        private_write=private_write,
    )


def require_paper_write_readiness(
    evidence: PaperWriteReadinessEvidence,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PaperWriteReadinessReport:
    report = assess_paper_write_readiness(
        evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    report.require_ready()
    return report
