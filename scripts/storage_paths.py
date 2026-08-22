#!/usr/bin/env python3
"""AI Agent 데이터 물리 경계의 단일 경로 정의.

설정 파일이 없거나 유효하지 않으면 기존 ``data/`` 구조를 그대로 사용한다. 향후
마이그레이션 검증이 끝난 뒤 ``data/storage-layout.json``의 layout을
``private-v1``으로 바꾸고 서비스를 재시작하면 새 경계를 사용한다.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path


LEGACY_LAYOUT = "legacy"
PRIVATE_V1_LAYOUT = "private-v1"
VALID_LAYOUTS = {LEGACY_LAYOUT, PRIVATE_V1_LAYOUT}
LAYOUT_ENV = "AI_AGENT_STORAGE_LAYOUT"
LAYOUT_FILE_NAME = "storage-layout.json"


@dataclass(frozen=True)
class StoragePaths:
    project: Path
    layout: str = LEGACY_LAYOUT

    @property
    def data_root(self) -> Path:
        return self.project / "data"

    @property
    def private_root(self) -> Path:
        return self.data_root if self.layout == LEGACY_LAYOUT else self.data_root / "private"

    @property
    def shareable_root(self) -> Path:
        return self.data_root if self.layout == LEGACY_LAYOUT else self.data_root / "shareable"

    @property
    def paper_db(self) -> Path:
        return self.private_root / "paper.db"

    @property
    def schedule_db(self) -> Path:
        name = "schedule.db" if self.layout == LEGACY_LAYOUT else "assistant.db"
        return self.private_root / name

    @property
    def watchlist_db(self) -> Path:
        name = "private.db" if self.layout == LEGACY_LAYOUT else "assistant.db"
        return self.private_root / name

    @property
    def rag_dir(self) -> Path:
        name = "lancedb" if self.layout == LEGACY_LAYOUT else "rag"
        return self.private_root / name

    @property
    def private_state_dir(self) -> Path:
        return self.data_root / "cache" if self.layout == LEGACY_LAYOUT else self.private_root / "state"

    @property
    def action_schedules(self) -> Path:
        if self.layout == LEGACY_LAYOUT:
            return self.data_root / "action_schedules.json"
        return self.private_state_dir / "action_schedules.json"

    @property
    def raw_processed(self) -> Path:
        if self.layout == LEGACY_LAYOUT:
            return self.data_root / "raw_processed.json"
        return self.private_state_dir / "raw_processed.json"

    @property
    def logs_dir(self) -> Path:
        return self.private_root / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.data_root / "reports" if self.layout == LEGACY_LAYOUT else self.private_root / "reports"

    @property
    def watch_raw_log(self) -> Path:
        return self.data_root / "watch_raw.log" if self.layout == LEGACY_LAYOUT else self.logs_dir / "watch_raw.log"

    @property
    def shareable_cache_dir(self) -> Path:
        return self.data_root / "cache" if self.layout == LEGACY_LAYOUT else self.shareable_root / "cache"

    @property
    def shareable_samples_dir(self) -> Path:
        return self.data_root / "ipo_samples" if self.layout == LEGACY_LAYOUT else self.shareable_root / "samples"

    @property
    def dart_finance_validation(self) -> Path:
        if self.layout == LEGACY_LAYOUT:
            return self.data_root / "dart_finance_validation.csv"
        return self.shareable_samples_dir / "dart_finance_validation.csv"

    @property
    def ipo_demand_validation(self) -> Path:
        if self.layout == LEGACY_LAYOUT:
            return self.data_root / "ipo_demand_validation.csv"
        return self.shareable_samples_dir / "ipo_demand_validation.csv"

    def private_state_file(self, name: str) -> Path:
        return self.private_state_dir / name

    def shareable_cache_file(self, name: str) -> Path:
        return self.shareable_cache_dir / name


def _layout_from_file(project: Path) -> str:
    config = project / "data" / LAYOUT_FILE_NAME
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
        value = str(payload.get("layout", "")).strip()
        return value if value in VALID_LAYOUTS else LEGACY_LAYOUT
    except (OSError, ValueError, TypeError):
        return LEGACY_LAYOUT


def active_layout(project: Path, *, environ: dict[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    override = str(environ.get(LAYOUT_ENV, "")).strip()
    if override:
        return override if override in VALID_LAYOUTS else LEGACY_LAYOUT
    return _layout_from_file(project)


def get_paths(project: Path | None = None, *, layout: str | None = None) -> StoragePaths:
    project = (project or Path(__file__).resolve().parent.parent).resolve()
    selected = layout or active_layout(project)
    if selected not in VALID_LAYOUTS:
        raise ValueError(f"지원하지 않는 storage layout: {selected}")
    return StoragePaths(project=project, layout=selected)


PATHS = get_paths()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "field",
        choices=(
            "layout",
            "paper_db",
            "schedule_db",
            "watchlist_db",
            "rag_dir",
            "private_state_dir",
            "logs_dir",
            "reports_dir",
            "shareable_cache_dir",
            "shareable_samples_dir",
        ),
    )
    args = parser.parse_args()
    if args.field == "layout":
        print(PATHS.layout)
    else:
        print(getattr(PATHS, args.field))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
