"""test_private_write_reissue.py — 번들 재발급 도구 (hermetic, tmp_path만 사용).

설치 경로는 여기서 시험하지 않는다 — 실제 로더 검증을 통과해야만 설치되도록
설계했고, 그 검증은 운영 DB·백업이 있어야 의미가 있다. 여기서는 **어느 백업을
고르는가**와 **나이 계산**을 본다. 잘못 고르면 오래된 백업으로 재발급해 놓고
'복구했다'고 착각하게 된다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import private_write_reissue as pr
import pytest


def _snapshot(root, name, created):
    d = root / name
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        json.dumps({"created_at": created, "all_restore_verified": True}),
        encoding="utf-8")
    return d / "manifest.json"


def test_picks_the_newest_backup(tmp_path):
    _snapshot(tmp_path, "20260101T000000+0900", "2026-01-01T00:00:00+09:00")
    newest = _snapshot(tmp_path, "20260827T093747+0900", "2026-08-27T09:37:47+09:00")
    assert pr.latest_manifest(tmp_path) == newest


def test_content_time_wins_over_directory_name(tmp_path):
    """디렉터리를 복사·이동하면 이름과 내용이 어긋날 수 있다 — 내용을 믿는다."""
    _snapshot(tmp_path, "29991231T000000+0900", "2026-01-01T00:00:00+09:00")
    real = _snapshot(tmp_path, "20260827T093747+0900", "2026-08-27T09:37:47+09:00")
    assert pr.latest_manifest(tmp_path) == real


def test_unreadable_manifests_are_skipped_not_fatal(tmp_path):
    bad = tmp_path / "20260828T000000+0900"
    bad.mkdir()
    (bad / "manifest.json").write_text("{망가진", encoding="utf-8")
    good = _snapshot(tmp_path, "20260827T093747+0900", "2026-08-27T09:37:47+09:00")
    assert pr.latest_manifest(tmp_path) == good


def test_no_backup_is_a_clear_error(tmp_path):
    with pytest.raises(pr.ReissueError):
        pr.latest_manifest(tmp_path)


def test_age_is_measured_from_the_manifest_not_the_file_mtime(tmp_path):
    """파일을 복사하면 mtime은 갱신되지만 백업이 새것이 되는 건 아니다."""
    created = datetime.now(timezone.utc) - timedelta(hours=30)
    m = _snapshot(tmp_path, "snap", created.isoformat())
    age = pr.manifest_age(m)
    assert timedelta(hours=29, minutes=55) < age < timedelta(hours=30, minutes=5)


def test_age_of_a_broken_manifest_is_unknown_not_zero(tmp_path):
    """모르는 것을 0으로 두면 '방금 뜬 백업'으로 통과해 버린다."""
    d = tmp_path / "snap"
    d.mkdir()
    m = d / "manifest.json"
    m.write_text("{}", encoding="utf-8")
    assert pr.manifest_age(m) is None


def test_evidence_carries_the_new_manifest_into_both_stacks(tmp_path):
    """재발급은 **백업 경로만** 바꾼다 — 정책을 조용히 바꾸는 통로가 되면 안 된다."""
    payload = {
        "api_writer_enabled": True, "client_writes_enabled": True,
        "consumer_executor_enabled": True, "user_approval_required": True,
        "direct_db_fallback_disabled": True, "rollback_verified": True,
        "schedule": {"user_writer_owner": "private-data-api",
                     "notifier_enabled": True,
                     "notifier_writer_owner": "telegram-schedule-notifier",
                     "notifier_operations": ["schedule.mark_notified"],
                     "notifier_uses_same_database": True,
                     "telegram_direct_user_mutations_disabled": True,
                     "notifier_user_mutations_disabled": True},
        "paper": {"writer_owner": "private-data-api",
                  "caller_policies": [{"caller": "paper-ui",
                                       "operations": ["paper.buy"],
                                       "approval_mode": "explicit-user"}],
                  "consumers_use_private_api": True,
                  "consumers_use_same_database": True,
                  "paper_ui_direct_writes_disabled": True,
                  "telegram_direct_writes_disabled": True,
                  "operator_cli_rollback_only": True},
    }
    manifest = tmp_path / "새백업" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{}", encoding="utf-8")

    assistant, paper = pr.build_evidence(payload, manifest)
    assert assistant.private_write_evidence.backup_manifest_path == manifest
    assert paper.private_write_evidence.backup_manifest_path == manifest
    # 두 스택은 **서로 다른 DB**를 가리켜야 한다 (후보 생성이 이를 요구한다)
    assert (assistant.private_write_evidence.database_path
            != paper.private_write_evidence.database_path)
    assert paper.caller_policies[0].approval_mode == "explicit-user"


# ─── 검증 경로 (2026-08-28) ──────────────────────────
#
# 첫 실행에서 ④단계 `invalid_bundle_file`로 멈췄다. 로더는 번들이
# `private_root/private-write-activation.json` **바로 그 경로**일 것을 요구하는데,
# 스테이징의 후보를 기본 private_root로 검증하려 했기 때문이다.
# (설치 전에 멈춘 것 자체는 의도대로였다 — 아무것도 바뀌지 않았다.)

import inspect  # noqa: E402


def test_candidate_is_verified_against_its_own_staging_root():
    src = inspect.getsource(pr.reissue)
    assert "verify(candidate_path, private_root=candidate_path.parent)" in src


def test_the_installed_bundle_is_verified_at_the_operational_path():
    """설치본은 기본 private_root로 봐야 한다 — 스테이징으로 보면 의미가 없다."""
    src = inspect.getsource(pr.reissue)
    assert "verify(path)" in src


def test_a_bad_install_is_rolled_back():
    """검증에 실패한 번들을 그대로 두면 다음 재시작에서 전 서비스가 잠긴다."""
    src = inspect.getsource(pr.reissue)
    assert "shutil.copy2(kept, path)" in src
    assert "되돌렸습니다" in src
