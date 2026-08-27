from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

import paper_db
from private_paper_write_rollback import (
    PaperWriteRollbackError,
    rehearse_paper_write_rollback,
)


NOW = datetime.now().astimezone()


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def _workspace(tmp_path, name="paper-rehearsal"):
    path = (tmp_path / name).resolve()
    path.mkdir(mode=0o700)
    return path


def _evidence(tmp_path, private_write_readiness_evidence_factory):
    database = (tmp_path / "paper-source" / "paper.db").resolve()
    paper_db.ensure_seed(db_path=database, seed_capital=100_000_000)
    return private_write_readiness_evidence_factory(database)


def _counts(path):
    with sqlite3.connect(path) as connection:
        return {
            name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("portfolios", "slots", "positions", "trades")
        }


def test_paper_rehearsal_preserves_source_backup_and_artifacts(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = _evidence(tmp_path, private_write_readiness_evidence_factory)
    backup = evidence.backup_manifest_path.parent / "paper.db"
    source_before = _digest(evidence.database_path)
    backup_before = _digest(backup)
    baseline_counts = _counts(evidence.database_path)
    workspace = _workspace(tmp_path)

    report = rehearse_paper_write_rollback(
        evidence.backup_manifest_path,
        workspace,
        now=NOW,
    )

    assert report.verified is True
    assert report.api_buy_applied is True
    assert report.api_sell_applied is True
    assert report.api_round_trip_balanced is True
    assert report.legacy_buy_resumed is True
    assert report.legacy_sell_resumed is True
    assert report.service_rollback_preserved_api_writes is True
    assert report.emergency_restore_verified is True
    assert report.backup_unchanged is True
    assert source_before == _digest(evidence.database_path)
    assert backup_before == _digest(backup)
    active = workspace / "active-clone" / "paper.db"
    restored = workspace / "emergency-restore" / "paper.db"
    assert active.is_file()
    assert restored.is_file()
    assert _counts(active) == {**baseline_counts, "trades": baseline_counts["trades"] + 4}
    assert _counts(restored) == baseline_counts
    assert str(tmp_path) not in repr(report)


def test_paper_rehearsal_refuses_nonempty_workspace(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = _evidence(tmp_path, private_write_readiness_evidence_factory)
    workspace = _workspace(tmp_path)
    marker = workspace / "preserve.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(PaperWriteRollbackError) as error:
        rehearse_paper_write_rollback(
            evidence.backup_manifest_path,
            workspace,
            now=NOW,
        )

    assert error.value.code == "workspace_not_empty_private_dir"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_paper_rehearsal_rejects_stale_backup(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = _evidence(tmp_path, private_write_readiness_evidence_factory)
    manifest = evidence.backup_manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(PaperWriteRollbackError) as error:
        rehearse_paper_write_rollback(manifest, _workspace(tmp_path), now=NOW)

    assert error.value.code == "stale_backup"


def test_paper_rehearsal_rejects_tampered_backup(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = _evidence(tmp_path, private_write_readiness_evidence_factory)
    manifest = evidence.backup_manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["databases"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(PaperWriteRollbackError) as error:
        rehearse_paper_write_rollback(manifest, _workspace(tmp_path), now=NOW)

    assert error.value.code == "backup_hash_mismatch"


def test_paper_rehearsal_requires_exactly_one_paper_backup(
    tmp_path, private_write_readiness_evidence_factory
):
    assistant_only = private_write_readiness_evidence_factory()

    with pytest.raises(PaperWriteRollbackError) as error:
        rehearse_paper_write_rollback(
            assistant_only.backup_manifest_path,
            _workspace(tmp_path),
            now=NOW,
        )

    assert error.value.code == "paper_backup_missing"


def test_paper_rehearsal_requires_seeded_slot(
    tmp_path, private_write_readiness_evidence_factory
):
    empty = (tmp_path / "empty-paper" / "paper.db").resolve()
    evidence = private_write_readiness_evidence_factory(empty)

    with pytest.raises(PaperWriteRollbackError) as error:
        rehearse_paper_write_rollback(
            evidence.backup_manifest_path,
            _workspace(tmp_path),
            now=NOW,
        )

    assert error.value.code == "rehearsal_slot_unavailable"
