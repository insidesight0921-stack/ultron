from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from private_data_api import create_app
from private_schedule_write_cutover import (
    LEGACY_SCHEDULE_USER_WRITER,
    ScheduleWriteCutoverConfig,
    build_schedule_write_cutover_dry_run,
)
from private_schedule_write_readiness import (
    SCHEDULE_NOTIFIER_OPERATIONS,
    SCHEDULE_NOTIFIER_OWNER,
    ScheduleWriteReadinessEvidence,
)
from private_write_cutover import (
    PrivateWriteCutoverConfig,
    build_private_write_cutover_dry_run,
)
from private_write_runtime import (
    BUNDLE_FILENAME,
    BUNDLE_PATH_ENV,
    PrivateWriteRuntimeError,
    _canonical_path_text,
    _combined_bundle_id,
    load_private_write_runtime_bundle,
)


TOKEN = "private-runtime-test-token-32-characters-minimum"
NOW = datetime.now().astimezone()


def _payload(evidence, *, now=NOW):
    plan = build_private_write_cutover_dry_run(
        PrivateWriteCutoverConfig(
            readiness_evidence=evidence,
            current_api_writer_enabled=False,
            current_client_writes_enabled=False,
            current_consumer_executor_enabled=False,
            current_writer_owners=("telegram-direct",),
        ),
        now=now,
    )
    return {
        "version": 1,
        "enabled": True,
        "bundle_id": plan.bundle_id,
        "api_writer_enabled": True,
        "client_writes_enabled": True,
        "consumer_executor_enabled": True,
        "writer_owner": "private-data-api",
        "current_writer_owner": "telegram-direct",
        "backup_manifest": str(evidence.backup_manifest_path),
        "user_approval_required": True,
        "direct_db_fallback_disabled": True,
        "rollback_verified": True,
        "automatic_backup_restore": False,
    }


def _v2_disabled_payload(evidence, *, now=NOW):
    payload = _payload(evidence, now=now)
    payload["version"] = 2
    payload["schedule"] = {"enabled": False}
    return payload


def _v2_enabled_payload(evidence, *, now=NOW):
    payload = _v2_disabled_payload(evidence, now=now)
    schedule = {
        "enabled": True,
        "api_writer_enabled": True,
        "client_writes_enabled": True,
        "consumer_executor_enabled": True,
        "user_writer_owner": "private-data-api",
        "current_user_writer_owner": LEGACY_SCHEDULE_USER_WRITER,
        "notifier_enabled": True,
        "notifier_writer_owner": SCHEDULE_NOTIFIER_OWNER,
        "notifier_operations": list(SCHEDULE_NOTIFIER_OPERATIONS),
        "notifier_uses_same_database": True,
        "telegram_direct_user_mutations_disabled": True,
        "notifier_user_mutations_disabled": True,
    }
    schedule_evidence = ScheduleWriteReadinessEvidence(
        private_write_evidence=evidence,
        notifier_enabled=True,
        notifier_writer_owners=(SCHEDULE_NOTIFIER_OWNER,),
        notifier_operations=SCHEDULE_NOTIFIER_OPERATIONS,
        notifier_uses_same_database=True,
        telegram_direct_user_mutations_disabled=True,
        notifier_user_mutations_disabled=True,
    )
    schedule_plan = build_schedule_write_cutover_dry_run(
        ScheduleWriteCutoverConfig(
            readiness_evidence=schedule_evidence,
            current_api_schedule_writer_enabled=False,
            current_schedule_client_enabled=False,
            current_schedule_executor_enabled=False,
            current_user_mutation_writer_owners=(LEGACY_SCHEDULE_USER_WRITER,),
            current_notifier_enabled=True,
            current_notifier_writer_owners=(SCHEDULE_NOTIFIER_OWNER,),
            current_notifier_operations=SCHEDULE_NOTIFIER_OPERATIONS,
        ),
        now=now,
    )
    payload["schedule"] = schedule
    payload["bundle_id"] = _combined_bundle_id(
        payload["bundle_id"],
        schedule_plan.bundle_id,
    )
    return payload


def _config_file(evidence, payload):
    private_root = evidence.database_path.parent
    private_root.chmod(0o700)
    path = private_root / BUNDLE_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path, private_root


def _load(evidence, path, private_root, *, now=NOW):
    return load_private_write_runtime_bundle(
        {BUNDLE_PATH_ENV: str(path)},
        database_path=evidence.database_path,
        private_root=private_root,
        backup_root=evidence.backup_manifest_path.parent.parent,
        now=now,
    )


def test_absent_bundle_is_default_disabled_without_reading_files(tmp_path):
    missing_database = tmp_path / "never-read" / "assistant.db"
    missing_root = tmp_path / "never-read"

    assert load_private_write_runtime_bundle(
        {},
        database_path=missing_database,
        private_root=missing_root,
        now=NOW,
    ) is None
    assert not missing_root.exists()


