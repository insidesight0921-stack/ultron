from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

import paper_db
from private_paper_write_cutover import (
    PAPER_CUTOVER_STEPS,
    PAPER_LEGACY_RUNTIME_WRITERS,
    PAPER_ROLLBACK_STEPS,
    PaperWriteCutoverConfig,
    PaperWriteCutoverError,
    assess_paper_write_cutover,
    build_paper_write_cutover_dry_run,
)
from private_paper_write_readiness import (
    PAPER_TARGET_CALLER_POLICIES,
    PaperWriteReadinessEvidence,
)


NOW = datetime.now().astimezone()


def _config(tmp_path, private_write_readiness_evidence_factory):
    database = (tmp_path / "paper-cutover" / "paper.db").resolve()
    paper_db.ensure_seed(db_path=database, seed_capital=100_000_000)
    private = private_write_readiness_evidence_factory(database)
    readiness = PaperWriteReadinessEvidence(
        private_write_evidence=private,
        caller_policies=PAPER_TARGET_CALLER_POLICIES,
        consumers_use_private_api=True,
        consumers_use_same_database=True,
        paper_ui_direct_writes_disabled=True,
        telegram_direct_writes_disabled=True,
        operator_cli_rollback_only=True,
    )
    return PaperWriteCutoverConfig(
        readiness_evidence=readiness,
        current_api_paper_writer_enabled=False,
        current_paper_client_enabled=False,
        current_paper_executor_enabled=False,
        current_runtime_writer_owners=PAPER_LEGACY_RUNTIME_WRITERS,
        current_operator_cli_rollback_only=True,
    )


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_paper_dry_run_is_permit_bound_sanitized_and_non_mutating(
    tmp_path, private_write_readiness_evidence_factory
):
    config = _config(tmp_path, private_write_readiness_evidence_factory)
    private = config.readiness_evidence.private_write_evidence
    backup = private.backup_manifest_path.parent / "paper.db"
    paths = (private.database_path, private.backup_manifest_path, backup)
    before = {path: _digest(path) for path in paths}

    plan = build_paper_write_cutover_dry_run(config, now=NOW)

    assert plan.dry_run is True
    assert plan.automatic_backup_restore is False
    assert plan.cutover_steps == PAPER_CUTOVER_STEPS
    assert plan.rollback_steps == PAPER_ROLLBACK_STEPS
    assert plan.caller_policies == PAPER_TARGET_CALLER_POLICIES
    assert plan.writer_owner == "private-data-api"
    assert len(plan.bundle_id) == 64
    assert len(plan.activation_fingerprint) == 64
    assert not hasattr(plan, "activation_permit")
    assert not hasattr(plan, "execute")
    assert not hasattr(plan, "apply")
    assert str(private.database_path) not in repr(plan)
    assert before == {path: _digest(path) for path in paths}


@pytest.mark.parametrize(
    "field",
    [
        "current_api_paper_writer_enabled",
        "current_paper_client_enabled",
        "current_paper_executor_enabled",
    ],
)
def test_any_existing_partial_paper_activation_blocks_plan(
    tmp_path, private_write_readiness_evidence_factory, field
):
    config = replace(
        _config(tmp_path, private_write_readiness_evidence_factory),
        **{field: True},
    )
    report = assess_paper_write_cutover(config, now=NOW)
    assert "current_paper_write_stack_disabled" in report.failed_codes


@pytest.mark.parametrize(
    "owners",
    [
        (),
        PAPER_LEGACY_RUNTIME_WRITERS[:-1],
        PAPER_LEGACY_RUNTIME_WRITERS + ("private-data-api",),
        tuple(reversed(PAPER_LEGACY_RUNTIME_WRITERS)),
    ],
)
def test_current_legacy_runtime_writer_inventory_is_exact(
    tmp_path, private_write_readiness_evidence_factory, owners
):
    config = replace(
        _config(tmp_path, private_write_readiness_evidence_factory),
        current_runtime_writer_owners=owners,
    )
    report = assess_paper_write_cutover(config, now=NOW)
    assert "current_legacy_runtime_writers_fixed" in report.failed_codes


def test_operator_cli_must_remain_rollback_only(
    tmp_path, private_write_readiness_evidence_factory
):
    config = replace(
        _config(tmp_path, private_write_readiness_evidence_factory),
        current_operator_cli_rollback_only=False,
    )
    report = assess_paper_write_cutover(config, now=NOW)
    assert "current_operator_cli_rollback_only" in report.failed_codes


def test_target_readiness_drift_is_namespaced(
    tmp_path, private_write_readiness_evidence_factory
):
    config = _config(tmp_path, private_write_readiness_evidence_factory)
    readiness = replace(
        config.readiness_evidence,
        telegram_direct_writes_disabled=False,
    )
    report = assess_paper_write_cutover(
        replace(config, readiness_evidence=readiness),
        now=NOW,
    )
    assert "readiness:telegram_direct_writes_disabled" in report.failed_codes


def test_paper_cutover_order_drift_is_rejected(
    tmp_path, private_write_readiness_evidence_factory
):
    config = replace(
        _config(tmp_path, private_write_readiness_evidence_factory),
        cutover_steps=tuple(reversed(PAPER_CUTOVER_STEPS)),
    )
    with pytest.raises(PaperWriteCutoverError) as error:
        build_paper_write_cutover_dry_run(config, now=NOW)
    assert "cutover_order_fixed" in error.value.failed_codes


def test_paper_rollback_order_drift_is_rejected(
    tmp_path, private_write_readiness_evidence_factory
):
    config = replace(
        _config(tmp_path, private_write_readiness_evidence_factory),
        rollback_steps=tuple(reversed(PAPER_ROLLBACK_STEPS)),
    )
    with pytest.raises(PaperWriteCutoverError) as error:
        build_paper_write_cutover_dry_run(config, now=NOW)
    assert "rollback_order_fixed" in error.value.failed_codes


def test_automatic_restore_is_never_part_of_paper_cutover(
    tmp_path, private_write_readiness_evidence_factory
):
    config = replace(
        _config(tmp_path, private_write_readiness_evidence_factory),
        automatic_backup_restore=True,
    )
    report = assess_paper_write_cutover(config, now=NOW)
    assert "automatic_backup_restore_disabled" in report.failed_codes


def test_stale_paper_backup_prevents_permit_and_bundle(
    tmp_path, private_write_readiness_evidence_factory
):
    config = _config(tmp_path, private_write_readiness_evidence_factory)
    manifest = config.readiness_evidence.private_write_evidence.backup_manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(PaperWriteCutoverError) as error:
        build_paper_write_cutover_dry_run(config, now=NOW)

    assert "readiness:private:backup_fresh" in error.value.failed_codes
