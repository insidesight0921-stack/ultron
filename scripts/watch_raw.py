#!/usr/bin/env python3
"""
raw/ 폴더 백그라운드 감시 → 자동 정제 + 재인덱싱 데몬.

흐름:
  1. watchdog으로 raw/ 변경 감지 (생성/수정)
  2. 변경 파일을 pending set에 추가
  3. 마지막 변경 후 IDLE_SEC 동안 추가 변경 없으면 배치 처리 (디바운스)
  4. 또는 강제 주기 BATCH_INTERVAL_MIN 도달하면 처리
  5. refine_raw.py --auto 호출 → wiki/ 저장
  6. index_wiki.py 호출 → LanceDB 재인덱싱

사용:
    # 포그라운드 (Ctrl+C로 종료)
    python watch_raw.py

    # 백그라운드 데몬으로 (nohup)
    nohup python watch_raw.py > /tmp/watch_raw.log 2>&1 &

    # 다른 주기로
    python watch_raw.py --idle-sec 600 --batch-min 60
"""
from __future__ import annotations
import argparse
import logging
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


def to_nfc(p) -> Path:
    """
    macOS APFS는 한국어 파일명을 NFD로 저장.
    Python에서 만든 경로는 NFC.
    relative_to() 비교 등은 정확한 문자열 매칭이라 정규화 필수.
    """
    return Path(unicodedata.normalize("NFC", str(p)))

HOME = Path.home()
PROJECT = HOME / "울트론" / "ai-agent"
VAULT = HOME / "울트론" / "obsidian-vault"
RAW = VAULT / "raw"
SCRIPTS = PROJECT / "scripts"
LOG_FILE = PROJECT / "data" / "watch_raw.log"

REFINE_SCRIPT = SCRIPTS / "refine_raw.py"
INDEX_SCRIPT = SCRIPTS / "index_wiki.py"

# 디바운스: 마지막 변경 후 이 초만큼 조용하면 처리
DEFAULT_IDLE_SEC = 300  # 5분
# 강제 처리 주기: 변경이 계속 들어와도 이만큼 지나면 처리
DEFAULT_BATCH_MIN = 30  # 30분


# ─── 로깅 ───────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("watch_raw")


# ─── 변경 추적 ──────────────────────────────────────

class PendingChanges:
    def __init__(self):
        self._files: set[Path] = set()
        self._lock = Lock()
        self._last_change = time.time()
        self._first_change_in_batch: float | None = None

    def add(self, path: Path) -> None:
        with self._lock:
            self._files.add(path)
            self._last_change = time.time()
            if self._first_change_in_batch is None:
                self._first_change_in_batch = self._last_change
            log.info(f"📝 변경 감지: {path.relative_to(VAULT)}")

    def drain(self) -> set[Path]:
        with self._lock:
            files = self._files.copy()
            self._files.clear()
            self._first_change_in_batch = None
            return files

    def should_process(self, idle_sec: int, batch_sec: int) -> bool:
        with self._lock:
            if not self._files:
                return False
            now = time.time()
            silent_for = now - self._last_change
            since_first = now - (self._first_change_in_batch or now)
            return silent_for >= idle_sec or since_first >= batch_sec

    def count(self) -> int:
        with self._lock:
            return len(self._files)


# ─── 파일 이벤트 핸들러 ─────────────────────────────

class RawHandler(FileSystemEventHandler):
    def __init__(self, pending: PendingChanges):
        self.pending = pending

    def _is_target(self, path: str) -> bool:
        p = Path(path)
        # .md만, 숨김 파일 제외, .gitkeep 제외
        return (
            p.suffix == ".md"
            and not p.name.startswith(".")
            and p.name != ".gitkeep"
        )

    def on_created(self, event):
        if not event.is_directory and self._is_target(event.src_path):
            self.pending.add(to_nfc(event.src_path))

    def on_modified(self, event):
        if not event.is_directory and self._is_target(event.src_path):
            self.pending.add(to_nfc(event.src_path))

    def on_moved(self, event):
        if not event.is_directory and self._is_target(event.dest_path):
            self.pending.add(to_nfc(event.dest_path))


# ─── 처리 루프 ──────────────────────────────────────

