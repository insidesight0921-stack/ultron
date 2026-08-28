from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import private_data_security as pds
from private_write_readiness import (
    PrivateWriteActivationError,
    PrivateWriteActivationPermit,
    PrivateWriteReadinessError,
    PrivateWriteReadinessEvidence,
    assess_private_write_readiness,
    issue_private_write_activation_permit,
    require_private_write_activation_permit,
    require_private_write_readiness,
)


NOW = datetime(2026, 8, 25, 22, 0, tzinfo=timezone(timedelta(hours=9)))


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ready_evidence(tmp_path):
    database = (tmp_path / "operational" / "assistant.db").resolve()
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE watchlist(ticker TEXT PRIMARY KEY, name TEXT NOT NULL)")
        con.execute("INSERT INTO watchlist VALUES ('005930', '삼성전자')")
        con.commit()
    database.chmod(0o600)

    snapshot = (tmp_path / "backups" / "snapshot").resolve()
    snapshot.mkdir(parents=True, mode=0o700)
    backup_result = pds.backup_database(database, snapshot / "assistant.db")
    manifest = snapshot / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": (NOW - timedelta(minutes=5)).isoformat(),
                "all_restore_verified": True,
                "databases": [backup_result],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    evidence = PrivateWriteReadinessEvidence(
        api_writer_enabled=True,
        client_writes_enabled=True,
        consumer_executor_enabled=True,
        writer_owners=("private-data-api",),
        database_path=database,
        backup_manifest_path=manifest,
        user_approval_required=True,
        direct_db_fallback_disabled=True,
        rollback_verified=True,
    )
    return evidence


def test_complete_readiness_evidence_passes_without_mutating_files(tmp_path):
    evidence = _ready_evidence(tmp_path)
    backup = evidence.backup_manifest_path.parent / "assistant.db"
    before = {
        path: (_sha256(path), path.stat().st_mtime_ns)
        for path in (evidence.database_path, evidence.backup_manifest_path, backup)
    }

    report = require_private_write_readiness(evidence, now=NOW)

    assert report.ready is True
    assert report.failed_codes == ()
    assert len(report.checks) == 12
    assert before == {
        path: (_sha256(path), path.stat().st_mtime_ns)
        for path in (evidence.database_path, evidence.backup_manifest_path, backup)
    }


def test_paper_database_uses_the_same_readiness_contract(tmp_path):
    evidence = _ready_evidence(tmp_path)
    paper_database = evidence.database_path.with_name("paper.db")
    evidence.database_path.rename(paper_database)
    payload = json.loads(evidence.backup_manifest_path.read_text(encoding="utf-8"))
    payload["databases"][0]["database"] = "paper.db"
    evidence.backup_manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    evidence.backup_manifest_path.chmod(0o600)

    report = require_private_write_readiness(
        replace(evidence, database_path=paper_database),
        now=NOW,
    )

    assert report.ready is True


def test_unrecognized_database_name_fails_closed(tmp_path):
    evidence = _ready_evidence(tmp_path)
    unexpected = evidence.database_path.with_name("other.db")
    evidence.database_path.rename(unexpected)

    report = assess_private_write_readiness(
        replace(evidence, database_path=unexpected),
        now=NOW,
    )

    assert "source_database_private" in report.failed_codes
    assert "source_database_integrity" in report.failed_codes


@pytest.mark.parametrize(
    "field,code",
    [
        ("api_writer_enabled", "api_writer_enabled"),
        ("client_writes_enabled", "client_writes_enabled"),
        ("consumer_executor_enabled", "consumer_executor_enabled"),
        ("user_approval_required", "user_approval_required"),
        ("direct_db_fallback_disabled", "direct_db_fallback_disabled"),
        ("rollback_verified", "rollback_verified"),
    ],
)
def test_each_activation_and_safety_gate_fails_closed(tmp_path, field, code):
    evidence = replace(_ready_evidence(tmp_path), **{field: False})

    report = assess_private_write_readiness(evidence, now=NOW)

    assert report.ready is False
    assert code in report.failed_codes


@pytest.mark.parametrize(
    "owners",
    [(), ("telegram",), ("private-data-api", "telegram"), ("private-data-api",) * 2],
)
def test_writer_ownership_must_be_exactly_one_private_api(tmp_path, owners):
    evidence = replace(_ready_evidence(tmp_path), writer_owners=owners)
    report = assess_private_write_readiness(evidence, now=NOW)
    assert report.ready is False
    assert "single_writer_owner" in report.failed_codes


