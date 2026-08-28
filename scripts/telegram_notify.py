"""telegram_notify.py — 스탠드얼론 스크립트용 텔레그램 발송 (순수 + 얇은 경계)

launchd로 도는 스크립트(주간 스캔·월간 리포트 등)는 봇 프로세스와 별개로 실행되므로
Bot API를 직접 부른다. 그 코드가 스크립트마다 복사되면 재시도·분할·오류 처리가
제각각으로 갈라진다. 여기 한 곳에 둔다.

  - 분할은 순수 함수(`split_chunks`)로 두고 테스트한다. 4096자 제한을 넘기면
    텔레그램이 **메시지 전체를 거절**하므로, 길이 계산이 틀리면 리포트가 통째로 사라진다.
  - `parse_mode`는 기본 없음(평문)이다. 성과 리포트에는 `-`, `_`, `*`가 흔한데
    Markdown으로 보내면 텔레그램이 파싱 오류로 거절한다. 서식이 필요한 곳만 명시한다.
  - 발송 실패는 예외를 올리지 않고 건수로 돌려준다 — 알림이 안 갔다고 리포트 생성까지
    실패시킬 이유가 없다. 노트는 이미 파일로 남아 있다.
  - **`.env`를 여기서 읽는다.** 봇 프로세스는 기동 시 `.env`를 읽지만 launchd가 띄우는
    스크립트는 그 환경을 물려받지 못한다. 스크립트마다 로딩을 기억해 붙이는 대신,
    자격 정보를 실제로 쓰는 이 모듈이 필요할 때 읽는다. 이미 설정된 값은 덮지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("telegram_notify")

API = "https://api.telegram.org/bot{token}/sendMessage"
LIMIT = 4000          # 텔레그램 상한 4096 — 여유를 둔다
TIMEOUT = 20


# ─── 순수 ────────────────────────────────────────────


def split_chunks(text: str, limit: int = LIMIT) -> list[str]:
    """긴 메시지를 상한 이하로 나눈다. **줄 경계를 우선 지킨다.**

    글자 수로만 자르면 표가 중간에서 끊겨 읽을 수 없게 된다. 한 줄이 상한보다 길면
    그때만 글자 수로 자른다.
    """
    if not text:
        return []
    out: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:            # 한 줄이 통째로 너무 길 때만
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:limit])
            line = line[limit:]
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > limit:
            out.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


def chat_ids_from(raw: Optional[str]) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


# ─── 경계 ────────────────────────────────────────────


ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "ALLOWED_TELEGRAM_USER_ID")


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


def default_env_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def ensure_env(path=None) -> None:
    """자격 정보가 없으면 `.env`에서 채운다. **이미 있는 값은 덮지 않는다.**

    launchd가 띄운 스크립트는 봇 프로세스의 환경을 물려받지 못한다.
    python-dotenv가 없어도 동작하도록 직접 읽는다(의존성을 하나 더 만들 이유가 없다).
    """
    if all(os.environ.get(k, "").strip() for k in ENV_KEYS):
        return
    try:
        text = Path(path or default_env_path()).read_text(encoding="utf-8")
    except OSError as exc:
        # 파일이 없거나 못 읽는 경우만 조용히 넘어간다. 넓게 잡으면 코드 오류까지
        # ".env 없음"으로 보여서, 발송이 왜 안 되는지 알 수 없게 된다.
        log.debug(".env 읽기 실패: %s", exc)
        return
    values = parse_env_file(text)
    for key in ENV_KEYS:
        if not os.environ.get(key, "").strip() and values.get(key):
            os.environ[key] = values[key]


def _post(token: str, chat_id: str, text: str, parse_mode: Optional[str]) -> bool:
    payload: dict = {"chat_id": chat_id, "text": text,
                     "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    req = urllib.request.Request(
        API.format(token=token), data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
        if not result.get("ok"):
            log.warning("텔레그램 응답 오류: %s", result)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("텔레그램 전송 실패 (chat_id=%s): %s", chat_id, exc)
        return False


def send(text: str, *, parse_mode: Optional[str] = None,
         token: Optional[str] = None, chat_ids: Optional[Iterable[str]] = None,
         poster=_post, env_path=None) -> int:
    """등록된 모든 chat_id에 보낸다. **보낸 조각 수**를 돌려준다(0이면 발송 안 됨).

    환경변수가 없으면 조용히 0을 돌려준다 — 토큰 없는 환경에서 리포트 생성이
    실패하면 안 된다.
    """
    if token is None or chat_ids is None:
        ensure_env(env_path)
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    ids = list(chat_ids) if chat_ids is not None else chat_ids_from(
        os.environ.get("ALLOWED_TELEGRAM_USER_ID"))
    if not token or not ids:
        log.warning("텔레그램 환경변수 없음 — 발송 건너뜀")
        return 0
    parts = split_chunks(text)
    sent = 0
    for cid in ids:
        for part in parts:
            if poster(token, cid, part, parse_mode):
                sent += 1
    return sent
