"""
pytest fixtures + sys.path 세팅.

scripts/ 모듈을 그대로 import할 수 있게 한다.
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture
def private_write_readiness_evidence_factory(tmp_path):
    """Build real isolated DB/backup evidence without changing runtime state."""
    from private_data_security import backup_database
    from private_write_readiness import PrivateWriteReadinessEvidence

    counter = 0

    def factory(database_path=None):
        nonlocal counter
        counter += 1
        database = Path(
            database_path or tmp_path / f"permit-{counter}" / "assistant.db"
        ).resolve()
        database.parent.mkdir(parents=True, exist_ok=True)
        if not database.exists():
            with sqlite3.connect(database):
                pass
        database.chmod(0o600)

        snapshot = (tmp_path / "permit-backups" / f"snapshot-{counter}").resolve()
        snapshot.mkdir(parents=True, mode=0o700)
        snapshot.parent.chmod(0o700)
        backup_result = backup_database(database, snapshot / database.name)
        manifest = snapshot / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "created_at": datetime.now().astimezone().isoformat(),
                    "all_restore_verified": True,
                    "databases": [backup_result],
                }
            ),
            encoding="utf-8",
        )
        manifest.chmod(0o600)
        return PrivateWriteReadinessEvidence(
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

    return factory


@pytest.fixture
def private_write_permit_factory(private_write_readiness_evidence_factory):
    """Issue real readiness-backed permits for isolated write contract tests."""
    from private_write_readiness import issue_private_write_activation_permit

    def factory(database_path=None):
        return issue_private_write_activation_permit(
            private_write_readiness_evidence_factory(database_path)
        )

    return factory

# ─── 운영 데이터 보호 (2026-08-27 사고 대응) ─────────────────────────────
#
# `signal_bot.scan()`의 log_path 기본값이 운영 경로였던 탓에, 테스트가 매 실행마다
# 운영 신호 기록에 가짜 신호를 써 넣고 있었다(100건 중 86건). 오염이 조용했기 때문에
# 그 데이터로 잘못된 분석까지 만들었다.
#
# 아래 훅은 테스트 세션이 운영 상태 파일·볼트 노트를 건드렸는지 감시한다.
# 어떤 테스트가 범인인지는 못 짚지만, **조용히 넘어가지는 않는다.**
# 임시 경로(tmp_path)를 쓰는 정상적인 테스트는 여기에 걸리지 않는다.

_GUARDED_DIRS = [
    ROOT / "data" / "private" / "state",
    ROOT.parent / "obsidian-vault",
]
_guard_snapshot: dict = {}


def _guard_scan() -> dict:
    out = {}
    for base in _GUARDED_DIRS:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_dir() or ".git" in path.parts:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            out[str(path)] = (st.st_size, st.st_mtime_ns)
    return out


def pytest_sessionstart(session):
    _guard_snapshot.clear()
    _guard_snapshot.update(_guard_scan())


def pytest_sessionfinish(session, exitstatus):
    if not _guard_snapshot:
        return
    after = _guard_scan()
    touched = sorted(
        [p for p in after if p not in _guard_snapshot or _guard_snapshot[p] != after[p]]
        + [p for p in _guard_snapshot if p not in after]
    )
    if not touched:
        return
    rel = [str(Path(p).relative_to(ROOT.parent)) for p in touched[:20]]
    print("\n" + "=" * 70)
    print("❌ 테스트가 운영 데이터를 수정했습니다 — 임시 경로(tmp_path)를 쓰세요:")
    for r in rel:
        print("   ", r)
    if len(touched) > len(rel):
        print(f"    ... 외 {len(touched) - len(rel)}건")
    print("=" * 70)
    session.exitstatus = 1
