#!/usr/bin/env python3
"""
인박스봇 (inbox_bot) — 메모성 입력을 raw/inbox/에 자동 저장.

라우터(26B)가 사용자 메시지를 보고 "이건 메모/관찰/기록 의도다"라고 판단하면
이 봇으로 위임. 명시적 /note 명령은 기존대로 직접 처리(텔레그램 핸들러)이며,
inbox_bot은 자연어("이거 기록해줘", "내 일지에 추가") 처리 전용.

⚠️ 라우터는 보수적으로만 분기해야 함 — 헷갈리면 knowledge_bot으로 가서 사용자
   질문에 답하는 게 메모 잘못 저장하는 것보다 안전. 라우터 시스템 프롬프트에
   "확실히 메모 의도일 때만" 명시.

API:
  run(content: str, hint: str = "메모", chat_id: str | None = None,
      mode: str = "fast") -> tuple[str, list[dict]]

content는 라우터가 추출한 "저장할 본문" (사용자 입력에서 메타 동사 제거).
hint는 파일명 prefix 힌트 (예: "투자_관찰", "독서_노트").
"""
from __future__ import annotations

import logging

from inbox import save_to_inbox, VAULT  # 기존 헬퍼 재사용

log = logging.getLogger("inbox_bot")


def run(
    content: str,
    hint: str = "메모",
    chat_id: str | None = None,
    mode: str = "fast",
    **kwargs,
) -> tuple[str, list[dict]]:
    """라우터 entrypoint. (answer, sources=[]) 반환."""
    content = (content or "").strip()
    if not content:
        return ("❌ 저장할 내용이 비어있습니다.", [])

    hint = (hint or "메모").strip() or "메모"

    try:
        source = f"telegram inbox_bot chat_id={chat_id}" if chat_id else "inbox_bot"
        target = save_to_inbox(content, hint, source)
    except Exception as e:
        log.exception("inbox 저장 실패")
        return (f"❌ 저장 실패: {e}", [])

    rel = target.relative_to(VAULT) if str(target).startswith(str(VAULT)) else target.name
    return (
        f"✅ 메모 저장됨\n"
        f"📁 {rel}\n"
        f"📏 {len(content)}자\n"
        f"\n"
        f"5분 idle 또는 30분 batch에 자동으로 정제 → wiki/에 인덱싱됩니다.",
        [],
    )


# ─── CLI ─────────────────────────────────────────────


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("content")
    ap.add_argument("--hint", default="메모")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    msg, _ = run(args.content, hint=args.hint)
    print(msg)


if __name__ == "__main__":
    _cli()
