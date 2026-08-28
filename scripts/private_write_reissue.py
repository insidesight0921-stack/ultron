#!/usr/bin/env python3
"""private_write_reissue.py — Private write 활성화 번들 재발급 (운영자 CLI)

**왜 필요한가.** 활성화 번들은 백업 manifest의 *내용 해시*를 지문에 넣는다. 그래서
백업을 새로 뜨면 번들의 `bundle_id`가 반드시 바뀐다. 그런데 기동 게이트는 백업이
24시간 이내일 것을 요구한다. 재발급 수단이 없으면 백업이 24시간을 넘긴 순간부터
텔레그램 봇과 Private API가 **재시작 시 올라오지 못한다.**

2026-08-28에 실제로 그렇게 됐다. 봇은 떠 있는 동안은 멀쩡했고, 서비스를 재시작한
순간 `readiness:backup_fresh`로 죽었다. 장 중 손절·익절 모니터가 이 봇 프로세스
안에서 돌기 때문에, 이 상태를 방치하면 다음 개장에 손절 감시가 없다.

**보안 게이트는 건드리지 않는다.** 기존 후보 생성 경로
(`generate_paper_write_activation_candidate`)로 새 번들을 만들고, **진짜 로더로
검증한 뒤에만** 설치한다. 검증에 실패하면 아무것도 바꾸지 않는다. 지문 계산을
여기서 흉내 내지 않으므로, 계산이 틀리면 조용히 나쁜 번들이 깔리는 일이 없다.

사용법:
    python3 scripts/private_write_reissue.py --check      # 진단만 (아무것도 안 바꿈)
    python3 scripts/private_write_reissue.py --backup     # 새 백업 + 재발급 + 검증
    python3 scripts/private_write_reissue.py              # 최신 백업으로 재발급만
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from private_data_security import default_paths as default_security_paths  # noqa: E402
from private_paper_write_candidate import (  # noqa: E402
    generate_paper_write_activation_candidate,
)
from private_paper_write_readiness import (  # noqa: E402
    PaperCallerPolicy,
    PaperWriteReadinessEvidence,
)
from private_schedule_write_readiness import (  # noqa: E402
    ScheduleWriteReadinessEvidence,
)
from private_write_readiness import (  # noqa: E402
    DEFAULT_MAX_BACKUP_AGE,
    PrivateWriteReadinessEvidence,
    assess_private_write_readiness,
)
from private_write_runtime import (  # noqa: E402
    BUNDLE_PATH_ENV,
    load_private_write_runtime_bundle,
)
from storage_paths import get_paths  # noqa: E402

PROJECT = SCRIPTS.parent
PATHS = get_paths(PROJECT)


class ReissueError(RuntimeError):
    pass


# ─── 조회 ────────────────────────────────────────────


def bundle_path() -> Path:
    configured = os.environ.get(BUNDLE_PATH_ENV, "").strip()
    if not configured:
        raise ReissueError(f"{BUNDLE_PATH_ENV} 미설정 — .env를 읽었는지 확인")
    return Path(configured)


def latest_manifest(backup_root: Path) -> Path:
    """승인된 백업 루트에서 가장 최근 manifest.json.

    스냅샷 디렉터리 이름이 타임스탬프라 사전순 정렬이 곧 시간순이다. 그래도
    이름을 믿지 않고 manifest의 `created_at`으로 다시 정렬한다 — 디렉터리를
    복사·이동하면 이름과 내용이 어긋날 수 있다.
    """
    candidates = []
    for manifest in backup_root.glob("*/manifest.json"):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(str(payload["created_at"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        candidates.append((created, manifest))
    if not candidates:
        raise ReissueError(f"백업을 찾지 못함: {backup_root}")
    candidates.sort()
    return candidates[-1][1]


def manifest_age(manifest: Path) -> timedelta | None:
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        created = datetime.fromisoformat(str(payload["created_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    now = datetime.now(created.tzinfo or timezone.utc)
    return now - created


# ─── 증거 재구성 ─────────────────────────────────────
#
# 번들 payload에는 필요한 값이 전부 들어 있다. **백업 경로 하나만 바꾸고**
# 나머지는 그대로 옮긴다 — 재발급이 정책을 조용히 바꾸는 통로가 되면 안 된다.


def _private_evidence(payload: dict, *, writer_owner: str, database_path: Path,
                      manifest: Path) -> PrivateWriteReadinessEvidence:
    return PrivateWriteReadinessEvidence(
        api_writer_enabled=bool(payload["api_writer_enabled"]),
        client_writes_enabled=bool(payload["client_writes_enabled"]),
        consumer_executor_enabled=bool(payload["consumer_executor_enabled"]),
        writer_owners=(writer_owner,),
        database_path=database_path.resolve(),
        backup_manifest_path=manifest,
        user_approval_required=bool(payload["user_approval_required"]),
        direct_db_fallback_disabled=bool(payload["direct_db_fallback_disabled"]),
        rollback_verified=bool(payload["rollback_verified"]),
    )


def build_evidence(payload: dict, manifest: Path):
    schedule = payload["schedule"]
    paper = payload["paper"]

    assistant_private = _private_evidence(
        payload, writer_owner=str(schedule["user_writer_owner"]),
        database_path=PATHS.watchlist_db, manifest=manifest)
    assistant = ScheduleWriteReadinessEvidence(
        private_write_evidence=assistant_private,
        notifier_enabled=bool(schedule["notifier_enabled"]),
        notifier_writer_owners=(str(schedule["notifier_writer_owner"]),),
        notifier_operations=tuple(schedule["notifier_operations"]),
        notifier_uses_same_database=bool(schedule["notifier_uses_same_database"]),
        telegram_direct_user_mutations_disabled=bool(
            schedule["telegram_direct_user_mutations_disabled"]),
        notifier_user_mutations_disabled=bool(
            schedule["notifier_user_mutations_disabled"]),
    )

    paper_private = _private_evidence(
        payload, writer_owner=str(paper["writer_owner"]),
        database_path=PATHS.paper_db, manifest=manifest)
    policies = tuple(
        PaperCallerPolicy(
            caller=str(item["caller"]),
            operations=tuple(item["operations"]),
            approval_mode=str(item["approval_mode"]),
        )
        for item in paper["caller_policies"]
    )
    paper_evidence = PaperWriteReadinessEvidence(
        private_write_evidence=paper_private,
        caller_policies=policies,
        consumers_use_private_api=bool(paper["consumers_use_private_api"]),
        consumers_use_same_database=bool(paper["consumers_use_same_database"]),
        paper_ui_direct_writes_disabled=bool(paper["paper_ui_direct_writes_disabled"]),
        telegram_direct_writes_disabled=bool(paper["telegram_direct_writes_disabled"]),
        operator_cli_rollback_only=bool(paper["operator_cli_rollback_only"]),
    )
    return assistant, paper_evidence


# ─── 진단 ────────────────────────────────────────────


def check() -> int:
    path = bundle_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    pinned = Path(str(payload["backup_manifest"]))
    root = default_security_paths().backup_root

    print(f"번들:        {path}")
    print(f"고정된 백업: {pinned.parent.name}")
    age = manifest_age(pinned)
    print(f"  나이:      {'?' if age is None else f'{age.total_seconds()/3600:.1f}시간'}"
          f"  (한도 {DEFAULT_MAX_BACKUP_AGE.total_seconds()/3600:.0f}시간)")
    try:
        newest = latest_manifest(root)
        newest_age = manifest_age(newest)
        print(f"최신 백업:   {newest.parent.name}"
              f"  ({'?' if newest_age is None else f'{newest_age.total_seconds()/3600:.1f}시간 전'})")
    except ReissueError as exc:
        print(f"최신 백업:   {exc}")

    assistant, _ = build_evidence(payload, pinned.resolve())
    report = assess_private_write_readiness(assistant.private_write_evidence)
    print(f"\n준비 상태:   "
          f"{'✅ ready' if report.ready else '❌ ' + ', '.join(report.failed_codes)}")
    if not report.ready:
        print("\n재발급하려면: python3 scripts/private_write_reissue.py --backup")
    return 0 if report.ready else 1


# ─── 재발급 ──────────────────────────────────────────


def run_backup() -> None:
    print("① 새 백업 생성 중 (온라인 백업 + 복구 검증)...")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "private_data_security.py"), "all"],
        cwd=str(PROJECT), capture_output=True, text=True)
    if result.returncode != 0:
        raise ReissueError(f"백업 실패:\n{result.stderr.strip()}")
    print(f"   {result.stdout.strip().splitlines()[-1]}")


def verify(candidate: Path) -> None:
    """**진짜 로더로** 검증한다. 여기서 통과하지 못하면 설치하지 않는다."""
    environ = dict(os.environ)
    environ[BUNDLE_PATH_ENV] = str(candidate)
    bundle = load_private_write_runtime_bundle(environ)
    if bundle is None:
        raise ReissueError("검증 실패: 번들이 비활성으로 읽힘")
    if not bundle.paper_writes_enabled:
        raise ReissueError("검증 실패: Paper 쓰기가 꺼진 번들")


def reissue(*, do_backup: bool) -> int:
    os.umask(0o077)
    path = bundle_path()
    payload = json.loads(path.read_text(encoding="utf-8"))

    if do_backup:
        run_backup()

    root = default_security_paths().backup_root
    manifest = latest_manifest(root).resolve()
    age = manifest_age(manifest)
    print(f"② 백업 선택: {manifest.parent.name} "
          f"({'?' if age is None else f'{age.total_seconds()/3600:.1f}시간 전'})")
    if age is not None and age > DEFAULT_MAX_BACKUP_AGE:
        raise ReissueError(
            f"가장 최근 백업도 한도를 넘었다 — --backup 으로 새로 뜨고 다시 실행")

    assistant, paper = build_evidence(payload, manifest)

    staging = Path(tempfile.mkdtemp(prefix="ultron-write-reissue-"))
    try:
        print("③ 후보 번들 생성·교차 검증...")
        candidate = generate_paper_write_activation_candidate(
            assistant, paper, staging)
        candidate_path = Path(candidate.path)
        print("④ 실제 로더로 검증...")
        verify(candidate_path)

        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        kept = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
        shutil.copy2(path, kept)
        os.chmod(kept, 0o600)
        print(f"⑤ 기존 번들 보존: {kept.name}")

        tmp = path.with_name(f"{path.name}.new")
        shutil.copy2(candidate_path, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)          # 원자적 교체
        print(f"⑥ 설치 완료: {path.name}")

        verify(path)
        print("⑦ 설치본 재검증 통과 ✅")
    except Exception:
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print("\n서비스를 다시 올리세요: ./scripts/agent_services.sh restart")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="진단만 한다 (아무것도 바꾸지 않음)")
    parser.add_argument("--backup", action="store_true",
                        help="재발급 전에 새 백업을 만든다")
    args = parser.parse_args()
    try:
        import telegram_notify
        telegram_notify.ensure_env()
        import env_config
        env_config.ensure_env([BUNDLE_PATH_ENV])
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ .env 로딩 실패: {exc}", file=sys.stderr)
    try:
        return check() if args.check else reissue(do_backup=args.backup)
    except ReissueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
