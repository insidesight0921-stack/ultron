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
