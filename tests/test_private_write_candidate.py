from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from private_write_candidate import (
    PrivateWriteCandidateError,
    generate_private_write_activation_candidate,
)
from private_write_runtime import BUNDLE_FILENAME


NOW = datetime.now().astimezone()


def _staging(tmp_path, name="candidate"):
    path = (tmp_path / name).resolve()
    path.mkdir(mode=0o700)
    return path


def _state(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_candidate_is_exclusive_noninstalled_and_runtime_validated_without_mutation(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    backup = evidence.backup_manifest_path.parent / "assistant.db"
    before = {
        path: _state(path)
        for path in (evidence.database_path, evidence.backup_manifest_path, backup)
    }
    staging = _staging(tmp_path)

    candidate = generate_private_write_activation_candidate(
        evidence,
        staging,
        now=NOW,
    )

    assert candidate.runtime_validated is True
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
        for path in (evidence.database_path, evidence.backup_manifest_path, backup)
    }

    payload = json.loads(candidate.path.read_text(encoding="utf-8"))
    assert all("token" not in key.lower() for key in payload)
    assert "database_path" not in payload


def test_candidate_refuses_operational_private_root(
    private_write_readiness_evidence_factory,
):
    evidence = private_write_readiness_evidence_factory()

    with pytest.raises(PrivateWriteCandidateError) as exc:
        generate_private_write_activation_candidate(
            evidence,
            evidence.database_path.parent,
            now=NOW,
        )

    assert exc.value.code == "invalid_noninstall_staging"


def test_candidate_refuses_nonempty_staging_without_overwrite(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    staging = _staging(tmp_path)
    marker = staging / "preserve.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(PrivateWriteCandidateError) as exc:
        generate_private_write_activation_candidate(evidence, staging, now=NOW)

    assert exc.value.code == "invalid_noninstall_staging"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_candidate_refuses_existing_activation_filename(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    staging = _staging(tmp_path)
    target = staging / BUNDLE_FILENAME
    target.write_text("preserve", encoding="utf-8")
    target.chmod(0o600)

    with pytest.raises(PrivateWriteCandidateError) as exc:
        generate_private_write_activation_candidate(evidence, staging, now=NOW)

    assert exc.value.code == "invalid_noninstall_staging"
    assert target.read_text(encoding="utf-8") == "preserve"


def test_candidate_requires_verified_rollback_before_writing(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = replace(
        private_write_readiness_evidence_factory(),
        rollback_verified=False,
    )
    staging = _staging(tmp_path)

    with pytest.raises(RuntimeError) as exc:
        generate_private_write_activation_candidate(evidence, staging, now=NOW)

    assert "rollback_verified" in str(exc.value)
    assert list(staging.iterdir()) == []


def test_candidate_rejects_stale_backup_before_writing(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    payload = json.loads(evidence.backup_manifest_path.read_text(encoding="utf-8"))
    payload["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    evidence.backup_manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    evidence.backup_manifest_path.chmod(0o600)
    staging = _staging(tmp_path)

    with pytest.raises(RuntimeError) as exc:
        generate_private_write_activation_candidate(evidence, staging, now=NOW)

    assert "backup_fresh" in str(exc.value)
    assert list(staging.iterdir()) == []


def test_same_evidence_produces_same_candidate_ids(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    first = generate_private_write_activation_candidate(
        evidence,
        _staging(tmp_path, "first"),
        now=NOW,
    )
    second = generate_private_write_activation_candidate(
        evidence,
        _staging(tmp_path, "second"),
        now=NOW,
    )

    assert first.bundle_id == second.bundle_id
    assert first.activation_fingerprint == second.activation_fingerprint
