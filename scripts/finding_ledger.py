"""finding_ledger.py — 측정 결과를 남기는 **추가만 되는 원장** (공용).

2026-09-02에 같은 것을 두 번 만들 뻔했다. 국면 판정 원장(`phase_validation`)을
만들고, 나우캐스팅 판정도 화면에 이어야 해서 똑같은 것이 필요해졌다.
**두 곳에서 만들면 한쪽만 바뀌는 날이 온다** — 그날 임계값 측정과 운영이
서로 다른 값을 읽던 일을 겪었다. 그래서 한 곳에 둔다.

규칙 셋:

1. **추가만 한다.** 지난 판정을 고치지 않는다. 무엇이 언제 바뀌었는지가
   원장의 존재 이유다.
2. **같은 결과는 다시 적지 않는다.** 새 표본이 없는 재실행은 새 판정이
   아니다. 같은 값이 반복되면 변화 지점이 묻힌다.
3. **기록이 없는 것과 검증된 것은 다르다.** 없으면 「미측정」이라고 말한다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("finding_ledger")

# 시각은 판정 내용이 아니다 — 같은지 볼 때 뺀다.
TIME_KEYS = ("at",)


def same_finding(a: Optional[dict], b: Optional[dict],
                 *, ignore=TIME_KEYS) -> bool:
    """시각을 빼고 **판정 내용이 같은가**(순수)."""
    if not a or not b:
        return False
    keys = {k for k in list(a) + list(b) if k not in ignore}
    return all(a.get(k) == b.get(k) for k in keys)


def load(path) -> list:
    """원장 전체. 없거나 깨졌으면 빈 목록."""
    try:
        got = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return got if isinstance(got, list) else []


def latest(path) -> Optional[dict]:
    """마지막 판정. 없으면 None."""
    log_rows = load(path)
    return log_rows[-1] if log_rows else None


def append(record: dict, path, *, ignore=TIME_KEYS) -> tuple[list, bool]:
    """새 판정이면 붙이고, 같으면 그대로 둔다. 반환: (원장, 붙였는가)."""
    path = Path(path)
    rows = load(path)
    if rows and same_finding(rows[-1], record, ignore=ignore):
        return rows, False
    rows = rows + [record]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("원장 쓰기 실패(무시): %s", e)
        return load(path), False
    return rows, True
