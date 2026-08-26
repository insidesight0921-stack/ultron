from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime

import pytest

from private_schedule_write_readiness import (
    SCHEDULE_NOTIFIER_OPERATIONS,
    SCHEDULE_NOTIFIER_OWNER,
    ScheduleWriteReadinessError,
    ScheduleWriteReadinessEvidence,
    assess_schedule_write_readiness,
    require_schedule_write_readiness,
)


NOW = datetime.now().astimezone()


def _evidence(private_write_readiness_evidence_factory):
    return ScheduleWriteReadinessEvidence(
        private_write_evidence=private_write_readiness_evidence_factory(),
        notifier_enabled=True,
        notifier_writer_owners=(SCHEDULE_NOTIFIER_OWNER,),
        notifier_operations=SCHEDULE_NOTIFIER_OPERATIONS,
        notifier_uses_same_database=True,
        telegram_direct_user_mutations_disabled=True,
        notifier_user_mutations_disabled=True,
    )


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_complete_schedule_readiness_passes_without_mutating_evidence(
    private_write_readiness_evidence_factory,
):
    evidence = _evidence(private_write_readiness_evidence_factory)
    private = evidence.private_write_evidence
    backup = private.backup_manifest_path.parent / "assistant.db"
    paths = (private.database_path, private.backup_manifest_path, backup)
    before = {path: _digest(path) for path in paths}

    report = require_schedule_write_readiness(evidence, now=NOW)

    assert report.ready is True
    assert report.failed_codes == ()
    assert len(report.checks) == 8
    assert before == {path: _digest(path) for path in paths}


@pytest.mark.parametrize(
    "field,code",
    [
        ("notifier_enabled", "notifier_enabled"),
        ("notifier_uses_same_database", "notifier_same_database"),
        (
            "telegram_direct_user_mutations_disabled",
            "telegram_direct_user_mutations_disabled",
        ),
        ("notifier_user_mutations_disabled", "notifier_user_mutations_disabled"),
    ],
)
def test_each_schedule_safety_flag_fails_closed(
    private_write_readiness_evidence_factory,
    field,
    code,
):
    evidence = replace(
        _evidence(private_write_readiness_evidence_factory),
        **{field: False},
    )

    report = assess_schedule_write_readiness(evidence, now=NOW)

    assert report.ready is False
    assert code in report.failed_codes


@pytest.mark.parametrize(
    "owners",
    [(), ("telegram",), (SCHEDULE_NOTIFIER_OWNER,) * 2, ("private-data-api",)],
)
def test_notifier_must_have_exactly_one_dedicated_owner(
    private_write_readiness_evidence_factory,
    owners,
):
    evidence = replace(
        _evidence(private_write_readiness_evidence_factory),
        notifier_writer_owners=owners,
    )
    report = assess_schedule_write_readiness(evidence, now=NOW)
    assert "single_notifier_owner" in report.failed_codes


def test_notifier_operations_are_exact_and_disjoint(
    private_write_readiness_evidence_factory,
):
    missing = replace(
        _evidence(private_write_readiness_evidence_factory),
        notifier_operations=SCHEDULE_NOTIFIER_OPERATIONS[:-1],
    )
    overlap = replace(
        _evidence(private_write_readiness_evidence_factory),
        notifier_operations=("schedule.add",) + SCHEDULE_NOTIFIER_OPERATIONS[1:],
    )

    assert "notifier_operations_fixed" in assess_schedule_write_readiness(
        missing, now=NOW
    ).failed_codes
    overlap_report = assess_schedule_write_readiness(overlap, now=NOW)
    assert "notifier_operations_fixed" in overlap_report.failed_codes
    assert "writer_operations_disjoint" in overlap_report.failed_codes


def test_private_write_failure_is_namespaced(
    private_write_readiness_evidence_factory,
):
    evidence = _evidence(private_write_readiness_evidence_factory)
    evidence = replace(
        evidence,
        private_write_evidence=replace(
            evidence.private_write_evidence,
            consumer_executor_enabled=False,
        ),
    )

    report = assess_schedule_write_readiness(evidence, now=NOW)

    assert "private:consumer_executor_enabled" in report.failed_codes


def test_schedule_readiness_error_exposes_codes_not_paths(
    private_write_readiness_evidence_factory,
):
    evidence = replace(
        _evidence(private_write_readiness_evidence_factory),
        notifier_enabled=False,
    )
    with pytest.raises(ScheduleWriteReadinessError) as error:
        require_schedule_write_readiness(evidence, now=NOW)

    assert error.value.failed_codes == ("notifier_enabled",)
    assert str(evidence.private_write_evidence.database_path) not in str(error.value)


def test_schedule_readiness_rejects_non_boolean_flags(
    private_write_readiness_evidence_factory,
):
    with pytest.raises(TypeError, match="flags must be bool"):
        replace(
            _evidence(private_write_readiness_evidence_factory),
            notifier_enabled=1,
        )
