from __future__ import annotations

import json
import sqlite3

import private_data_security as pds


def _paths(tmp_path):
    project = tmp_path / "ai-agent"
    vault = tmp_path / "obsidian-vault"
    return pds.SecurityPaths(project, vault, tmp_path / "private-backups" / "ai-agent")


def _make_db(path, rows=("a", "b")):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE sample(value TEXT)")
        con.executemany("INSERT INTO sample(value) VALUES (?)", [(row,) for row in rows])
        con.commit()


def test_secure_permissions_targets_only_private_assets(tmp_path):
    paths = _paths(tmp_path)
    private = paths.data / "action_schedules.json"
    private.parent.mkdir(parents=True)
    private.write_text("{}", encoding="utf-8")
    public = paths.data / "cache" / "corp_codes.json"
    public.parent.mkdir()
    public.write_text("{}", encoding="utf-8")
    wiki = paths.vault / "wiki" / "principle.md"
    wiki.parent.mkdir(parents=True)
    wiki.write_text("private", encoding="utf-8")
    private.chmod(0o644)
    public.chmod(0o644)
    wiki.chmod(0o644)

    result = pds.secure_permissions(paths)

    assert result["files_changed"] == 2
    assert private.stat().st_mode & 0o777 == 0o600
    assert wiki.stat().st_mode & 0o777 == 0o600
    assert wiki.parent.stat().st_mode & 0o777 == 0o700
    assert public.stat().st_mode & 0o777 == 0o644


def test_online_backup_and_memory_restore(tmp_path):
    paths = _paths(tmp_path)
    for index, name in enumerate(pds.PRIVATE_DB_NAMES, start=1):
        _make_db(paths.data / name, rows=tuple(str(i) for i in range(index)))

    snapshot, manifest = pds.backup_all(paths, stamp="20260822T180000+0900")

    assert manifest["all_restore_verified"] is True
    assert len(manifest["databases"]) == 3
    assert snapshot.stat().st_mode & 0o777 == 0o700
    for result in manifest["databases"]:
        backup = snapshot / result["backup_file"]
        assert backup.stat().st_mode & 0o777 == 0o600
        assert result["integrity"] == "ok"
        assert result["restore_verified"] is True
    saved_manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert saved_manifest["all_restore_verified"] is True
    assert (snapshot / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_backup_includes_uncheckpointed_wal_rows(tmp_path):
    paths = _paths(tmp_path)
    source = paths.data / "paper.db"
    source.parent.mkdir(parents=True)
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE sample(value TEXT)")
        writer.execute("INSERT INTO sample(value) VALUES ('live')")
        writer.commit()
        target_dir = paths.backup_root / "snapshot"
        target_dir.mkdir(parents=True)
        result = pds.backup_database(source, target_dir / "paper.db")
    finally:
        writer.close()

    assert result["table_counts"] == {"sample": 1}
    assert result["restore_verified"] is True


def test_private_v1_uses_one_assistant_database(tmp_path):
    paths = _paths(tmp_path)
    config = paths.data / "storage-layout.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"layout": "private-v1"}), encoding="utf-8")
    storage = paths.storage
    _make_db(storage.paper_db, rows=("paper",))
    _make_db(storage.schedule_db, rows=("assistant",))

    snapshot, manifest = pds.backup_all(paths, stamp="20260822T190000+0900")

    assert [item["database"] for item in manifest["databases"]] == [
        "paper.db",
        "assistant.db",
    ]
    assert storage.private_root.stat().st_mode & 0o777 == 0o700
    assert (snapshot / "assistant.db").stat().st_mode & 0o777 == 0o600
