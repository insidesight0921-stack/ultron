from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from private_schedule_write_cutover import (
    LEGACY_SCHEDULE_USER_WRITER,
    SCHEDULE_CUTOVER_STEPS,
    SCHEDULE_ROLLBACK_STEPS,
    ScheduleWriteCutoverConfig,
    ScheduleWriteCutoverError,
    assess_schedule_write_cutover,
    build_schedule_write_cutover_dry_run,
)
from private_schedule_write_readiness import (
    SCHEDULE_NOTIFIER_OPERATIONS,
    SCHEDULE_NOTIFIER_OWNER,
    ScheduleWriteReadinessEvidence,
)


NOW = datetime.now().astimezone()


def _config(private_write_readiness_evidence_factory):
    readiness = ScheduleWriteReadinessEvidence(
        private_write_evidence=private_write_readiness_evidence_factory(),
        notifier_enabled=True,
        notifier_writer_owners=(SCHEDULE_NOTIFIER_OWNER,),
        notifier_operations=SCHEDULE_NOTIFIER_OPERATIONS,
        notifier_uses_same_database=True,
        telegram_direct_user_mutations_disabled=True,
        notifier_user_mutations_disabled=True,
    )
    return ScheduleWriteCutoverConfig(
        readiness_evidence=readiness,
        current_api_schedule_writer_enabled=False,
        current_schedule_client_enabled=False,
        current_schedule_executor_enabled=False,
        current_user_mutation_writer_owners=(LEGACY_SCHEDULE_USER_WRITER,),
        current_notifier_enabled=True,
        current_notifier_writer_owners=(SCHEDULE_NOTIFIER_OWNER,),
        current_notifier_operations=SCHEDULE_NOTIFIER_OPERATIONS,
    )


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_schedule_dry_run_is_permit_bound_sanitized_and_non_mutating(
    private_write_readiness_evidence_factory,
):
    config = _config(private_write_readiness_evidence_factory)
    private = config.readiness_evidence.private_write_evidence
    backup = private.backup_manifest_path.parent / "assistant.db"
    paths = (private.database_path, private.backup_manifest_path, backup)
    before = {path: _digest(path) for path in paths}

    plan = build_schedule_write_cutover_dry_run(config, now=NOW)

    assert plan.dry_run is True
    assert plan.automatic_backup_restore is False
    assert plan.cutover_steps == SCHEDULE_CUTOVER_STEPS
    assert plan.rollback_steps == SCHEDULE_ROLLBACK_STEPS
    assert plan.notifier_owner == SCHEDULE_NOTIFIER_OWNER
    assert plan.notifier_operations == SCHEDULE_NOTIFIER_OPERATIONS
    assert len(plan.bundle_id) == 64
    assert len(plan.activation_fingerprint) == 64
    assert not hasattr(plan, "activation_permit")
    assert not hasattr(plan, "execute")
    assert str(private.database_path) not in repr(plan)
    assert before == {path: _digest(path) for path in paths}


@pytest.mark.parametrize(
    "field",
    [
        "current_api_schedule_writer_enabled",
        "current_schedule_client_enabled",
        "current_schedule_executor_enabled",
    ],
)
def test_any_existing_partial_schedule_activation_blocks_plan(
    private_write_readiness_evidence_factory,
    field,
):
    config = replace(
        _config(private_write_readiness_evidence_factory),
        **{field: True},
    )
    report = assess_schedule_write_cutover(config, now=NOW)
    assert "current_schedule_write_stack_disabled" in report.failed_codes


@pytest.mark.parametrize(
    "owners",
    [(), ("telegram",), (LEGACY_SCHEDULE_USER_WRITER,) * 2],
)
def test_current_user_mutation_writer_must_be_exactly_legacy(
    private_write_readiness_evidence_factory,
    owners,
):
    config = replace(
        _config(private_write_readiness_evidence_factory),
        current_user_mutation_writer_owners=owners,
    )
    report = assess_schedule_write_cutover(config, now=NOW)
    assert "current_single_legacy_user_writer" in report.failed_codes


def test_current_notifier_cannot_be_stopped_or_changed(
    private_write_readiness_evidence_factory,
):
    base = _config(private_write_readiness_evidence_factory)
    stopped = replace(base, current_notifier_enabled=False)
    changed_owner = replace(base, current_notifier_writer_owners=("other",))
    changed_ops = replace(base, current_notifier_operations=("schedule.mark_notified",))

    for config in (stopped, changed_owner, changed_ops):
        report = assess_schedule_write_cutover(config, now=NOW)
        assert "current_notifier_preserved" in report.failed_codes


def test_target_notifier_drift_is_reported_by_readiness_and_cutover(
    private_write_readiness_evidence_factory,
):
    config = _config(private_write_readiness_evidence_factory)
    changed = replace(
        config.readiness_evidence,
        notifier_writer_owners=("other",),
    )
    report = assess_schedule_write_cutover(
        replace(config, readiness_evidence=changed),
        now=NOW,
    )

    assert "readiness:single_notifier_owner" in report.failed_codes
    assert "target_notifier_preserved" in report.failed_codes


def test_schedule_cutover_order_drift_is_rejected(
    private_write_readiness_evidence_factory,
):
    config = replace(
        _config(private_write_readiness_evidence_factory),
        cutover_steps=tuple(reversed(SCHEDULE_CUTOVER_STEPS)),
    )
    with pytest.raises(ScheduleWriteCutoverError) as error:
        build_schedule_write_cutover_dry_run(config, now=NOW)
    assert "cutover_order_fixed" in error.value.failed_codes


def test_schedule_rollback_order_drift_is_rejected(
    private_write_readiness_evidence_factory,
):
    config = replace(
        _config(private_write_readiness_evidence_factory),
        rollback_steps=tuple(reversed(SCHEDULE_ROLLBACK_STEPS)),
    )
    with pytest.raises(ScheduleWriteCutoverError) as error:
        build_schedule_write_cutover_dry_run(config, now=NOW)
    assert "rollback_order_fixed" in error.value.failed_codes


def test_automatic_restore_is_not_part_of_schedule_cutover(
    private_write_readiness_evidence_factory,
):
    config = replace(
        _config(private_write_readiness_evidence_factory),
        automatic_backup_restore=True,
    )
    report = assess_schedule_write_cutover(config, now=NOW)
    assert "automatic_backup_restore_disabled" in report.failed_codes


def test_stale_backup_prevents_schedule_bundle(
    private_write_readiness_evidence_factory,
):
    config = _config(private_write_readiness_evidence_factory)
    manifest = config.readiness_evidence.private_write_evidence.backup_manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(ScheduleWriteCutoverError) as error:
        build_schedule_write_cutover_dry_run(config, now=NOW)

    assert "readiness:private:backup_fresh" in error.value.failed_codes
