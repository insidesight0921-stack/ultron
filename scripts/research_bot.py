"""research_bot.py — RAG 미스 시 웹 검색→정리→wiki 저장→답변 (v1)

흐름: knowledge_bot RAG가 위키에서 충분히 못 찾으면(거리 임계 초과) 이 모듈이
웹(DuckDuckGo 무료)을 검색해 상위 결과를 모아 로컬 Gemma로 정리하고,
raw/inbox에 노트로 저장(→watch_raw가 wiki로 정제)한 뒤 답변을 돌려준다.

설계:
  - 외부 호출(_ddg_search_raw / _fetch_page / _summarize / save_fn)은 전부 주입/분리
    → 단위 테스트는 monkeypatch(네트워크·LLM 불필요).
  - 완전 로컬 지향: 검색은 API키 없는 DuckDuckGo HTML, 요약은 Gemma 31B.
  - graceful: 검색 0건/네트워크 실패 시 정중히 안내(저장 안 함).

주의: DDG HTML 스크래핑은 변동 가능 — 첫 실환경에서 검증 필요.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

log = logging.getLogger("research_bot")

OLLAMA_URL = "http://127.0.0.1:11434"
LLM_MODEL = "gemma4:31b"
KEEP_ALIVE = "30m"

# RAG 거리(bge-m3 cosine, 낮을수록 유사)가 이 값보다 크면 "위키에 없음"으로 판단
RAG_WEAK_THRESHOLD = 0.50
TOP_RESULTS = 4
MAX_PAGE_CHARS = 2500


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str


# ─── RAG 신뢰도 판정 ─────────────────────────────────


def rag_is_weak(chunks: list, threshold: float = RAG_WEAK_THRESHOLD) -> bool:
    """검색 청크가 비었거나 최소 거리가 임계 초과면 '위키에 없음'."""
    if not chunks:
        return True
    dists = []
    for c in chunks:
        d = c.get("distance") if isinstance(c, dict) else getattr(c, "distance", None)
        if d is not None:
            dists.append(d)
    if not dists:
        return True
    return min(dists) > threshold


# ─── 자기참조/개인 질문 판정 (웹 폴백 가드, v1.1) ─────
# 봇 자신·사용자 개인 상태에 대한 질문이면 웹 검색으로 새지 않게 한다.
# (system_info 도구가 못 잡은 잔여 케이스까지 knowledge 답변에 머물게)
_SELF_REF_RE = re.compile(
    r"(?:^|\s)(?:내|나의|우리|제)(?:\s|$)|"
    r"시스템|봇|리밸런싱|포지션|슬롯|주소|포트|자동\s*작업|자동화"
)


def is_self_referential(query: str) -> bool:
    """자기참조/개인 질문이면 True → 웹 검색 금지(위키 답변에 머묾)."""
    if not query:
        return False
    return bool(_SELF_REF_RE.search(query))


# ─── 웹 검색 (DuckDuckGo 무료, 주입 가능) ─────────────


def _ddg_search_raw(query: str) -> str:
    """DuckDuckGo HTML 검색 결과 페이지 원문. 외부 호출 — 테스트는 monkeypatch."""
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (research_bot)"})
    with urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_ddg(html: str, limit: int = TOP_RESULTS) -> list[WebResult]:
    """DDG HTML → WebResult 리스트. bs4 있으면 사용, 없으면 정규식 fallback."""
    out: list[WebResult] = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for res in soup.select(".result, .web-result"):
            a = res.select_one("a.result__a")
            if not a:
                continue
            title = a.get_text(strip=True)
            href = a.get("href", "")
            sn = res.select_one(".result__snippet")
            snippet = sn.get_text(strip=True) if sn else ""
            if title and href:
                out.append(WebResult(title, href, snippet))
            if len(out) >= limit:
                break
    except ImportError:
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL):
            href = m.group(1)
            title = re.sub("<[^>]+>", "", m.group(2)).strip()
            if title and href:
                out.append(WebResult(title, href, ""))
            if len(out) >= limit:
                break
    return out


def _fetch_page(url: str) -> str:
    """결과 페이지 본문 텍스트(태그 제거, 길이 제한). 외부 호출 — monkeypatch."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (research_bot)"})
        with urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"page fetch 실패 {url}: {e}")
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header"]):
            t.decompose()
        text = soup.get_text(" ", strip=True)
    except ImportError:
        text = re.sub("<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)[:MAX_PAGE_CHARS]


# ─── 요약 (Gemma, 주입 가능) ─────────────────────────


def _summarize(query: str, results: list[WebResult], pages: list[str]) -> str:
    """검색 결과+본문 → 한국어 정리 노트(마크다운 본문). 외부 호출 — monkeypatch."""
    src = "\n\n".join(
        f"[{i+1}] {r.title}\n{r.url}\n{(pages[i] if i < len(pages) else r.snippet)[:1200]}"
        for i, r in enumerate(results)
    )
    prompt = (
        f"다음 웹 검색 결과를 바탕으로 '{query}'에 대해 한국어로 정리해라.\n"
        "핵심 개념·사실 위주로 5~10줄. 마지막에 '출처:' 줄에 URL들을 나열.\n"
        "추측은 피하고 결과에 있는 내용만.\n\n" + src
    )
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0.3, "num_ctx": 16384, "num_predict": 800},
    }).encode("utf-8")
    req = Request(f"{OLLAMA_URL}/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"].strip()


# ─── 오케스트레이션 ──────────────────────────────────


def research(query: str,
             save_fn: Optional[Callable[[str, str], object]] = None,
             search_fn: Callable[[str], str] = None,
             page_fn: Callable[[str], str] = None,
             summarize_fn: Callable = None) -> str:
    """웹 검색 → 정리 → (save_fn으로) inbox 저장 → 답변 문자열.

    save_fn(prefix, content): inbox 저장 함수(telegram의 save_to_inbox 주입).
    나머지 *_fn은 테스트 주입용(기본은 실제 구현).
    """
    search_fn = search_fn or _ddg_search_raw
    page_fn = page_fn or _fetch_page
    summarize_fn = summarize_fn or _summarize
    if not query or not query.strip():
        return "검색할 내용이 없습니다."
    try:
        html = search_fn(query)
        results = parse_ddg(html)
    except Exception as e:
        log.warning(f"웹 검색 실패: {e}")
        return "위키에 없고 웹 검색도 실패했습니다. 잠시 후 다시 시도해 주세요."
    if not results:
        return "위키에 없고 웹에서도 관련 결과를 찾지 못했습니다."
    pages = []
    for r in results:
        pages.append(page_fn(r.url) or r.snippet)
    try:
        summary = summarize_fn(query, results, pages)
    except Exception as e:
        log.warning(f"요약 실패: {e}")
        # 요약 실패해도 결과 링크는 제공
        summary = "웹 검색 결과:\n" + "\n".join(f"- {r.title} {r.url}" for r in results)
    # wiki 저장 (inbox → watch_raw 정제)
    saved_note = ""
    if save_fn:
        try:
            note = f"# {query}\n\n## 📖 내용 (웹 검색 자동 정리)\n{summary}\n"
            save_fn(_safe_prefix(query), note)
            saved_note = "\n\n📝 위키에 저장했습니다(곧 정제됨)."
        except Exception as e:
            log.warning(f"wiki 저장 실패: {e}")
    return (f"🔎 위키에 없어 웹에서 찾아 정리했어요:\n\n{summary}{saved_note}")


def _safe_prefix(query: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣 _-]", "", query)[:40].strip().replace(" ", "_") or "웹검색"
