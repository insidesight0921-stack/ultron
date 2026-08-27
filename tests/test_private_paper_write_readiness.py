from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime

import pytest

import paper_db
from private_paper_write_readiness import (
    PAPER_POLICY_APPROVAL_MODE,
    PAPER_TARGET_CALLER_POLICIES,
    PaperCallerPolicy,
    PaperWriteReadinessError,
    PaperWriteReadinessEvidence,
    assess_paper_write_readiness,
    require_paper_write_readiness,
)


NOW = datetime.now().astimezone()


def _evidence(tmp_path, private_write_readiness_evidence_factory):
    database = (tmp_path / "paper-readiness" / "paper.db").resolve()
    paper_db.ensure_seed(db_path=database, seed_capital=100_000_000)
    private = private_write_readiness_evidence_factory(database)
    return PaperWriteReadinessEvidence(
        private_write_evidence=private,
        caller_policies=PAPER_TARGET_CALLER_POLICIES,
        consumers_use_private_api=True,
        consumers_use_same_database=True,
        paper_ui_direct_writes_disabled=True,
        telegram_direct_writes_disabled=True,
        operator_cli_rollback_only=True,
    )


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_complete_paper_readiness_passes_without_mutating_evidence(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = _evidence(tmp_path, private_write_readiness_evidence_factory)
    private = evidence.private_write_evidence
    backup = private.backup_manifest_path.parent / "paper.db"
    paths = (private.database_path, private.backup_manifest_path, backup)
    before = {path: _digest(path) for path in paths}

    report = require_paper_write_readiness(evidence, now=NOW)

    assert report.ready is True
    assert report.failed_codes == ()
    assert len(report.checks) == 9
    assert before == {path: _digest(path) for path in paths}


def test_paper_readiness_requires_paper_name_and_domain_schema(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = _evidence(tmp_path, private_write_readiness_evidence_factory)
    wrong_name = evidence.private_write_evidence.database_path.with_name("assistant.db")
    wrong_name.write_bytes(evidence.private_write_evidence.database_path.read_bytes())
    wrong_name.chmod(0o600)
    wrong = replace(
        evidence,
        private_write_evidence=replace(
            evidence.private_write_evidence,
            database_path=wrong_name,
        ),
    )
    wrong_report = assess_paper_write_readiness(wrong, now=NOW)
    assert "paper_database_scope" in wrong_report.failed_codes
    assert "paper_schema_ready" in wrong_report.failed_codes

    empty_db = (tmp_path / "empty" / "paper.db").resolve()
    empty_private = private_write_readiness_evidence_factory(empty_db)
    empty = replace(evidence, private_write_evidence=empty_private)
    empty_report = assess_paper_write_readiness(empty, now=NOW)
    assert "paper_schema_ready" in empty_report.failed_codes


@pytest.mark.parametrize(
    "field,code",
    [
        ("consumers_use_private_api", "consumers_use_private_api"),
        ("consumers_use_same_database", "consumers_use_same_database"),
        (
            "paper_ui_direct_writes_disabled",
            "paper_ui_direct_writes_disabled",
        ),
        (
            "telegram_direct_writes_disabled",
            "telegram_direct_writes_disabled",
        ),
        ("operator_cli_rollback_only", "operator_cli_rollback_only"),
    ],
)
def test_each_paper_ownership_flag_fails_closed(
    tmp_path, private_write_readiness_evidence_factory, field, code
):
    evidence = replace(
        _evidence(tmp_path, private_write_readiness_evidence_factory),
        **{field: False},
    )
    report = assess_paper_write_readiness(evidence, now=NOW)
    assert code in report.failed_codes


@pytest.mark.parametrize(
    "policies",
    [
        PAPER_TARGET_CALLER_POLICIES[:-1],
        PAPER_TARGET_CALLER_POLICIES + (PAPER_TARGET_CALLER_POLICIES[0],),
        tuple(
            replace(policy, operations=("paper.buy", "paper.sell"))
            if policy.caller == "telegram-intraday"
            else policy
            for policy in PAPER_TARGET_CALLER_POLICIES
        ),
        tuple(
            replace(policy, approval_mode=PAPER_POLICY_APPROVAL_MODE)
            if policy.caller == "paper-ui"
            else policy
            for policy in PAPER_TARGET_CALLER_POLICIES
        ),
        PAPER_TARGET_CALLER_POLICIES
        + (
            PaperCallerPolicy(
                caller="unknown",
                operations=("paper.buy",),
                approval_mode="explicit-user",
            ),
        ),
    ],
)
def test_caller_policy_matrix_is_exact(
    tmp_path, private_write_readiness_evidence_factory, policies
):
    evidence = replace(
        _evidence(tmp_path, private_write_readiness_evidence_factory),
        caller_policies=policies,
    )
    report = assess_paper_write_readiness(evidence, now=NOW)
    assert "caller_policy_matrix_fixed" in report.failed_codes


def test_private_readiness_failure_is_namespaced(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = _evidence(tmp_path, private_write_readiness_evidence_factory)
    changed = replace(
        evidence,
        private_write_evidence=replace(
            evidence.private_write_evidence,
            consumer_executor_enabled=False,
        ),
    )
    report = assess_paper_write_readiness(changed, now=NOW)
    assert "private:consumer_executor_enabled" in report.failed_codes


def test_paper_readiness_error_contains_codes_not_paths(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = replace(
        _evidence(tmp_path, private_write_readiness_evidence_factory),
        telegram_direct_writes_disabled=False,
    )
    with pytest.raises(PaperWriteReadinessError) as error:
        require_paper_write_readiness(evidence, now=NOW)

    assert error.value.failed_codes == ("telegram_direct_writes_disabled",)
    assert str(tmp_path) not in str(error.value)


def test_paper_readiness_rejects_invalid_evidence_types(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = _evidence(tmp_path, private_write_readiness_evidence_factory)
    with pytest.raises(TypeError, match="flags must be bool"):
        replace(evidence, consumers_use_private_api=1)
    with pytest.raises(TypeError, match="PaperCallerPolicy"):
        replace(evidence, caller_policies=("paper-ui",))
    with pytest.raises(TypeError, match="operations"):
        PaperCallerPolicy("paper-ui", ["paper.buy"], "explicit-user")
