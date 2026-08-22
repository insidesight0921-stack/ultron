#!/usr/bin/env python3
"""legacy 데이터 레이아웃을 private-v1으로 비파괴 복제·검증한다.

기본 명령 ``plan``은 읽기 전용이다. 실제 적용은 복구 검증을 통과한 최근 백업
manifest와 서비스 중지 확인 플래그가 모두 있어야 시작한다. 원본 파일은 삭제하거나
덮어쓰지 않으며, 레이아웃 설정은 모든 검증이 끝난 뒤 마지막에 기록한다.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from private_data_security import (
    PRIVATE_CACHE_FILES,
    PRIVATE_DB_NAMES,
    backup_database,
    database_signature,
    verify_database,
)
from storage_paths import (
    LAYOUT_FILE_NAME,
    LEGACY_LAYOUT,
    PRIVATE_V1_LAYOUT,
    StoragePaths,
    get_paths,
)


PUBLIC_CACHE_PATTERNS = (
    "corp_codes.json",
    "dart_metrics_cache.json",
    "etf_map_*.json",
    "ticker_map_*.json",
    "universe_*.json",
)
PRIVATE_CACHE_PATTERNS = (*PRIVATE_CACHE_FILES, "myquant_scan_*.json")
PUBLIC_CACHE_DIRS = {"ohlcv"}
REQUIRED_LEGACY_DATABASES = set(PRIVATE_DB_NAMES)


class MigrationError(RuntimeError):
    pass


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def classify_cache(cache_dir: Path) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {"private": [], "shareable": []}
    if not cache_dir.exists():
        return result
    for path in sorted(cache_dir.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise MigrationError(f"캐시 symlink는 허용하지 않음: {path}")
        if path.is_dir() and path.name in PUBLIC_CACHE_DIRS:
            result["shareable"].append(path)
        elif path.is_file() and _matches(path.name, PRIVATE_CACHE_PATTERNS):
            result["private"].append(path)
        elif path.is_file() and _matches(path.name, PUBLIC_CACHE_PATTERNS):
            result["shareable"].append(path)
        else:
            raise MigrationError(f"분류되지 않은 cache 자산: {path.name}")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _copy_file(source: Path, target: Path) -> dict:
    if source.is_symlink() or not source.is_file():
        raise MigrationError(f"일반 파일이 아님: {source}")
    if target.exists():
        raise MigrationError(f"대상 파일이 이미 존재함: {target}")
    _ensure_private_dir(target.parent)
    source_hash = _sha256(source)
    shutil.copy2(source, target)
    target.chmod(0o600)
    target_hash = _sha256(target)
    if source_hash != target_hash:
        raise MigrationError(f"복사 해시 불일치: {source}")
    return {"source": str(source), "target": str(target), "sha256": source_hash}


def _copy_tree(source: Path, target: Path) -> list[dict]:
    if source.is_symlink() or not source.is_dir():
        raise MigrationError(f"일반 디렉터리가 아님: {source}")
    if target.exists():
        raise MigrationError(f"대상 디렉터리가 이미 존재함: {target}")
    _ensure_private_dir(target)
    copied: list[dict] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise MigrationError(f"트리 내부 symlink는 허용하지 않음: {path}")
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            _ensure_private_dir(destination)
        elif path.is_file():
            copied.append(_copy_file(path, destination))
    return copied


def _write_json_exclusive(path: Path, payload: dict) -> None:
    _ensure_private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def validate_backup_manifest(path: Path, *, max_age_hours: int = 24) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise MigrationError(f"백업 manifest 읽기 실패: {path}") from exc
    if payload.get("all_restore_verified") is not True:
        raise MigrationError("복구 검증을 통과한 백업이 아님")

    try:
        created = datetime.fromisoformat(payload["created_at"])
        now = datetime.now().astimezone()
        age = now - created.astimezone(now.tzinfo)
    except (KeyError, TypeError, ValueError) as exc:
        raise MigrationError("백업 생성시각이 유효하지 않음") from exc
    if age < timedelta(minutes=-5) or age > timedelta(hours=max_age_hours):
        raise MigrationError(f"최근 {max_age_hours}시간 이내 백업이 아님")

    databases = payload.get("databases")
    if not isinstance(databases, list):
        raise MigrationError("백업 DB 목록이 없음")
    names = {str(item.get("database")) for item in databases if isinstance(item, dict)}
    if not REQUIRED_LEGACY_DATABASES.issubset(names):
        raise MigrationError("legacy DB 3개가 모두 포함된 백업이 아님")

    for item in databases:
        if not isinstance(item, dict) or item.get("restore_verified") is not True:
            raise MigrationError("DB별 복구 검증 정보가 올바르지 않음")
        backup_file = path.parent / str(item.get("backup_file", ""))
        if not backup_file.is_file() or _sha256(backup_file) != item.get("sha256"):
            raise MigrationError(f"백업 파일 해시 불일치: {backup_file.name}")
        verify_database(backup_file)
    return payload


def build_plan(project: Path) -> dict:
    project = project.resolve()
    active = get_paths(project)
    legacy = StoragePaths(project, LEGACY_LAYOUT)
    target = StoragePaths(project, PRIVATE_V1_LAYOUT)
    if active.layout != LEGACY_LAYOUT:
        raise MigrationError(f"현재 레이아웃이 legacy가 아님: {active.layout}")
    layout_file = project / "data" / LAYOUT_FILE_NAME
    if layout_file.exists():
        raise MigrationError("기존 storage layout 설정 파일이 있어 자동 전환할 수 없음")
    if target.private_root.exists() or target.shareable_root.exists():
        raise MigrationError("private-v1 대상 디렉터리가 이미 존재함")

    database_paths = (legacy.paper_db, legacy.schedule_db, legacy.watchlist_db)
    missing = [str(path) for path in database_paths if not path.is_file()]
    if missing:
        raise MigrationError(f"필수 legacy DB 없음: {', '.join(missing)}")

    cache = classify_cache(legacy.shareable_cache_dir)
    return {
        "active_layout": active.layout,
        "target_layout": PRIVATE_V1_LAYOUT,
        "databases": [str(path) for path in database_paths],
        "private_cache_entries": [path.name for path in cache["private"]],
        "shareable_cache_entries": [path.name for path in cache["shareable"]],
        "target_private": str(target.private_root),
        "target_shareable": str(target.shareable_root),
    }


def _merge_assistant_database(schedule_db: Path, watchlist_db: Path, target: Path) -> dict:
    backup_database(schedule_db, target)
    with sqlite3.connect(watchlist_db) as watch:
        table_names = {
            row[0]
            for row in watch.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if table_names != {"watchlist"}:
            raise MigrationError(f"예상하지 못한 watchlist DB 테이블: {sorted(table_names)}")
        table_sql = watch.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='watchlist'"
        ).fetchone()[0]
        columns = [row[1] for row in watch.execute("PRAGMA table_info(watchlist)")]
        rows = watch.execute("SELECT * FROM watchlist").fetchall()
        index_sql = [
            row[0]
            for row in watch.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='watchlist' AND sql IS NOT NULL"
            )
        ]

    quoted_columns = ", ".join('"' + col.replace('"', '""') + '"' for col in columns)
    placeholders = ", ".join("?" for _ in columns)
    with sqlite3.connect(target) as assistant:
        if assistant.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchlist'"
        ).fetchone():
            raise MigrationError("assistant.db에 watchlist 테이블이 이미 존재함")
        assistant.execute(table_sql)
        if rows:
            assistant.executemany(
                f"INSERT INTO watchlist ({quoted_columns}) VALUES ({placeholders})",
                rows,
            )
        for sql in index_sql:
            assistant.execute(sql)
        assistant.commit()

    verification = verify_database(target)
    with sqlite3.connect(schedule_db) as schedule:
        schedule_signature = database_signature(schedule)
        schedule_counts = schedule_signature["table_counts"]
    with sqlite3.connect(watchlist_db) as watch:
        watch_count = watch.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    target_counts = verification["table_counts"]
    for table, count in schedule_counts.items():
        if target_counts.get(table) != count:
            raise MigrationError(f"assistant.db 일정 테이블 행 수 불일치: {table}")
    if target_counts.get("watchlist") != watch_count:
        raise MigrationError("assistant.db watchlist 행 수 불일치")
    return {
        "schedule_source": schedule_signature,
        "watchlist_source_count": watch_count,
        "assistant_verification": verification,
        "assistant_sha256": _sha256(target),
    }


def migrate(project: Path, backup_manifest: Path, *, services_stopped: bool) -> dict:
    if not services_stopped:
        raise MigrationError("서비스 중지 확인 없이는 적용할 수 없음")
    plan = build_plan(project)
    validate_backup_manifest(backup_manifest)

    project = project.resolve()
    legacy = StoragePaths(project, LEGACY_LAYOUT)
    target = StoragePaths(project, PRIVATE_V1_LAYOUT)
    _ensure_private_dir(target.private_root)
    _ensure_private_dir(target.shareable_root)

    paper_result = backup_database(legacy.paper_db, target.paper_db)
    assistant_result = _merge_assistant_database(
        legacy.schedule_db, legacy.watchlist_db, target.schedule_db
    )
    copied: list[dict] = []

    for source, destination in (
        (legacy.rag_dir, target.rag_dir),
        (legacy.logs_dir, target.logs_dir),
        (legacy.reports_dir, target.reports_dir),
        (legacy.shareable_samples_dir, target.shareable_samples_dir),
    ):
        if source.is_dir():
            copied.extend(_copy_tree(source, destination))

    for source, destination in (
        (legacy.action_schedules, target.action_schedules),
        (legacy.raw_processed, target.raw_processed),
        (legacy.watch_raw_log, target.watch_raw_log),
        (legacy.dart_finance_validation, target.dart_finance_validation),
        (legacy.ipo_demand_validation, target.ipo_demand_validation),
    ):
        if source.is_file():
            copied.append(_copy_file(source, destination))

    cache = classify_cache(legacy.shareable_cache_dir)
    for source in cache["private"]:
        destination = target.private_state_dir / source.name
        copied.extend(_copy_tree(source, destination) if source.is_dir() else [_copy_file(source, destination)])
    for source in cache["shareable"]:
        destination = target.shareable_cache_dir / source.name
        copied.extend(_copy_tree(source, destination) if source.is_dir() else [_copy_file(source, destination)])

    migration_manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_layout": LEGACY_LAYOUT,
        "target_layout": PRIVATE_V1_LAYOUT,
        "backup_manifest": str(backup_manifest.resolve()),
        "paper": paper_result,
        "assistant": assistant_result,
        "copied_files": copied,
        "legacy_preserved": True,
    }
    _write_json_exclusive(target.private_root / "migration-manifest.json", migration_manifest)
    _write_json_exclusive(
        project / "data" / LAYOUT_FILE_NAME,
        {
            "layout": PRIVATE_V1_LAYOUT,
            "activated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )
    return {"plan": plan, "manifest": migration_manifest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply"), nargs="?", default="plan")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--backup-manifest", type=Path)
    parser.add_argument("--confirm-services-stopped", action="store_true")
    args = parser.parse_args()

    try:
        if args.command == "plan":
            print(json.dumps(build_plan(args.project), ensure_ascii=False, indent=2))
            return 0
        if args.backup_manifest is None:
            parser.error("apply에는 --backup-manifest가 필요합니다")
        result = migrate(
            args.project,
            args.backup_manifest,
            services_stopped=args.confirm_services_stopped,
        )
        print(json.dumps(result["plan"], ensure_ascii=False, indent=2))
        print("private-v1 복제·검증·전환 완료. legacy 원본은 보존됨.")
        return 0
    except MigrationError as exc:
        parser.exit(2, f"오류: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
