from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

import paper_db
from private_data_api import create_app
from private_data_security import backup_database
from private_paper_write_candidate import (
    PAPER_RUNTIME_ROLES,
    PaperWriteCandidateError,
    generate_paper_write_activation_candidate,
)
from private_paper_write_cutover import PAPER_LEGACY_RUNTIME_WRITERS
from private_paper_write_readiness import (
    PAPER_TARGET_CALLER_POLICIES,
    PaperWriteReadinessEvidence,
)
from private_schedule_write_readiness import (
    SCHEDULE_NOTIFIER_OPERATIONS,
    SCHEDULE_NOTIFIER_OWNER,
    ScheduleWriteReadinessEvidence,
)
from private_write_readiness import PrivateWriteReadinessEvidence
from private_write_runtime import (
    BUNDLE_FILENAME,
    BUNDLE_PATH_ENV,
    PrivateWriteRuntimeError,
    load_private_write_runtime_bundle,
)

NOW = datetime.now().astimezone()
TOKEN = "paper-candidate-test-token-32-characters-minimum"


def _private(database, manifest):
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


def _evidence(tmp_path):
    data = (tmp_path / "private-data").resolve()
    data.mkdir(mode=0o700)
    assistant_database = data / "assistant.db"
    with sqlite3.connect(assistant_database) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
    assistant_database.chmod(0o600)
    paper_database = data / "paper.db"
    paper_db.ensure_seed(db_path=paper_database, seed_capital=100_000_000)
    paper_database.chmod(0o600)

    snapshot = (tmp_path / "private-backups" / "snapshot").resolve()
    snapshot.mkdir(parents=True, mode=0o700)
    snapshot.parent.chmod(0o700)
    databases = [
        backup_database(assistant_database, snapshot / "assistant.db"),
        backup_database(paper_database, snapshot / "paper.db"),
    ]
    manifest = snapshot / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": NOW.isoformat(),
                "all_restore_verified": True,
                "databases": databases,
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)

    assistant_private = _private(assistant_database, manifest)
    assistant = ScheduleWriteReadinessEvidence(
        private_write_evidence=assistant_private,
        notifier_enabled=True,
        notifier_writer_owners=(SCHEDULE_NOTIFIER_OWNER,),
        notifier_operations=SCHEDULE_NOTIFIER_OPERATIONS,
        notifier_uses_same_database=True,
        telegram_direct_user_mutations_disabled=True,
        notifier_user_mutations_disabled=True,
    )
    paper = PaperWriteReadinessEvidence(
        private_write_evidence=_private(paper_database, manifest),
        caller_policies=PAPER_TARGET_CALLER_POLICIES,
        consumers_use_private_api=True,
        consumers_use_same_database=True,
        paper_ui_direct_writes_disabled=True,
        telegram_direct_writes_disabled=True,
        operator_cli_rollback_only=True,
    )
    return assistant, paper


def _staging(tmp_path, name="paper-candidate"):
    path = (tmp_path / name).resolve()
    path.mkdir(mode=0o700)
    return path


