from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from private_write_cutover import (
    CUTOVER_STEPS,
    ROLLBACK_STEPS,
    PrivateWriteCutoverConfig,
    PrivateWriteCutoverError,
    assess_private_write_cutover,
    build_private_write_cutover_dry_run,
)


NOW = datetime.now().astimezone()


def _config(private_write_readiness_evidence_factory):
    return PrivateWriteCutoverConfig(
        readiness_evidence=private_write_readiness_evidence_factory(),
        current_api_writer_enabled=False,
        current_client_writes_enabled=False,
        current_consumer_executor_enabled=False,
        current_writer_owners=("telegram-direct",),
    )


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_dry_run_builds_permit_bound_immutable_service_plan_without_mutation(
    private_write_readiness_evidence_factory,
):
    config = _config(private_write_readiness_evidence_factory)
    evidence = config.readiness_evidence
    backup = evidence.backup_manifest_path.parent / "assistant.db"
    paths = (evidence.database_path, evidence.backup_manifest_path, backup)
    before = {path: _digest(path) for path in paths}

    plan = build_private_write_cutover_dry_run(config, now=NOW)

    assert plan.dry_run is True
    assert plan.automatic_backup_restore is False
    assert plan.cutover_steps == CUTOVER_STEPS
    assert plan.rollback_steps == ROLLBACK_STEPS
    assert len(plan.bundle_id) == 64
    assert len(plan.activation_fingerprint) == 64
    assert not hasattr(plan, "activation_permit")
    assert not hasattr(plan, "execute")
    assert before == {path: _digest(path) for path in paths}
    assert str(evidence.database_path) not in repr(plan)


@pytest.mark.parametrize(
    "field",
    [
        "current_api_writer_enabled",
        "current_client_writes_enabled",
        "current_consumer_executor_enabled",
    ],
)
def test_any_preexisting_partial_activation_blocks_plan(
    private_write_readiness_evidence_factory, field
):
    config = replace(_config(private_write_readiness_evidence_factory), **{field: True})
    report = assess_private_write_cutover(config, now=NOW)
    assert report.ready is False
    assert "current_write_stack_disabled" in report.failed_codes


@pytest.mark.parametrize(
    "owners",
    [("telegram-direct", "private-data-api"), ("other",), ("telegram-direct",) * 2],
)
def test_current_writer_must_be_none_or_one_legacy_owner(
    private_write_readiness_evidence_factory, owners
):
    config = replace(
        _config(private_write_readiness_evidence_factory),
        current_writer_owners=owners,
    )
    report = assess_private_write_cutover(config, now=NOW)
    assert "current_single_legacy_writer" in report.failed_codes


def test_cutover_order_drift_is_rejected(private_write_readiness_evidence_factory):
    config = replace(
        _config(private_write_readiness_evidence_factory),
        cutover_steps=tuple(reversed(CUTOVER_STEPS)),
    )
    with pytest.raises(PrivateWriteCutoverError) as exc:
        build_private_write_cutover_dry_run(config, now=NOW)
    assert "cutover_order_fixed" in exc.value.failed_codes


def test_rollback_order_drift_is_rejected(private_write_readiness_evidence_factory):
    config = replace(
        _config(private_write_readiness_evidence_factory),
        rollback_steps=tuple(reversed(ROLLBACK_STEPS)),
    )
    with pytest.raises(PrivateWriteCutoverError) as exc:
        build_private_write_cutover_dry_run(config, now=NOW)
    assert "rollback_order_fixed" in exc.value.failed_codes


def test_automatic_backup_restore_is_never_part_of_cutover(
    private_write_readiness_evidence_factory,
):
    config = replace(
        _config(private_write_readiness_evidence_factory),
        automatic_backup_restore=True,
    )
    report = assess_private_write_cutover(config, now=NOW)
    assert "automatic_backup_restore_disabled" in report.failed_codes


def test_target_partial_activation_is_reported_as_readiness_failure(
    private_write_readiness_evidence_factory,
):
    config = _config(private_write_readiness_evidence_factory)
    evidence = replace(config.readiness_evidence, client_writes_enabled=False)
    report = assess_private_write_cutover(
        replace(config, readiness_evidence=evidence),
        now=NOW,
    )
    assert "readiness:client_writes_enabled" in report.failed_codes


def test_stale_backup_prevents_permit_and_bundle(
    private_write_readiness_evidence_factory,
):
    config = _config(private_write_readiness_evidence_factory)
    manifest = config.readiness_evidence.backup_manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(PrivateWriteCutoverError) as exc:
        build_private_write_cutover_dry_run(config, now=NOW)

    assert "readiness:backup_fresh" in exc.value.failed_codes
