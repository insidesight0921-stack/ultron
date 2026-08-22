from __future__ import annotations

import hashlib
import json
import sqlite3

import migrate_storage as ms
import private_data_security as pds
from storage_paths import PRIVATE_V1_LAYOUT, get_paths


def _create_db(path, table, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT)')
        con.executemany(f'INSERT INTO "{table}" (id, value) VALUES (?, ?)', rows)
        con.commit()


def _project(tmp_path):
    project = tmp_path / "ai-agent"
    data = project / "data"
    _create_db(data / "paper.db", "trades", [(1, "paper")])
    _create_db(data / "schedule.db", "events", [(1, "meeting"), (2, "report")])
    _create_db(data / "private.db", "watchlist", [(1, "005930")])
    (data / "action_schedules.json").write_text('{"schedules":[]}', encoding="utf-8")
    (data / "raw_processed.json").write_text("{}", encoding="utf-8")
    (data / "watch_raw.log").write_text("log", encoding="utf-8")
    cache = data / "cache"
    cache.mkdir()
    (cache / "signal_last.json").write_text("{}", encoding="utf-8")
    (cache / "corp_codes.json").write_text('{"005930":"00126380"}', encoding="utf-8")
    ohlcv = cache / "ohlcv"
    ohlcv.mkdir()
    (ohlcv / "005930_20260822.json").write_text('{"close":[1]}', encoding="utf-8")
    rag = data / "lancedb" / "wiki_chunks.lance"
    rag.mkdir(parents=True)
    (rag / "data.bin").write_bytes(b"rag")
    logs = data / "logs"
    logs.mkdir()
    (logs / "telegram.out.log").write_text("started", encoding="utf-8")
    samples = data / "ipo_samples"
    samples.mkdir()
    (samples / "kind_raw.html").write_text("<html></html>", encoding="utf-8")
    return project


def _backup(project, tmp_path):
    security_paths = pds.SecurityPaths(
        project,
        project.parent / "obsidian-vault",
        tmp_path / "backups",
    )
    snapshot, _ = pds.backup_all(security_paths, stamp="snapshot")
    return snapshot / "manifest.json"


def test_plan_is_read_only(tmp_path):
    project = _project(tmp_path)
    plan = ms.build_plan(project)
    assert plan["active_layout"] == "legacy"
    assert "signal_last.json" in plan["private_cache_entries"]
    assert "corp_codes.json" in plan["shareable_cache_entries"]
    assert not (project / "data" / "private").exists()
    assert not (project / "data" / "shareable").exists()


def test_migrate_copies_verifies_and_preserves_legacy(tmp_path):
    project = _project(tmp_path)
    backup_manifest = _backup(project, tmp_path)

    result = ms.migrate(project, backup_manifest, services_stopped=True)

    paths = get_paths(project)
    assert paths.layout == PRIVATE_V1_LAYOUT
    assert result["manifest"]["legacy_preserved"] is True
    assert (project / "data" / "paper.db").is_file()
    assert (project / "data" / "schedule.db").is_file()
    assert (project / "data" / "private.db").is_file()
    with sqlite3.connect(paths.paper_db) as con:
        assert con.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    with sqlite3.connect(paths.schedule_db) as con:
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1
    assert paths.private_state_file("signal_last.json").is_file()
    assert paths.shareable_cache_file("corp_codes.json").is_file()
    assert (paths.shareable_cache_dir / "ohlcv" / "005930_20260822.json").is_file()
    assert (paths.rag_dir / "wiki_chunks.lance" / "data.bin").is_file()
    assert (paths.private_root / "migration-manifest.json").is_file()
    assert (project / "data" / "storage-layout.json").stat().st_mode & 0o777 == 0o600
    expected_hash = hashlib.sha256(paths.schedule_db.read_bytes()).hexdigest()
    assert result["manifest"]["assistant"]["assistant_sha256"] == expected_hash


def test_unknown_cache_asset_blocks_before_writes(tmp_path):
    project = _project(tmp_path)
    (project / "data" / "cache" / "unknown.json").write_text("{}", encoding="utf-8")
    try:
        ms.build_plan(project)
    except ms.MigrationError as exc:
        assert "분류되지 않은" in str(exc)
    else:
        raise AssertionError("unknown cache must block migration")
    assert not (project / "data" / "private").exists()


def test_apply_requires_services_stopped_and_valid_backup(tmp_path):
    project = _project(tmp_path)
    backup_manifest = _backup(project, tmp_path)
    try:
        ms.migrate(project, backup_manifest, services_stopped=False)
    except ms.MigrationError as exc:
        assert "서비스 중지" in str(exc)
    else:
        raise AssertionError("service confirmation must be required")

    payload = json.loads(backup_manifest.read_text(encoding="utf-8"))
    payload["all_restore_verified"] = False
    invalid = backup_manifest.parent / "invalid.json"
    invalid.write_text(json.dumps(payload), encoding="utf-8")
    try:
        ms.migrate(project, invalid, services_stopped=True)
    except ms.MigrationError as exc:
        assert "복구 검증" in str(exc)
    else:
        raise AssertionError("invalid backup must block migration")


def test_existing_layout_file_blocks_before_writes(tmp_path):
    project = _project(tmp_path)
    layout = project / "data" / "storage-layout.json"
    layout.write_text('{"layout":"legacy"}', encoding="utf-8")
    try:
        ms.build_plan(project)
    except ms.MigrationError as exc:
        assert "기존 storage layout" in str(exc)
    else:
        raise AssertionError("existing layout file must block migration")
    assert not (project / "data" / "private").exists()