def process_batch(files: set[Path]) -> None:
    """변경된 파일들 → refine_raw → index_wiki"""
    if not files:
        return

    log.info(f"🔧 배치 처리 시작: {len(files)}개 파일")
    success, failed = 0, 0

    for f in sorted(files):
        if not f.exists():
            log.warning(f"  ⚠️  사라진 파일 스킵: {f.name}")
            continue
        try:
            log.info(f"  → 정제: {f.relative_to(VAULT)}")
            result = subprocess.run(
                [
                    sys.executable, str(REFINE_SCRIPT),
                    str(f.relative_to(VAULT)),
                    "--auto",
                ],
                cwd=str(PROJECT),
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode == 0:
                success += 1
                # refine_raw 출력 마지막 줄 (저장 경로) 로그에 기록
                tail = [l for l in result.stdout.strip().splitlines() if l.strip()][-3:]
                for l in tail:
                    log.info(f"    {l}")
            else:
                failed += 1
                log.error(f"  ❌ 정제 실패 (rc={result.returncode}): {f.name}")
                log.error(f"     stderr: {result.stderr[:300]}")
        except subprocess.TimeoutExpired:
            failed += 1
            log.error(f"  ⏰ 타임아웃 (180초 초과): {f.name}")
        except Exception as e:
            failed += 1
            log.error(f"  ❌ 예외: {f.name} — {e}")

    log.info(f"📊 정제 완료: {success} 성공 / {failed} 실패")

    # 인덱스 재실행 (변경된 wiki 파일 자동 감지)
    if success > 0:
        log.info("🔄 LanceDB 재인덱싱 중...")
        try:
            result = subprocess.run(
                [sys.executable, str(INDEX_SCRIPT)],
                cwd=str(PROJECT),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                # 마지막 줄 (📊 결과)
                stat_line = [l for l in result.stdout.strip().splitlines() if "결과:" in l]
                if stat_line:
                    log.info(f"  {stat_line[0].strip()}")
                else:
                    log.info("  ✅ 인덱싱 완료")
            else:
                log.error(f"  ❌ 인덱싱 실패: {result.stderr[:300]}")
        except Exception as e:
            log.error(f"  ❌ 인덱싱 예외: {e}")


def watcher_loop(pending: PendingChanges, stop: Event, idle_sec: int, batch_sec: int) -> None:
    """주기적으로 pending 확인 → 조건 충족 시 처리"""
    while not stop.is_set():
        if pending.should_process(idle_sec, batch_sec):
            files = pending.drain()
            try:
                process_batch(files)
            except Exception as e:
                log.exception(f"배치 처리 중 예외: {e}")
        # 10초마다 확인
        stop.wait(10)


# ─── 메인 ───────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle-sec", type=int, default=DEFAULT_IDLE_SEC,
                    help=f"마지막 변경 후 처리까지 대기 (초). 기본 {DEFAULT_IDLE_SEC}")
    ap.add_argument("--batch-min", type=int, default=DEFAULT_BATCH_MIN,
                    help=f"강제 처리 주기 (분). 기본 {DEFAULT_BATCH_MIN}")
    ap.add_argument("--once", action="store_true",
                    help="시작 시 raw/ 의 모든 미처리 파일 1회 처리하고 종료")
    args = ap.parse_args()

    if not RAW.exists():
        sys.exit(f"❌ raw 폴더 없음: {RAW}")
    if not REFINE_SCRIPT.exists():
        sys.exit(f"❌ refine_raw.py 없음: {REFINE_SCRIPT}")

    log.info("=" * 60)
    log.info(f"watch_raw 시작 — {datetime.now().isoformat(timespec='seconds')}")
    log.info(f"  감시 폴더: {RAW}")
    log.info(f"  IDLE: {args.idle_sec}s / BATCH: {args.batch_min}min")
    log.info("=" * 60)

    if args.once:
        log.info("🔁 --once 모드: 미처리 파일 1회 처리")
        result = subprocess.run(
            [sys.executable, str(REFINE_SCRIPT), "--all", "--auto"],
            cwd=str(PROJECT),
            capture_output=False,
        )
        if result.returncode == 0:
            subprocess.run(
                [sys.executable, str(INDEX_SCRIPT)],
                cwd=str(PROJECT),
            )
        return

    pending = PendingChanges()
    handler = RawHandler(pending)
    observer = Observer()
    observer.schedule(handler, str(RAW), recursive=True)
    observer.start()

    stop = Event()

    def shutdown(signum, frame):
        log.info(f"\n🛑 종료 시그널 수신 ({signum}) — 마지막 배치 처리 후 종료")
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    worker = Thread(
        target=watcher_loop,
        args=(pending, stop, args.idle_sec, args.batch_min * 60),
        daemon=False,
    )
    worker.start()

    try:
        while not stop.is_set():
            stop.wait(1)
    finally:
        observer.stop()
        observer.join()
        # 종료 직전 마지막 배치 처리
        remaining = pending.drain()
        if remaining:
            log.info(f"종료 전 마지막 배치 ({len(remaining)}개)")
            process_batch(remaining)
        worker.join(timeout=5)
        log.info("✅ watch_raw 종료")


if __name__ == "__main__":
    main()
