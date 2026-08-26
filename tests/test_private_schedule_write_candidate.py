from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from private_schedule_write_candidate import (
    ScheduleWriteCandidateError,
    generate_schedule_write_activation_candidate,
)
from private_schedule_write_readiness import (
    SCHEDULE_NOTIFIER_OPERATIONS,
    SCHEDULE_NOTIFIER_OWNER,
    ScheduleWriteReadinessEvidence,
)
from private_write_runtime import BUNDLE_FILENAME


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


def _staging(tmp_path, name="schedule-candidate"):
    path = (tmp_path / name).resolve()
    path.mkdir(mode=0o700)
    return path


def _state(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_schedule_candidate_is_private_noninstalled_and_runtime_validated(
    tmp_path,
    private_write_readiness_evidence_factory,
):
    evidence = _evidence(private_write_readiness_evidence_factory)
    private = evidence.private_write_evidence
    backup = private.backup_manifest_path.parent / "assistant.db"
    before = {
        path: _state(path)
        for path in (private.database_path, private.backup_manifest_path, backup)
    }
    staging = _staging(tmp_path)

    candidate = generate_schedule_write_activation_candidate(
        evidence,
        staging,
        now=NOW,
    )

    assert candidate.runtime_validated is True
    assert candidate.schedule_enabled is True
    assert candidate.installed is False
    assert candidate.path == staging / BUNDLE_FILENAME
    assert candidate.path.stat().st_mode & 0o777 == 0o600
    assert len(candidate.bundle_id) == 64
    assert len(candidate.activation_fingerprint) == 64
    assert not hasattr(candidate, "install")
    assert not hasattr(candidate, "execute")
    assert str(staging) not in repr(candidate)
    assert before == {
        path: _state(path)
        for path in (private.database_path, private.backup_manifest_path, backup)
    }

    payload = json.loads(candidate.path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["schedule"]["enabled"] is True
    assert payload["schedule"]["notifier_operations"] == list(
        SCHEDULE_NOTIFIER_OPERATIONS
    )
    assert all("token" not in key.lower() for key in payload)
    assert "database_path" not in payload


def test_schedule_candidate_refuses_operational_or_nonempty_staging(
    tmp_path,
    private_write_readiness_evidence_factory,
):
    evidence = _evidence(private_write_readiness_evidence_factory)
    private = evidence.private_write_evidence
    with pytest.raises(ScheduleWriteCandidateError) as operational:
        generate_schedule_write_activation_candidate(
            evidence,
            private.database_path.parent,
            now=NOW,
        )
    assert operational.value.code == "invalid_noninstall_staging"

    staging = _staging(tmp_path)
    marker = staging / "preserve.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(ScheduleWriteCandidateError) as nonempty:
        generate_schedule_write_activation_candidate(evidence, staging, now=NOW)
    assert nonempty.value.code == "invalid_noninstall_staging"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_schedule_candidate_requires_notifier_ownership_before_writing(
    tmp_path,
    private_write_readiness_evidence_factory,
):
    evidence = replace(
        _evidence(private_write_readiness_evidence_factory),
        notifier_writer_owners=("other",),
    )
    staging = _staging(tmp_path)

    with pytest.raises(RuntimeError) as error:
        generate_schedule_write_activation_candidate(evidence, staging, now=NOW)

    assert "single_notifier_owner" in str(error.value)
    assert list(staging.iterdir()) == []


def test_schedule_candidate_requires_rollback_and_fresh_backup_before_writing(
    tmp_path,
    private_write_readiness_evidence_factory,
):
    evidence = _evidence(private_write_readiness_evidence_factory)
    not_rehearsed = replace(
        evidence,
        private_write_evidence=replace(
            evidence.private_write_evidence,
            rollback_verified=False,
        ),
    )
    first = _staging(tmp_path, "not-rehearsed")
    with pytest.raises(RuntimeError) as rollback_error:
        generate_schedule_write_activation_candidate(not_rehearsed, first, now=NOW)
    assert "rollback_verified" in str(rollback_error.value)
    assert list(first.iterdir()) == []

    manifest = evidence.private_write_evidence.backup_manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)
    second = _staging(tmp_path, "stale")
    with pytest.raises(RuntimeError) as stale_error:
        generate_schedule_write_activation_candidate(evidence, second, now=NOW)
    assert "backup_fresh" in str(stale_error.value)
    assert list(second.iterdir()) == []


def test_schedule_candidate_rejects_invalid_current_owners(
    tmp_path,
    private_write_readiness_evidence_factory,
):
    evidence = _evidence(private_write_readiness_evidence_factory)
    with pytest.raises(ScheduleWriteCandidateError) as watchlist_owner:
        generate_schedule_write_activation_candidate(
            evidence,
            _staging(tmp_path, "watchlist-owner"),
            current_watchlist_writer_owner="other",
            now=NOW,
        )
    assert watchlist_owner.value.code == "invalid_current_watchlist_writer_owner"

    with pytest.raises(ScheduleWriteCandidateError) as schedule_owner:
        generate_schedule_write_activation_candidate(
            evidence,
            _staging(tmp_path, "schedule-owner"),
            current_schedule_user_writer_owner="other",
            now=NOW,
        )
    assert schedule_owner.value.code == "invalid_current_schedule_writer_owner"


def test_same_schedule_evidence_produces_same_candidate_ids(
    tmp_path,
    private_write_readiness_evidence_factory,
):
    evidence = _evidence(private_write_readiness_evidence_factory)
    first = generate_schedule_write_activation_candidate(
        evidence,
        _staging(tmp_path, "first"),
        now=NOW,
    )
    second = generate_schedule_write_activation_candidate(
        evidence,
        _staging(tmp_path, "second"),
        now=NOW,
    )

    assert first.bundle_id == second.bundle_id
    assert first.activation_fingerprint == second.activation_fingerprint
