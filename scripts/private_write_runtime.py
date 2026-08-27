#!/usr/bin/env python3
"""Atomic, default-disabled runtime bundle for Private writes.

Activation requires one private JSON file referenced by one environment key.
No individual component flag or database path is accepted from the runtime
environment.  With the key absent this module reads no file and returns None.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from private_data_api_client import PrivateDataClient
from private_paper_write_consumer import PaperTradeWriteExecutor
from private_paper_write_cutover import (
    PAPER_LEGACY_RUNTIME_WRITERS,
    PaperWriteCutoverConfig,
    build_paper_write_cutover_dry_run,
)
from private_paper_write_readiness import (
    PAPER_API_OWNER,
    PAPER_TARGET_CALLER_POLICIES,
    PaperCallerPolicy,
    PaperWriteReadinessEvidence,
)
from private_paper_write_store import PaperTradeWriteStore
from private_schedule_write_consumer import ScheduleWriteExecutor
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
from private_schedule_write_store import ScheduleWriteStore
from private_watchlist_write_consumer import WatchlistWriteExecutor
from private_watchlist_write_store import WatchlistWriteStore
from private_data_security import default_paths as default_security_paths
from private_write_cutover import (
    LEGACY_WRITER_OWNER,
    PrivateWriteCutoverConfig,
    build_private_write_cutover_dry_run,
)
from private_write_readiness import (
    DEFAULT_MAX_BACKUP_AGE,
    PrivateWriteActivationPermit,
    PrivateWriteReadinessEvidence,
    issue_private_write_activation_permit,
)
from storage_paths import PATHS

BUNDLE_PATH_ENV = "AI_AGENT_PRIVATE_WRITE_BUNDLE_PATH"
BUNDLE_FILENAME = "private-write-activation.json"
LEGACY_BUNDLE_VERSION = 1
BUNDLE_VERSION = 2
PAPER_BUNDLE_VERSION = 3
MAX_BUNDLE_BYTES = 16_384
_BUNDLE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_V1_CONFIG_KEYS = frozenset(
    {
        "version",
        "enabled",
        "bundle_id",
        "api_writer_enabled",
        "client_writes_enabled",
        "consumer_executor_enabled",
        "writer_owner",
        "current_writer_owner",
        "backup_manifest",
        "user_approval_required",
        "direct_db_fallback_disabled",
        "rollback_verified",
        "automatic_backup_restore",
    }
)
_V2_CONFIG_KEYS = _V1_CONFIG_KEYS | {"schedule"}
_V3_CONFIG_KEYS = _V2_CONFIG_KEYS | {"paper"}
_SCHEDULE_ENABLED_KEYS = frozenset(
    {
        "enabled",
        "api_writer_enabled",
        "client_writes_enabled",
        "consumer_executor_enabled",
        "user_writer_owner",
        "current_user_writer_owner",
        "notifier_enabled",
        "notifier_writer_owner",
        "notifier_operations",
        "notifier_uses_same_database",
        "telegram_direct_user_mutations_disabled",
        "notifier_user_mutations_disabled",
    }
)
_PAPER_ENABLED_KEYS = frozenset(
    {
        "enabled",
        "api_writer_enabled",
        "client_writes_enabled",
        "consumer_executor_enabled",
        "writer_owner",
        "current_runtime_writer_owners",
        "current_operator_cli_rollback_only",
        "caller_policies",
        "consumers_use_private_api",
        "consumers_use_same_database",
        "paper_ui_direct_writes_disabled",
        "telegram_direct_writes_disabled",
        "operator_cli_rollback_only",
    }
)


class PrivateWriteRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Private write runtime bundle rejected: {code}")
        self.code = code


@dataclass(frozen=True)
class PrivateWriteRuntimeBundle:
    bundle_id: str
    activation_fingerprint: str
    _database_path: Path = field(repr=False)
    _permit: PrivateWriteActivationPermit = field(repr=False)
    schedule_writes_enabled: bool = False
    paper_writes_enabled: bool = False
    _paper_database_path: Path | None = field(repr=False, default=None)
    _paper_permit: PrivateWriteActivationPermit | None = field(
        repr=False,
        default=None,
    )

    def build_api_writer(self) -> WatchlistWriteStore:
        return WatchlistWriteStore(self._database_path, writes_enabled=True)

    def build_client(
        self,
        *,
        token: str | None = None,
        opener: Any = None,
    ) -> PrivateDataClient:
        kwargs: dict[str, Any] = {
            "token": token,
            "writes_enabled": True,
            "write_activation_permit": self._permit,
        }
        if opener is not None:
            kwargs["opener"] = opener
        return PrivateDataClient(**kwargs)

    def build_executor(self, client: PrivateDataClient) -> WatchlistWriteExecutor:
        return WatchlistWriteExecutor(
            client,
            enabled=True,
            write_activation_permit=self._permit,
        )

    def _require_schedule_enabled(self) -> None:
        if not self.schedule_writes_enabled:
            raise PrivateWriteRuntimeError("schedule_capability_disabled")

    def build_schedule_api_writer(self) -> ScheduleWriteStore:
        self._require_schedule_enabled()
        return ScheduleWriteStore(self._database_path, writes_enabled=True)

    def build_schedule_executor(
        self,
        client: PrivateDataClient,
    ) -> ScheduleWriteExecutor:
        self._require_schedule_enabled()
        return ScheduleWriteExecutor(
            client,
            enabled=True,
            write_activation_permit=self._permit,
        )

    def _require_paper_enabled(self) -> None:
        if (
            not self.paper_writes_enabled
            or self._paper_database_path is None
            or self._paper_permit is None
        ):
            raise PrivateWriteRuntimeError("paper_capability_disabled")

    def build_paper_api_writer(self) -> PaperTradeWriteStore:
        self._require_paper_enabled()
        return PaperTradeWriteStore(self._paper_database_path, writes_enabled=True)

    def build_paper_client(
        self,
        *,
        token: str | None = None,
        opener: Any = None,
    ) -> PrivateDataClient:
        self._require_paper_enabled()
        kwargs: dict[str, Any] = {
            "token": token,
            "paper_writes_enabled": True,
            "paper_write_activation_permit": self._paper_permit,
            "paper_write_database_path": self._paper_database_path,
        }
        if opener is not None:
            kwargs["opener"] = opener
        return PrivateDataClient(**kwargs)

    def build_paper_executor(
        self,
        client: PrivateDataClient,
    ) -> PaperTradeWriteExecutor:
        self._require_paper_enabled()
        return PaperTradeWriteExecutor(
            client,
            enabled=True,
            paper_write_activation_permit=self._paper_permit,
        )

    @property
    def activation_permit(self) -> PrivateWriteActivationPermit:
        return self._permit

    @property
    def paper_activation_permit(self) -> PrivateWriteActivationPermit:
        self._require_paper_enabled()
        assert self._paper_permit is not None
        return self._paper_permit


def _canonical_path_text(path: Path) -> str:
    """Compare macOS paths without treating NFC/NFD spelling as drift."""
    return unicodedata.normalize("NFD", os.fspath(path.resolve()))


def _require_private_config_file(path: Path, private_root: Path) -> Path:
    try:
        root_valid = (
            private_root.is_absolute()
            and not private_root.is_symlink()
            and private_root.is_dir()
            and private_root.stat().st_mode & 0o077 == 0
        )
        expected = (private_root / BUNDLE_FILENAME).resolve()
        valid = (
            root_valid
            and path.is_absolute()
            and _canonical_path_text(path) == _canonical_path_text(expected)
            and not path.is_symlink()
            and path.is_file()
            and path.stat().st_mode & 0o077 == 0
            and 0 < path.stat().st_size <= MAX_BUNDLE_BYTES
        )
    except OSError:
        valid = False
    if not valid:
        raise PrivateWriteRuntimeError("invalid_bundle_file")
    return path


def _load_config(path: Path) -> dict[str, object]:
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PrivateWriteRuntimeError("duplicate_bundle_key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise PrivateWriteRuntimeError("invalid_bundle_json") from exc
    if not isinstance(payload, dict):
        raise PrivateWriteRuntimeError("invalid_bundle_schema")
    version = payload.get("version")
    if version not in {
        LEGACY_BUNDLE_VERSION,
        BUNDLE_VERSION,
        PAPER_BUNDLE_VERSION,
    }:
        raise PrivateWriteRuntimeError("unsupported_bundle_version")
    if version == LEGACY_BUNDLE_VERSION:
        expected_keys = _V1_CONFIG_KEYS
    elif version == BUNDLE_VERSION:
        expected_keys = _V2_CONFIG_KEYS
    else:
        expected_keys = _V3_CONFIG_KEYS
    if set(payload) != expected_keys:
        raise PrivateWriteRuntimeError("invalid_bundle_schema")
    boolean_keys = (
        "enabled",
        "api_writer_enabled",
        "client_writes_enabled",
        "consumer_executor_enabled",
        "user_approval_required",
        "direct_db_fallback_disabled",
        "rollback_verified",
        "automatic_backup_restore",
    )
    if any(type(payload[key]) is not bool for key in boolean_keys):
        raise PrivateWriteRuntimeError("invalid_bundle_flags")
    if (
        not all(
            payload[key]
            for key in (
                "enabled",
                "api_writer_enabled",
                "client_writes_enabled",
                "consumer_executor_enabled",
                "user_approval_required",
                "direct_db_fallback_disabled",
                "rollback_verified",
            )
        )
        or payload["automatic_backup_restore"] is not False
    ):
        raise PrivateWriteRuntimeError("incomplete_atomic_activation")
    bundle_id = payload["bundle_id"]
    if not isinstance(bundle_id, str) or _BUNDLE_ID_RE.fullmatch(bundle_id) is None:
        raise PrivateWriteRuntimeError("invalid_bundle_id")
    if payload["writer_owner"] != "private-data-api":
        raise PrivateWriteRuntimeError("invalid_target_writer_owner")
    if payload["current_writer_owner"] not in {"", LEGACY_WRITER_OWNER}:
        raise PrivateWriteRuntimeError("invalid_current_writer_owner")
    if version == LEGACY_BUNDLE_VERSION:
        payload["schedule"] = {"enabled": False}
    else:
        _validate_schedule_config(payload["schedule"])
    if version == PAPER_BUNDLE_VERSION:
        _validate_paper_config(payload["paper"])
    else:
        payload["paper"] = {"enabled": False}
    return payload


def _validate_schedule_config(value: object) -> None:
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise PrivateWriteRuntimeError("invalid_schedule_bundle_schema")
    if value["enabled"] is False:
        if set(value) != {"enabled"}:
            raise PrivateWriteRuntimeError("incomplete_schedule_activation")
        return
    if set(value) != _SCHEDULE_ENABLED_KEYS:
        raise PrivateWriteRuntimeError("invalid_schedule_bundle_schema")
    boolean_keys = (
        "api_writer_enabled",
        "client_writes_enabled",
        "consumer_executor_enabled",
        "notifier_enabled",
        "notifier_uses_same_database",
        "telegram_direct_user_mutations_disabled",
        "notifier_user_mutations_disabled",
    )
    if any(type(value.get(key)) is not bool for key in boolean_keys):
        raise PrivateWriteRuntimeError("invalid_schedule_bundle_flags")
    if not all(value[key] for key in boolean_keys):
        raise PrivateWriteRuntimeError("incomplete_schedule_activation")
    if value["user_writer_owner"] != "private-data-api":
        raise PrivateWriteRuntimeError("invalid_schedule_user_writer_owner")
    if value["current_user_writer_owner"] != LEGACY_SCHEDULE_USER_WRITER:
        raise PrivateWriteRuntimeError("invalid_schedule_current_writer_owner")
    if value["notifier_writer_owner"] != SCHEDULE_NOTIFIER_OWNER:
        raise PrivateWriteRuntimeError("invalid_schedule_notifier_owner")
    operations = value["notifier_operations"]
    if (
        not isinstance(operations, list)
        or tuple(operations) != SCHEDULE_NOTIFIER_OPERATIONS
    ):
        raise PrivateWriteRuntimeError("invalid_schedule_notifier_operations")


def _paper_policy_payload() -> list[dict[str, object]]:
    return [
        {
            "caller": policy.caller,
            "operations": list(policy.operations),
            "approval_mode": policy.approval_mode,
        }
        for policy in PAPER_TARGET_CALLER_POLICIES
    ]


def _validate_paper_config(value: object) -> None:
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise PrivateWriteRuntimeError("invalid_paper_bundle_schema")
    if value["enabled"] is False:
        if set(value) != {"enabled"}:
            raise PrivateWriteRuntimeError("incomplete_paper_activation")
        return
    if set(value) != _PAPER_ENABLED_KEYS:
        raise PrivateWriteRuntimeError("invalid_paper_bundle_schema")
    boolean_keys = (
        "api_writer_enabled",
        "client_writes_enabled",
        "consumer_executor_enabled",
        "current_operator_cli_rollback_only",
        "consumers_use_private_api",
        "consumers_use_same_database",
        "paper_ui_direct_writes_disabled",
        "telegram_direct_writes_disabled",
        "operator_cli_rollback_only",
    )
    if any(type(value.get(key)) is not bool for key in boolean_keys):
        raise PrivateWriteRuntimeError("invalid_paper_bundle_flags")
    if not all(value[key] for key in boolean_keys):
        raise PrivateWriteRuntimeError("incomplete_paper_activation")
    if value["writer_owner"] != PAPER_API_OWNER:
        raise PrivateWriteRuntimeError("invalid_paper_writer_owner")
    owners = value["current_runtime_writer_owners"]
    if not isinstance(owners, list) or tuple(owners) != PAPER_LEGACY_RUNTIME_WRITERS:
        raise PrivateWriteRuntimeError("invalid_paper_current_writer_owners")
    if value["caller_policies"] != _paper_policy_payload():
        raise PrivateWriteRuntimeError("invalid_paper_caller_policies")


def _combined_bundle_id(watchlist_bundle_id: str, schedule_bundle_id: str) -> str:
    payload = f"private-write-runtime-v2\0{watchlist_bundle_id}\0{schedule_bundle_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _paper_combined_bundle_id(
    assistant_bundle_id: str,
    paper_bundle_id: str,
) -> str:
    payload = f"private-write-runtime-v3\0{assistant_bundle_id}\0{paper_bundle_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_private_write_runtime_bundle(
    environ: Mapping[str, str] | None = None,
    *,
    database_path: Path = PATHS.watchlist_db,
    paper_database_path: Path = PATHS.paper_db,
    private_root: Path = PATHS.private_root,
    backup_root: Path | None = None,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PrivateWriteRuntimeBundle | None:
    """Return None when absent; otherwise validate the entire atomic bundle."""
    source = os.environ if environ is None else environ
    configured = str(source.get(BUNDLE_PATH_ENV, "")).strip()
    if not configured:
        return None
    bundle_path = _require_private_config_file(Path(configured), Path(private_root))
    payload = _load_config(bundle_path)

    backup_value = payload["backup_manifest"]
    if not isinstance(backup_value, str) or not backup_value.strip():
        raise PrivateWriteRuntimeError("invalid_backup_manifest")
    backup_manifest = Path(backup_value)
    if not backup_manifest.is_absolute():
        raise PrivateWriteRuntimeError("invalid_backup_manifest")
    backup_manifest = backup_manifest.resolve()
    approved_backup_root = Path(
        backup_root if backup_root is not None else default_security_paths().backup_root
    ).resolve()
    try:
        backup_location_valid = (
            approved_backup_root.is_absolute()
            and not approved_backup_root.is_symlink()
            and approved_backup_root.is_dir()
            and approved_backup_root.stat().st_mode & 0o077 == 0
            and backup_manifest.name == "manifest.json"
            and _canonical_path_text(backup_manifest.parent.parent)
            == _canonical_path_text(approved_backup_root)
        )
    except OSError:
        backup_location_valid = False
    if not backup_location_valid:
        raise PrivateWriteRuntimeError("invalid_backup_location")
    current_owner = str(payload["current_writer_owner"])
    evidence = PrivateWriteReadinessEvidence(
        api_writer_enabled=bool(payload["api_writer_enabled"]),
        client_writes_enabled=bool(payload["client_writes_enabled"]),
        consumer_executor_enabled=bool(payload["consumer_executor_enabled"]),
        writer_owners=(str(payload["writer_owner"]),),
        database_path=Path(database_path).resolve(),
        backup_manifest_path=backup_manifest,
        user_approval_required=bool(payload["user_approval_required"]),
        direct_db_fallback_disabled=bool(payload["direct_db_fallback_disabled"]),
        rollback_verified=bool(payload["rollback_verified"]),
    )
    cutover = build_private_write_cutover_dry_run(
        PrivateWriteCutoverConfig(
            readiness_evidence=evidence,
            current_api_writer_enabled=False,
            current_client_writes_enabled=False,
            current_consumer_executor_enabled=False,
            current_writer_owners=(current_owner,) if current_owner else (),
            automatic_backup_restore=bool(payload["automatic_backup_restore"]),
        ),
        now=now,
        max_backup_age=max_backup_age,
    )
    schedule_config = payload["schedule"]
    schedule_enabled = bool(schedule_config["enabled"])
    expected_bundle_id = cutover.bundle_id
    schedule_activation_fingerprint = cutover.activation_fingerprint
    if schedule_enabled:
        schedule_private_evidence = PrivateWriteReadinessEvidence(
            api_writer_enabled=bool(schedule_config["api_writer_enabled"]),
            client_writes_enabled=bool(schedule_config["client_writes_enabled"]),
            consumer_executor_enabled=bool(
                schedule_config["consumer_executor_enabled"]
            ),
            writer_owners=(str(schedule_config["user_writer_owner"]),),
            database_path=evidence.database_path,
            backup_manifest_path=backup_manifest,
            user_approval_required=bool(payload["user_approval_required"]),
            direct_db_fallback_disabled=bool(payload["direct_db_fallback_disabled"]),
            rollback_verified=bool(payload["rollback_verified"]),
        )
        schedule_evidence = ScheduleWriteReadinessEvidence(
            private_write_evidence=schedule_private_evidence,
            notifier_enabled=bool(schedule_config["notifier_enabled"]),
            notifier_writer_owners=(str(schedule_config["notifier_writer_owner"]),),
            notifier_operations=tuple(schedule_config["notifier_operations"]),
            notifier_uses_same_database=bool(
                schedule_config["notifier_uses_same_database"]
            ),
            telegram_direct_user_mutations_disabled=bool(
                schedule_config["telegram_direct_user_mutations_disabled"]
            ),
            notifier_user_mutations_disabled=bool(
                schedule_config["notifier_user_mutations_disabled"]
            ),
        )
        schedule_cutover = build_schedule_write_cutover_dry_run(
            ScheduleWriteCutoverConfig(
                readiness_evidence=schedule_evidence,
                current_api_schedule_writer_enabled=False,
                current_schedule_client_enabled=False,
                current_schedule_executor_enabled=False,
                current_user_mutation_writer_owners=(
                    str(schedule_config["current_user_writer_owner"]),
                ),
                current_notifier_enabled=bool(schedule_config["notifier_enabled"]),
                current_notifier_writer_owners=(
                    str(schedule_config["notifier_writer_owner"]),
                ),
                current_notifier_operations=tuple(
                    schedule_config["notifier_operations"]
                ),
                automatic_backup_restore=bool(payload["automatic_backup_restore"]),
            ),
            now=now,
            max_backup_age=max_backup_age,
        )
        expected_bundle_id = _combined_bundle_id(
            cutover.bundle_id,
            schedule_cutover.bundle_id,
        )
        schedule_activation_fingerprint = schedule_cutover.activation_fingerprint
    paper_config = payload["paper"]
    paper_enabled = bool(paper_config["enabled"])
    paper_database: Path | None = None
    paper_permit: PrivateWriteActivationPermit | None = None
    if paper_enabled:
        paper_database = Path(paper_database_path).resolve()
        paper_private_evidence = PrivateWriteReadinessEvidence(
            api_writer_enabled=bool(paper_config["api_writer_enabled"]),
            client_writes_enabled=bool(paper_config["client_writes_enabled"]),
            consumer_executor_enabled=bool(paper_config["consumer_executor_enabled"]),
            writer_owners=(str(paper_config["writer_owner"]),),
            database_path=paper_database,
            backup_manifest_path=backup_manifest,
            user_approval_required=bool(payload["user_approval_required"]),
            direct_db_fallback_disabled=bool(payload["direct_db_fallback_disabled"]),
            rollback_verified=bool(payload["rollback_verified"]),
        )
        paper_policies = tuple(
            PaperCallerPolicy(
                caller=str(policy["caller"]),
                operations=tuple(policy["operations"]),
                approval_mode=str(policy["approval_mode"]),
            )
            for policy in paper_config["caller_policies"]
        )
        paper_evidence = PaperWriteReadinessEvidence(
            private_write_evidence=paper_private_evidence,
            caller_policies=paper_policies,
            consumers_use_private_api=bool(paper_config["consumers_use_private_api"]),
            consumers_use_same_database=bool(
                paper_config["consumers_use_same_database"]
            ),
            paper_ui_direct_writes_disabled=bool(
                paper_config["paper_ui_direct_writes_disabled"]
            ),
            telegram_direct_writes_disabled=bool(
                paper_config["telegram_direct_writes_disabled"]
            ),
            operator_cli_rollback_only=bool(paper_config["operator_cli_rollback_only"]),
        )
        paper_cutover = build_paper_write_cutover_dry_run(
            PaperWriteCutoverConfig(
                readiness_evidence=paper_evidence,
                current_api_paper_writer_enabled=False,
                current_paper_client_enabled=False,
                current_paper_executor_enabled=False,
                current_runtime_writer_owners=tuple(
                    paper_config["current_runtime_writer_owners"]
                ),
                current_operator_cli_rollback_only=bool(
                    paper_config["current_operator_cli_rollback_only"]
                ),
                automatic_backup_restore=bool(payload["automatic_backup_restore"]),
            ),
            now=now,
            max_backup_age=max_backup_age,
        )
        expected_bundle_id = _paper_combined_bundle_id(
            expected_bundle_id,
            paper_cutover.bundle_id,
        )
    if expected_bundle_id != payload["bundle_id"]:
        raise PrivateWriteRuntimeError("bundle_fingerprint_mismatch")
    permit = issue_private_write_activation_permit(
        evidence,
        now=now,
        max_backup_age=max_backup_age,
    )
    if permit.fingerprint != cutover.activation_fingerprint:
        raise PrivateWriteRuntimeError("permit_fingerprint_mismatch")
    if permit.fingerprint != schedule_activation_fingerprint:
        raise PrivateWriteRuntimeError("schedule_permit_fingerprint_mismatch")
    if paper_enabled:
        assert paper_database is not None
        paper_permit = issue_private_write_activation_permit(
            paper_private_evidence,
            now=now,
            max_backup_age=max_backup_age,
        )
        if paper_permit.fingerprint != paper_cutover.activation_fingerprint:
            raise PrivateWriteRuntimeError("paper_permit_fingerprint_mismatch")
        if paper_permit.fingerprint == permit.fingerprint:
            raise PrivateWriteRuntimeError("paper_permit_not_separate")
    return PrivateWriteRuntimeBundle(
        bundle_id=expected_bundle_id,
        activation_fingerprint=cutover.activation_fingerprint,
        _database_path=evidence.database_path,
        _permit=permit,
        schedule_writes_enabled=schedule_enabled,
        paper_writes_enabled=paper_enabled,
        _paper_database_path=paper_database,
        _paper_permit=paper_permit,
    )
