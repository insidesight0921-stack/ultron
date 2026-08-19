#!/usr/bin/env python3
"""
raw/inbox/ 저장 + 외부 컨텐츠 추출 헬퍼.

telegram_bot.py가 메모·외부 콘텐츠 수집에 사용.
- save_to_inbox(): 텍스트 → raw/inbox/{prefix}_{ts}.md
- extract_url_content(): URL → trafilatura로 본문 추출
- extract_pdf_text(): PDF 파일 → pdfminer.six로 텍스트 추출

watch_raw가 raw/inbox/ 변경 감지 → refine_raw 자동 호출 → wiki/ 저장.
"""
from __future__ import annotations
import logging
import re
from datetime import datetime
from pathlib import Path

HOME = Path.home()
VAULT = HOME / "울트론" / "obsidian-vault"
RAW_INBOX = VAULT / "raw" / "inbox"

log = logging.getLogger("inbox")


# ─── 저장 ────────────────────────────────────────────

def save_to_inbox(content: str, prefix: str = "메모", source: str = "unknown") -> Path:
    """raw/inbox/에 타임스탬프 파일로 저장. watch_raw가 자동 픽업.

    파라미터:
      content: 저장할 본문 (메타 헤더는 자동 추가)
      prefix:  파일명 prefix (제목 또는 종류). 부적합 문자 자동 치환.
      source:  메타 헤더의 출처 (예: "telegram /note")
    """
    RAW_INBOX.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe_prefix = re.sub(r"[\\/:*?\"<>|\s]+", "_", prefix)[:40].strip("_") or "메모"
    target = RAW_INBOX / f"{safe_prefix}_{ts}.md"

    # 같은 초에 두 번 들어오면 충돌 방지
    i = 1
    while target.exists():
        i += 1
        target = RAW_INBOX / f"{safe_prefix}_{ts}_{i}.md"

    body = (
        f"{content.strip()}\n\n"
        f"---\n"
        f"출처: {source}\n"
        f"수집일: {datetime.now().isoformat(timespec='seconds')}\n"
    )
    target.write_text(body, encoding="utf-8")
    log.info(f"💾 inbox 저장: {target.relative_to(VAULT)} ({len(content)} chars)")
    return target


# ─── URL 추출 ────────────────────────────────────────

def extract_url_content(url: str) -> tuple[str, str] | None:
    """trafilatura로 URL 본문 추출. (title, body) 반환. 실패 시 None."""
    try:
        import trafilatura
    except ImportError:
        log.error("trafilatura 미설치 — pip install trafilatura")
        return None

    try:
        downloaded = trafilatura.fetch_url(url, no_ssl=False)
        if not downloaded:
            return None
        meta = trafilatura.extract_metadata(downloaded)
        title = (meta.title if meta else None) or url
        body = trafilatura.extract(
            downloaded,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_links=False,
            no_fallback=False,
        )
        if not body or len(body.strip()) < 50:
            return None
        return (title, body)
    except Exception as e:
        log.exception(f"URL 추출 실패: {url} — {e}")
        return None


# ─── PDF 추출 ────────────────────────────────────────

def extract_pdf_text(path: Path) -> str | None:
    """PDF 파일 → 텍스트 (pdfminer.six). 실패 시 None.

    한국어 텍스트 잘 처리. 이미지 OCR은 미지원 (스캔된 PDF는 빈 결과).
    """
    try:
        from pdfminer.high_level import extract_text
    except ImportError:
        log.error("pdfminer.six 미설치 — pip install pdfminer.six")
        return None

    try:
        text = extract_text(str(path))
        text = (text or "").strip()
        if len(text) < 50:
            log.warning(f"PDF 텍스트 너무 짧음 ({len(text)}자) — 스캔 PDF일 수 있음: {path.name}")
            return None
        return text
    except Exception as e:
        log.exception(f"PDF 추출 실패: {path} — {e}")
        return None