def _state(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def _load(candidate, assistant, paper, staging):
    private = assistant.private_write_evidence
    return load_private_write_runtime_bundle(
        {BUNDLE_PATH_ENV: str(candidate.path)},
        database_path=private.database_path,
        paper_database_path=paper.private_write_evidence.database_path,
        private_root=staging,
        backup_root=private.backup_manifest_path.parent.parent,
        now=NOW,
    )


def test_paper_candidate_is_private_noninstalled_and_cross_process_validated(tmp_path):
    assistant, paper = _evidence(tmp_path)
    private = assistant.private_write_evidence
    paths = (
        private.database_path,
        paper.private_write_evidence.database_path,
        private.backup_manifest_path,
        private.backup_manifest_path.parent / "assistant.db",
        private.backup_manifest_path.parent / "paper.db",
    )
    before = {path: _state(path) for path in paths}
    staging = _staging(tmp_path)

    candidate = generate_paper_write_activation_candidate(
        assistant,
        paper,
        staging,
        now=NOW,
    )

    assert candidate.installed is False
    assert candidate.paper_enabled is True
    assert candidate.schedule_enabled is True
    assert candidate.runtime_roles_validated == PAPER_RUNTIME_ROLES
    assert candidate.path == staging / BUNDLE_FILENAME
    assert candidate.path.stat().st_mode & 0o777 == 0o600
    assert (
        candidate.assistant_activation_fingerprint
        != candidate.paper_activation_fingerprint
    )
    assert not hasattr(candidate, "install")
    assert not hasattr(candidate, "execute")
    assert str(staging) not in repr(candidate)
    assert before == {path: _state(path) for path in paths}

    payload = json.loads(candidate.path.read_text(encoding="utf-8"))
    assert payload["version"] == 3
    assert payload["schedule"]["enabled"] is True
    assert payload["paper"]["enabled"] is True
    assert payload["paper"]["current_runtime_writer_owners"] == list(
        PAPER_LEGACY_RUNTIME_WRITERS
    )
    assert "database_path" not in candidate.path.read_text(encoding="utf-8")
    assert "token" not in candidate.path.read_text(encoding="utf-8").lower()


def test_v3_runtime_builds_separate_paper_stack_and_five_routes(tmp_path):
    assistant, paper = _evidence(tmp_path)
    staging = _staging(tmp_path)
    candidate = generate_paper_write_activation_candidate(
        assistant,
        paper,
        staging,
        now=NOW,
    )
    api_bundle = _load(candidate, assistant, paper, staging)
    paper_ui_bundle = _load(candidate, assistant, paper, staging)
    telegram_bundle = _load(candidate, assistant, paper, staging)

    writer = api_bundle.build_paper_api_writer()
    client = paper_ui_bundle.build_paper_client(token=TOKEN)
    executor = telegram_bundle.build_paper_executor(
        telegram_bundle.build_paper_client(token=TOKEN)
    )
    app = create_app(
        TOKEN,
        paper_trade_writer=writer,
        enable_paper_trade_writes=True,
        paper_write_activation_permit=api_bundle.paper_activation_permit,
    )
    paper_mutations = {
        (route.path, method)
        for route in app.routes
        for method in (route.methods or set())
        if method in {"POST", "PUT", "PATCH", "DELETE"} and "/paper/" in route.path
    }

    assert writer.writes_enabled is True
    assert client.paper_writes_enabled is True
    assert executor.enabled is True
    assert len(paper_mutations) == 5
    assert (
        api_bundle.bundle_id == paper_ui_bundle.bundle_id == telegram_bundle.bundle_id
    )
    assert (
        api_bundle.paper_activation_permit.fingerprint
        == paper_ui_bundle.paper_activation_permit.fingerprint
        == telegram_bundle.paper_activation_permit.fingerprint
    )


@pytest.mark.parametrize(
    "mutation,code",
    [
        (
            lambda paper: paper.update(client_writes_enabled=False),
            "incomplete_paper_activation",
        ),
        (
            lambda paper: paper.update(writer_owner="other"),
            "invalid_paper_writer_owner",
        ),
        (
            lambda paper: paper["caller_policies"].reverse(),
            "invalid_paper_caller_policies",
        ),
    ],
)
def test_v3_paper_partial_or_policy_drift_fails_closed(tmp_path, mutation, code):
    assistant, paper = _evidence(tmp_path)
    staging = _staging(tmp_path)
    candidate = generate_paper_write_activation_candidate(
        assistant,
        paper,
        staging,
        now=NOW,
    )
    payload = json.loads(candidate.path.read_text(encoding="utf-8"))
    mutation(payload["paper"])
    candidate.path.write_text(json.dumps(payload), encoding="utf-8")
    candidate.path.chmod(0o600)

    with pytest.raises(PrivateWriteRuntimeError) as error:
        _load(candidate, assistant, paper, staging)

    assert error.value.code == code


def test_candidate_refuses_nonshared_backup_and_invalid_paper_owner_before_write(
    tmp_path,
):
    assistant, paper = _evidence(tmp_path)
    different = replace(
        paper,
        private_write_evidence=replace(
            paper.private_write_evidence,
            backup_manifest_path=paper.private_write_evidence.backup_manifest_path.parent
            / "other-manifest.json",
        ),
    )
    first = _staging(tmp_path, "different-manifest")
    with pytest.raises(PaperWriteCandidateError) as manifest_error:
        generate_paper_write_activation_candidate(assistant, different, first, now=NOW)
    assert manifest_error.value.code == "backup_manifest_not_shared"
    assert list(first.iterdir()) == []

    second = _staging(tmp_path, "owner-drift")
    with pytest.raises(PaperWriteCandidateError) as owner_error:
        generate_paper_write_activation_candidate(
            assistant,
            paper,
            second,
            current_paper_runtime_writer_owners=tuple(
                reversed(PAPER_LEGACY_RUNTIME_WRITERS)
            ),
            now=NOW,
        )
    assert owner_error.value.code == "invalid_current_paper_writer_owners"
    assert list(second.iterdir()) == []


def test_candidate_requires_fresh_paper_backup_before_writing(tmp_path):
    assistant, paper = _evidence(tmp_path)
    manifest = assistant.private_write_evidence.backup_manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)
    staging = _staging(tmp_path)

    with pytest.raises(RuntimeError) as error:
        generate_paper_write_activation_candidate(
            assistant,
            paper,
            staging,
            now=NOW,
        )

    assert "backup_fresh" in str(error.value)
    assert list(staging.iterdir()) == []


def test_v2_runtime_stays_paper_disabled(tmp_path):
    assistant, paper = _evidence(tmp_path)
    staging = _staging(tmp_path)
    candidate = generate_paper_write_activation_candidate(
        assistant,
        paper,
        staging,
        now=NOW,
    )
    payload = json.loads(candidate.path.read_text(encoding="utf-8"))
    payload["version"] = 2
    payload.pop("paper")
    # A v2 fingerprint is intentionally not reconstructed here: schema parsing
    # must succeed far enough to reject the v3 combined fingerprint, not Paper.
    candidate.path.write_text(json.dumps(payload), encoding="utf-8")
    candidate.path.chmod(0o600)

    with pytest.raises(PrivateWriteRuntimeError) as error:
        _load(candidate, assistant, paper, staging)

    assert error.value.code == "bundle_fingerprint_mismatch"
