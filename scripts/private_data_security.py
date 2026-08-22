#!/usr/bin/env python3
"""Private 데이터 권한 고정과 SQLite 온라인 백업.

백업은 Git 저장소 밖의 FileVault 보호 로컬 경로에 누적한다. 실행 중인 SQLite에는
``Connection.backup``을 사용하며, 만들어진 사본을 메모리 DB로 복구해 무결성과
스키마·행 수를 다시 확인한다. 이 도구는 기존 백업을 자동 삭제하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from storage_paths import PRIVATE_V1_LAYOUT, StoragePaths, get_paths


PRIVATE_DB_NAMES = ("paper.db", "schedule.db", "private.db")
PRIVATE_CACHE_FILES = (
    "intraday_peaks.json",
    "ipo_weekly_last.json",
    "news_seen.json",
    "perf_history.json",
    "phase_by_month.json",
    "quant_rebalance_last.json",
    "signal_last.json",
    "news_digest_last.json",
)


@dataclass(frozen=True)
class SecurityPaths:
    project: Path
    vault: Path
    backup_root: Path

    @property
    def data(self) -> Path:
        return self.project / "data"

    @property
    def storage(self) -> StoragePaths:
        return get_paths(self.project)


def default_paths() -> SecurityPaths:
    project = Path(__file__).resolve().parent.parent
    backup_default = project.parent / "private-backups" / "ai-agent"
    return SecurityPaths(
        project=project,
        vault=project.parent / "obsidian-vault",
        backup_root=Path(os.getenv("AI_AGENT_PRIVATE_BACKUP_DIR", backup_default)).expanduser(),
    )


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _secure_file(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    changed = _mode(path) != 0o600
    path.chmod(0o600)
    return changed


def _secure_dir(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    changed = _mode(path) != 0o700
    path.chmod(0o700)
    return changed


def _secure_tree(root: Path) -> tuple[int, int]:
    if not root.exists() or root.is_symlink():
        return 0, 0
    dirs_changed = int(_secure_dir(root))
    files_changed = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            dirs_changed += int(_secure_dir(path))
        elif path.is_file():
            files_changed += int(_secure_file(path))
    return files_changed, dirs_changed


def _private_standalone_files(paths: SecurityPaths) -> Iterable[Path]:
    storage = paths.storage
    yield paths.project / ".env"
    for db_path in _private_database_paths(paths):
        yield db_path
        yield from db_path.parent.glob(f"{db_path.name}-*")
    yield storage.action_schedules
    yield storage.raw_processed
    yield storage.watch_raw_log
    for name in PRIVATE_CACHE_FILES:
        yield storage.private_state_file(name)
    yield from storage.private_state_dir.glob("myquant_scan_*.json")
    yield from paths.data.glob(".fuse_hidden*")


def _private_database_paths(paths: SecurityPaths) -> list[Path]:
    storage = paths.storage
    ordered = (storage.paper_db, storage.schedule_db, storage.watchlist_db)
    return list(dict.fromkeys(ordered))


def secure_permissions(paths: SecurityPaths | None = None) -> dict[str, int]:
    """Private 파일 600, Private 디렉터리 700을 적용한다."""
    paths = paths or default_paths()
    files_changed = 0
    dirs_changed = 0

    for path in _private_standalone_files(paths):
        files_changed += int(_secure_file(path))

    tree_roots = [paths.vault / "raw", paths.vault / "wiki", paths.backup_root]
    if paths.storage.layout == PRIVATE_V1_LAYOUT:
        tree_roots.append(paths.storage.private_root)
    else:
        tree_roots.extend(
            (paths.storage.logs_dir, paths.storage.rag_dir, paths.storage.reports_dir)
        )

    for root in tree_roots:
        file_count, dir_count = _secure_tree(root)
        files_changed += file_count
        dirs_changed += dir_count

    return {"files_changed": files_changed, "dirs_changed": dirs_changed}


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _database_signature(con: sqlite3.Connection) -> dict:
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity_check 실패: {integrity}")

    schema_rows = con.execute(
        """SELECT type, name, tbl_name, COALESCE(sql, '')
           FROM sqlite_master
           WHERE name NOT LIKE 'sqlite_%'
           ORDER BY type, name"""
    ).fetchall()
    schema_json = json.dumps(schema_rows, ensure_ascii=False, separators=(",", ":"))
    tables = [row[1] for row in schema_rows if row[0] == "table"]
    counts = {
        table: con.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}").fetchone()[0]
        for table in tables
    }
    return {
        "integrity": integrity,
        "schema_sha256": hashlib.sha256(schema_json.encode("utf-8")).hexdigest(),
        "table_counts": counts,
    }


def _create_private_file(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)


def backup_database(source: Path, target: Path) -> dict:
    """실행 중인 SQLite를 target으로 백업하고 메모리 복구까지 검증한다."""
    if not source.is_file():
        raise FileNotFoundError(source)
    _create_private_file(target)

    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            dst.commit()

    target.chmod(0o600)
    with sqlite3.connect(target) as stored:
        stored_signature = _database_signature(stored)
        restored = sqlite3.connect(":memory:")
        try:
            stored.backup(restored)
            restored_signature = _database_signature(restored)
        finally:
            restored.close()

    if stored_signature != restored_signature:
        raise RuntimeError(f"메모리 복구 결과 불일치: {source.name}")

    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "database": source.name,
        "backup_file": target.name,
        "bytes": target.stat().st_size,
        "sha256": digest.hexdigest(),
        **stored_signature,
        "restore_verified": True,
    }


def _make_snapshot_dir(root: Path, stamp: str) -> Path:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    snapshot = root / stamp
    snapshot.mkdir(mode=0o700)
    snapshot.chmod(0o700)
    return snapshot


def _write_private_json(path: Path, payload: dict) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def backup_all(
    paths: SecurityPaths | None = None,
    *,
    stamp: str | None = None,
) -> tuple[Path, dict]:
    paths = paths or default_paths()
    stamp = stamp or datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    snapshot = _make_snapshot_dir(paths.backup_root, stamp)
    results = [
        backup_database(source, snapshot / source.name)
        for source in _private_database_paths(paths)
    ]
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "storage": "FileVault-protected local volume",
        "retention": "no automatic deletion",
        "databases": results,
        "all_restore_verified": all(item["restore_verified"] for item in results),
    }
    _write_private_json(snapshot / "manifest.json", manifest)
    secure_permissions(paths)
    return snapshot, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("secure", "backup", "all"), nargs="?", default="all")
    args = parser.parse_args()
    os.umask(0o077)

    paths = default_paths()
    if args.command in {"secure", "all"}:
        result = secure_permissions(paths)
        print(
            f"권한 적용 완료: 파일 {result['files_changed']}개, "
            f"디렉터리 {result['dirs_changed']}개 변경"
        )
    if args.command in {"backup", "all"}:
        snapshot, manifest = backup_all(paths)
        print(
            f"온라인 백업·복구 검증 완료: {snapshot} "
            f"({len(manifest['databases'])}개 DB)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
