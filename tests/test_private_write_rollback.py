from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import pytest

from private_write_rollback import (
    PrivateWriteRollbackError,
    rehearse_private_write_rollback,
)


NOW = datetime.now().astimezone()


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def _workspace(tmp_path):
    path = (tmp_path / "rehearsal").resolve()
    path.mkdir(mode=0o700)
    return path


def test_rehearsal_uses_copies_preserves_source_and_keeps_artifacts(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    backup = evidence.backup_manifest_path.parent / "assistant.db"
    source_before = _digest(evidence.database_path)
    backup_before = _digest(backup)
    workspace = _workspace(tmp_path)

    report = rehearse_private_write_rollback(
        evidence.backup_manifest_path,
        workspace,
        now=NOW,
    )

    assert report.verified is True
    assert report.api_write_applied is True
    assert report.legacy_writer_resumed is True
    assert report.service_rollback_preserved_api_write is True
    assert report.emergency_restore_verified is True
    assert report.backup_unchanged is True
    assert source_before == _digest(evidence.database_path)
    assert backup_before == _digest(backup)
    assert (workspace / "active-clone" / "assistant.db").is_file()
    assert (workspace / "emergency-restore" / "assistant.db").is_file()
    assert str(tmp_path) not in repr(report)


def test_rehearsal_refuses_nonempty_workspace(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    workspace = _workspace(tmp_path)
    marker = workspace / "preserve.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(PrivateWriteRollbackError) as exc:
        rehearse_private_write_rollback(evidence.backup_manifest_path, workspace, now=NOW)

    assert exc.value.code == "workspace_not_empty_private_dir"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_rehearsal_rejects_stale_backup(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    manifest = evidence.backup_manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(PrivateWriteRollbackError) as exc:
        rehearse_private_write_rollback(manifest, _workspace(tmp_path), now=NOW)

    assert exc.value.code == "stale_backup"


def test_rehearsal_rejects_manifest_backup_path_traversal(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    manifest = evidence.backup_manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["databases"][0]["backup_file"] = "../assistant.db"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(PrivateWriteRollbackError) as exc:
        rehearse_private_write_rollback(manifest, _workspace(tmp_path), now=NOW)

    assert exc.value.code == "backup_path_invalid"


def test_rehearsal_rejects_tampered_backup_hash(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    manifest = evidence.backup_manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["databases"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(PrivateWriteRollbackError) as exc:
        rehearse_private_write_rollback(manifest, _workspace(tmp_path), now=NOW)

    assert exc.value.code == "backup_hash_mismatch"