def test_stale_backup_blocks_readiness(tmp_path):
    evidence = _ready_evidence(tmp_path)
    payload = json.loads(evidence.backup_manifest_path.read_text(encoding="utf-8"))
    payload["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    evidence.backup_manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    evidence.backup_manifest_path.chmod(0o600)

    report = assess_private_write_readiness(evidence, now=NOW)

    assert "backup_fresh" in report.failed_codes


def test_tampered_backup_hash_blocks_readiness(tmp_path):
    evidence = _ready_evidence(tmp_path)
    payload = json.loads(evidence.backup_manifest_path.read_text(encoding="utf-8"))
    payload["databases"][0]["sha256"] = "0" * 64
    evidence.backup_manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    evidence.backup_manifest_path.chmod(0o600)

    report = assess_private_write_readiness(evidence, now=NOW)

    assert "verified_backup_database" in report.failed_codes


def test_corrupt_source_database_blocks_integrity(tmp_path):
    evidence = _ready_evidence(tmp_path)
    evidence.database_path.write_bytes(b"not a sqlite database")
    evidence.database_path.chmod(0o600)

    report = assess_private_write_readiness(evidence, now=NOW)

    assert "source_database_integrity" in report.failed_codes


def test_manifest_traversal_is_rejected_without_opening_external_file(tmp_path):
    evidence = _ready_evidence(tmp_path)
    payload = json.loads(evidence.backup_manifest_path.read_text(encoding="utf-8"))
    payload["databases"][0]["backup_file"] = "../assistant.db"
    evidence.backup_manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    evidence.backup_manifest_path.chmod(0o600)

    report = assess_private_write_readiness(evidence, now=NOW)

    assert "verified_backup_manifest" in report.failed_codes
    assert "verified_backup_database" in report.failed_codes


def test_failure_exception_contains_codes_not_private_paths(tmp_path):
    evidence = replace(
        _ready_evidence(tmp_path),
        client_writes_enabled=False,
        rollback_verified=False,
    )

    with pytest.raises(PrivateWriteReadinessError) as exc:
        require_private_write_readiness(evidence, now=NOW)

    assert exc.value.failed_codes == ("client_writes_enabled", "rollback_verified")
    assert str(tmp_path) not in str(exc.value)


def test_non_boolean_activation_value_is_rejected(tmp_path):
    evidence = _ready_evidence(tmp_path)
    with pytest.raises(TypeError, match="flags must be bool"):
        replace(evidence, api_writer_enabled=1)


def test_activation_permit_is_issued_only_from_ready_evidence(tmp_path):
    evidence = _ready_evidence(tmp_path)

    permit = issue_private_write_activation_permit(evidence, now=NOW)

    assert len(permit.fingerprint) == 64
    assert str(tmp_path) not in repr(permit)
    assert require_private_write_activation_permit(
        permit,
        database_path=evidence.database_path,
    ) is permit


def test_activation_permit_cannot_be_constructed_or_replaced_by_report(tmp_path):
    evidence = _ready_evidence(tmp_path)
    report = require_private_write_readiness(evidence, now=NOW)

    with pytest.raises(PrivateWriteActivationError, match="cannot be constructed"):
        PrivateWriteActivationPermit(evidence.database_path, "0" * 64, object())
    with pytest.raises(PrivateWriteActivationError, match="permit is required"):
        require_private_write_activation_permit(report)


def test_activation_permit_is_bound_to_database_scope(tmp_path):
    evidence = _ready_evidence(tmp_path / "first")
    permit = issue_private_write_activation_permit(evidence, now=NOW)
    other_database = (tmp_path / "other" / "assistant.db").resolve()

    with pytest.raises(PrivateWriteActivationError, match="scope mismatch"):
        require_private_write_activation_permit(
            permit,
            database_path=other_database,
        )


def test_activation_permits_from_different_evidence_do_not_match(tmp_path):
    first = issue_private_write_activation_permit(
        _ready_evidence(tmp_path / "first"),
        now=NOW,
    )
    second = issue_private_write_activation_permit(
        _ready_evidence(tmp_path / "second"),
        now=NOW,
    )

    with pytest.raises(PrivateWriteActivationError, match="do not match"):
        require_private_write_activation_permit(first, matching_permit=second)


# ─── 고정 핀과 신선도 분리 (2026-08-28) ──────────────
#
# 이전에는 신선도를 **번들이 고정한 manifest**에서 쟀다. 고정 manifest의 내용
# 해시가 활성화 지문에 들어가므로 백업을 새로 뜨면 번들을 재발급해야 하는데,
# 신선도 한도는 24시간이고 백업 스케줄은 주 1회였다. 그래서 재시작하는 순간
# 텔레그램 봇과 Private API가 함께 죽었다(2026-08-28 실제 발생).

from private_write_readiness import newest_backup_age  # noqa: E402


def _extra_snapshot(evidence, name, created_at):
    """같은 백업 루트에 검증 완료 스냅샷을 하나 더 만든다."""
    root = evidence.backup_manifest_path.parent.parent
    src = evidence.backup_manifest_path
    payload = json.loads(src.read_text(encoding="utf-8"))
    payload["created_at"] = created_at.isoformat()
    snapshot = root / name
    snapshot.mkdir(mode=0o700)
    manifest = snapshot / "manifest.json"
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    manifest.chmod(0o600)
    return manifest


def test_a_stale_pin_still_passes_when_a_recent_backup_exists(tmp_path):
    """핀은 '이 백업으로 롤백이 검증됐다'는 증거지, '최근'이라는 뜻이 아니다."""
    evidence = _ready_evidence(tmp_path)
    old = json.loads(evidence.backup_manifest_path.read_text(encoding="utf-8"))
    old["created_at"] = (NOW - timedelta(days=30)).isoformat()
    evidence.backup_manifest_path.write_text(json.dumps(old), encoding="utf-8")

    assert assess_private_write_readiness(evidence, now=NOW).ready is False

    _extra_snapshot(evidence, "recent", NOW - timedelta(hours=2))
    assert assess_private_write_readiness(evidence, now=NOW).ready is True


def test_a_fresh_pin_alone_is_not_enough_if_nothing_recent_exists(tmp_path):
    """반대 방향도 확인한다 — 최근 백업이 사라지면 통과하면 안 된다."""
    evidence = _ready_evidence(tmp_path)
    for manifest in evidence.backup_manifest_path.parent.parent.glob("*/manifest.json"):
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["created_at"] = (NOW - timedelta(days=9)).isoformat()
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    report = assess_private_write_readiness(evidence, now=NOW)
    assert report.ready is False and "backup_fresh" in report.failed_codes


def test_freshness_takes_the_newest_backup_not_the_first_found(tmp_path):
    evidence = _ready_evidence(tmp_path)
    _extra_snapshot(evidence, "aaa-오래됨", NOW - timedelta(days=40))
    _extra_snapshot(evidence, "zzz-최근", NOW - timedelta(hours=1))
    age = newest_backup_age(
        evidence.backup_manifest_path.parent.parent, NOW,
        database_name=evidence.database_path.name)
    assert age < timedelta(hours=2)


def test_an_unverified_snapshot_does_not_count_as_fresh(tmp_path):
    """복구 검증을 통과하지 못한 백업은 '최근 백업이 있다'의 근거가 될 수 없다."""
    evidence = _ready_evidence(tmp_path)
    manifest = _extra_snapshot(evidence, "unverified", NOW - timedelta(hours=1))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["all_restore_verified"] = False
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    old = json.loads(evidence.backup_manifest_path.read_text(encoding="utf-8"))
    old["created_at"] = (NOW - timedelta(days=30)).isoformat()
    evidence.backup_manifest_path.write_text(json.dumps(old), encoding="utf-8")

    assert assess_private_write_readiness(evidence, now=NOW).ready is False


def test_a_snapshot_for_another_database_does_not_count(tmp_path):
    """paper.db 백업만 최근이라면 assistant.db는 여전히 오래된 것이다."""
    evidence = _ready_evidence(tmp_path)
    manifest = _extra_snapshot(evidence, "다른DB", NOW - timedelta(hours=1))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for item in payload["databases"]:
        item["database"] = "paper.db"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    old = json.loads(evidence.backup_manifest_path.read_text(encoding="utf-8"))
    old["created_at"] = (NOW - timedelta(days=30)).isoformat()
    evidence.backup_manifest_path.write_text(json.dumps(old), encoding="utf-8")

    assert assess_private_write_readiness(evidence, now=NOW).ready is False


def test_no_backup_root_at_all_is_not_fresh(tmp_path):
    assert newest_backup_age(tmp_path / "없음", NOW, database_name="assistant.db") is None


def test_a_future_dated_backup_is_rejected(tmp_path):
    """시계가 틀렸거나 조작된 백업을 '아주 신선함'으로 받아들이면 안 된다."""
    evidence = _ready_evidence(tmp_path)
    for manifest in evidence.backup_manifest_path.parent.parent.glob("*/manifest.json"):
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["created_at"] = (NOW + timedelta(days=1)).isoformat()
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    report = assess_private_write_readiness(evidence, now=NOW)
    assert report.ready is False and "backup_fresh" in report.failed_codes


def test_one_future_dated_backup_does_not_invalidate_a_good_one(tmp_path):
    """시계가 틀린 스냅샷 하나가 멀쩡한 최근 백업까지 무효로 만들면 안 된다."""
    evidence = _ready_evidence(tmp_path)
    _extra_snapshot(evidence, "시계틀림", NOW + timedelta(days=1))
    _extra_snapshot(evidence, "정상", NOW - timedelta(hours=1))
    old = json.loads(evidence.backup_manifest_path.read_text(encoding="utf-8"))
    old["created_at"] = (NOW - timedelta(days=30)).isoformat()
    evidence.backup_manifest_path.write_text(json.dumps(old), encoding="utf-8")
    assert assess_private_write_readiness(evidence, now=NOW).ready is True