def test_canonical_path_text_treats_macos_nfc_and_nfd_as_same_location(tmp_path):
    composed = tmp_path / "울트론" / BUNDLE_FILENAME
    decomposed = tmp_path / "울트론" / BUNDLE_FILENAME

    assert _canonical_path_text(composed) == _canonical_path_text(decomposed)


def test_valid_atomic_bundle_builds_matching_api_client_and_executor(
    private_write_readiness_evidence_factory,
):
    evidence = private_write_readiness_evidence_factory()
    path, private_root = _config_file(evidence, _payload(evidence))

    api_bundle = _load(evidence, path, private_root)
    telegram_bundle = _load(evidence, path, private_root)
    writer = api_bundle.build_api_writer()
    client = telegram_bundle.build_client(token=TOKEN)
    executor = telegram_bundle.build_executor(client)
    app = create_app(
        TOKEN,
        watchlist_writer=writer,
        enable_watchlist_writes=True,
        write_activation_permit=api_bundle.activation_permit,
    )

    mutation = {
        (route.path, method)
        for route in app.routes
        for method in (route.methods or set())
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert len(mutation) == 5
    assert client.writes_enabled is True
    assert executor.enabled is True
    assert api_bundle.schedule_writes_enabled is False
    with pytest.raises(PrivateWriteRuntimeError, match="schedule_capability_disabled"):
        api_bundle.build_schedule_api_writer()
    assert api_bundle.bundle_id == telegram_bundle.bundle_id
    assert api_bundle.activation_fingerprint == telegram_bundle.activation_fingerprint
    assert str(evidence.database_path) not in repr(api_bundle)


def test_v2_schedule_disabled_bundle_preserves_watchlist_only_surface(
    private_write_readiness_evidence_factory,
):
    evidence = private_write_readiness_evidence_factory()
    path, private_root = _config_file(evidence, _v2_disabled_payload(evidence))

    bundle = _load(evidence, path, private_root)
    app = create_app(
        TOKEN,
        watchlist_writer=bundle.build_api_writer(),
        enable_watchlist_writes=True,
        write_activation_permit=bundle.activation_permit,
    )

    mutation = {
        (route.path, method)
        for route in app.routes
        for method in (route.methods or set())
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert bundle.schedule_writes_enabled is False
    assert len(mutation) == 5


def test_v2_schedule_atomic_bundle_builds_both_process_stacks(
    private_write_readiness_evidence_factory,
):
    evidence = private_write_readiness_evidence_factory()
    path, private_root = _config_file(evidence, _v2_enabled_payload(evidence))

    api_bundle = _load(evidence, path, private_root)
    telegram_bundle = _load(evidence, path, private_root)
    client = telegram_bundle.build_client(token=TOKEN)
    schedule_executor = telegram_bundle.build_schedule_executor(client)
    app = create_app(
        TOKEN,
        watchlist_writer=api_bundle.build_api_writer(),
        enable_watchlist_writes=True,
        schedule_writer=api_bundle.build_schedule_api_writer(),
        enable_schedule_writes=True,
        write_activation_permit=api_bundle.activation_permit,
    )

    mutation = {
        (route.path, method)
        for route in app.routes
        for method in (route.methods or set())
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert api_bundle.schedule_writes_enabled is True
    assert schedule_executor.enabled is True
    assert len(mutation) == 10
    assert api_bundle.bundle_id == telegram_bundle.bundle_id
    assert api_bundle.activation_fingerprint == telegram_bundle.activation_fingerprint


@pytest.mark.parametrize(
    "mutation,code",
    [
        (
            lambda schedule: schedule.update(api_writer_enabled=False),
            "incomplete_schedule_activation",
        ),
        (
            lambda schedule: schedule.update(notifier_writer_owner="other"),
            "invalid_schedule_notifier_owner",
        ),
        (
            lambda schedule: schedule.update(
                notifier_operations=["schedule.mark_notified"]
            ),
            "invalid_schedule_notifier_operations",
        ),
    ],
)
def test_v2_schedule_partial_or_notifier_drift_fails_closed(
    private_write_readiness_evidence_factory,
    mutation,
    code,
):
    evidence = private_write_readiness_evidence_factory()
    payload = _v2_enabled_payload(evidence)
    mutation(payload["schedule"])
    path, private_root = _config_file(evidence, payload)

    with pytest.raises(PrivateWriteRuntimeError) as error:
        _load(evidence, path, private_root)

    assert error.value.code == code


def test_v2_disabled_schedule_rejects_hidden_partial_fields(
    private_write_readiness_evidence_factory,
):
    evidence = private_write_readiness_evidence_factory()
    payload = _v2_disabled_payload(evidence)
    payload["schedule"]["api_writer_enabled"] = False
    path, private_root = _config_file(evidence, payload)

    with pytest.raises(PrivateWriteRuntimeError) as error:
        _load(evidence, path, private_root)

    assert error.value.code == "incomplete_schedule_activation"


def test_v2_schedule_toggle_requires_combined_bundle_fingerprint(
    private_write_readiness_evidence_factory,
):
    evidence = private_write_readiness_evidence_factory()
    payload = _v2_enabled_payload(evidence)
    payload["bundle_id"] = _payload(evidence)["bundle_id"]
    path, private_root = _config_file(evidence, payload)

    with pytest.raises(PrivateWriteRuntimeError) as error:
        _load(evidence, path, private_root)

    assert error.value.code == "bundle_fingerprint_mismatch"


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", False),
        ("api_writer_enabled", False),
        ("client_writes_enabled", False),
        ("consumer_executor_enabled", False),
        ("user_approval_required", False),
        ("direct_db_fallback_disabled", False),
        ("rollback_verified", False),
        ("automatic_backup_restore", True),
    ],
)
def test_any_incomplete_atomic_flag_rejects_whole_bundle(
    private_write_readiness_evidence_factory, field, value
):
    evidence = private_write_readiness_evidence_factory()
    payload = _payload(evidence)
    payload[field] = value
    path, private_root = _config_file(evidence, payload)

    with pytest.raises(PrivateWriteRuntimeError) as exc:
        _load(evidence, path, private_root)

    assert exc.value.code == "incomplete_atomic_activation"


def test_bundle_fingerprint_drift_is_rejected(private_write_readiness_evidence_factory):
    evidence = private_write_readiness_evidence_factory()
    payload = _payload(evidence)
    payload["bundle_id"] = "0" * 64
    path, private_root = _config_file(evidence, payload)

    with pytest.raises(PrivateWriteRuntimeError) as exc:
        _load(evidence, path, private_root)

    assert exc.value.code == "bundle_fingerprint_mismatch"


def test_unknown_or_duplicate_config_key_is_rejected(
    private_write_readiness_evidence_factory,
):
    evidence = private_write_readiness_evidence_factory()
    payload = _payload(evidence)
    payload["token"] = "must-not-be-accepted"
    path, private_root = _config_file(evidence, payload)
    with pytest.raises(PrivateWriteRuntimeError) as unknown:
        _load(evidence, path, private_root)
    assert unknown.value.code == "invalid_bundle_schema"

    valid = _payload(evidence)
    raw = json.dumps(valid)[:-1] + ',"enabled":true}'
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(PrivateWriteRuntimeError) as duplicate:
        _load(evidence, path, private_root)
    assert duplicate.value.code == "duplicate_bundle_key"


def test_bundle_requires_exact_private_path_and_permissions(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    payload = _payload(evidence)
    path, private_root = _config_file(evidence, payload)
    path.chmod(0o644)
    with pytest.raises(PrivateWriteRuntimeError) as mode:
        _load(evidence, path, private_root)
    assert mode.value.code == "invalid_bundle_file"

    other = tmp_path / "other.json"
    other.write_text(json.dumps(payload), encoding="utf-8")
    other.chmod(0o600)
    with pytest.raises(PrivateWriteRuntimeError) as location:
        _load(evidence, other.resolve(), private_root)
    assert location.value.code == "invalid_bundle_file"


def test_bundle_manifest_must_be_under_approved_backup_root(
    tmp_path, private_write_readiness_evidence_factory
):
    evidence = private_write_readiness_evidence_factory()
    path, private_root = _config_file(evidence, _payload(evidence))
    unrelated_root = (tmp_path / "approved-backups").resolve()
    unrelated_root.mkdir(mode=0o700)

    with pytest.raises(PrivateWriteRuntimeError) as exc:
        load_private_write_runtime_bundle(
            {BUNDLE_PATH_ENV: str(path)},
            database_path=evidence.database_path,
            private_root=private_root,
            backup_root=unrelated_root,
            now=NOW,
        )

    assert exc.value.code == "invalid_backup_location"


def test_stale_backup_rejects_runtime_bundle(
    private_write_readiness_evidence_factory,
):
    evidence = private_write_readiness_evidence_factory()
    payload = json.loads(evidence.backup_manifest_path.read_text(encoding="utf-8"))
    payload["created_at"] = (NOW - timedelta(hours=25)).isoformat()
    evidence.backup_manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    evidence.backup_manifest_path.chmod(0o600)
    config_payload = _payload(evidence, now=NOW - timedelta(hours=24, minutes=55))
    path, private_root = _config_file(evidence, config_payload)

    with pytest.raises(RuntimeError) as exc:
        _load(evidence, path, private_root, now=NOW)

    assert "backup_fresh" in str(exc.value)


def test_bundle_schema_contains_no_token_or_database_override(
    private_write_readiness_evidence_factory,
):
    evidence = private_write_readiness_evidence_factory()
    payload = _payload(evidence)
    assert all("token" not in key.lower() for key in payload)
    assert "database_path" not in payload
    env_example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )
    assert BUNDLE_PATH_ENV not in env_example
