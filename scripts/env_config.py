"""env_config.py — `.env` 로딩 (순수 파싱 + 얇은 경계)

launchd가 띄우는 스크립트는 봇 프로세스의 환경을 물려받지 못한다. 같은 원인이
두 번 나왔다.

  1) 월간 텔레그램 리포트가 "환경변수 없음 — 발송 건너뜀"으로 조용히 끝났다.
  2) 대리 지표가 `.env`에 키가 **있는데도** "ECOS_API_KEY 미설정"을 찍었다.
     (ECOS·FRED·KRX 세 개가 한꺼번에 같은 이유로 실패했다.)

두 번 나왔으므로 스크립트마다 로딩을 기억해 붙이는 대신 한 곳에 둔다.

  - **이미 설정된 값은 덮지 않는다.** 운영 중 환경변수로 임시 교체하는 쪽이
    파일보다 우선이어야 한다.
  - **파일 오류(OSError)만 조용히 넘어간다.** 넓은 except는 코딩 오류까지
    ".env 없음"으로 위장시켜, 왜 안 되는지 알 수 없게 만든 적이 있다.
  - **여전히 비어 있는 키 목록을 돌려준다.** 호출부가 "무엇이 없어서 못 했는지"를
    그대로 말할 수 있어야 한다. 조용한 실패를 만들지 않기 위한 반환값이다.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("env_config")


# ─── 순수 ────────────────────────────────────────────


def parse_env_file(text: str) -> dict:
    """.env 텍스트 → {키: 값}(순수). 주석·빈 줄·따옴표를 처리한다."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            out[key] = val
    return out


# ─── 경계 ────────────────────────────────────────────


def default_env_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def ensure_env(keys: Iterable[str], path: Optional[object] = None) -> list[str]:
    """`keys` 중 비어 있는 값을 `.env`에서 채우고, **끝까지 빈 키 목록**을 돌려준다."""
    keys = tuple(keys)
    missing = [k for k in keys if not os.environ.get(k, "").strip()]
    if not missing:
        return []
    try:
        text = Path(path or default_env_path()).read_text(encoding="utf-8")
    except OSError as exc:
        log.debug(".env 읽기 실패: %s", exc)
        return missing
    values = parse_env_file(text)
    for key in missing:
        if values.get(key):
            os.environ[key] = values[key]
    return [k for k in keys if not os.environ.get(k, "").strip()]
