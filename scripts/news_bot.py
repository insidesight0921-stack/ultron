"""news_bot.py — IT/AI 뉴스 다이제스트 봇 (v1)

[[IT_뉴스_소스]] 워치 소스에서 최신 기사를 모아 로컬 LLM으로 헤드라인+3줄
요약 후 텔레그램으로 전송. 온디맨드(명령) + 정시 다이제스트 둘 다 지원.

설계 (기존 봇 패턴 계승):
  - 외부 호출(_fetch_rss_raw / _fetch_scrape_raw / _summarize_via_llm)은 분리
    → 테스트는 monkeypatch. 네트워크/LLM 없이 hermetic.
  - 빈 응답/예외 graceful skip (v3.21 가드). 소스 하나 죽어도 나머지는 진행.
  - 이미 보낸 기사(link)는 재전송 안 함 (롤링 dedup, 디스크 영속).
  - RSS 우선(안정), 스크래핑은 best-effort(셀렉터 변동 가능 — 0건이면 로그).

⚠️ 소스 URL은 첫 실행에서 per-source 건수를 보고 검증·보정할 것
   (python scripts/news_bot.py --check).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

from storage_paths import PATHS

log = logging.getLogger("news_bot")

OLLAMA_URL = "http://127.0.0.1:11434"
LLM_MODEL = "gemma4:31b"
KEEP_ALIVE = "30m"

_DATA_DIR = PATHS.private_root
_CACHE_DIR = PATHS.private_state_dir
_SEEN_PATH = PATHS.private_state_file("news_seen.json")
_SEEN_CAP = 600                 # 롤링 dedup 최대 보관 link 수
DEFAULT_PER_SOURCE = 4         # 소스당 최신 N건
SUMMARY_MAX_ITEMS = 10        # LLM 요약 총 상한(속도 관리)


# ─── 소스 정의 (SSOT — 필요 시 이 리스트만 수정) ──────────


@dataclass
class Source:
    name: str
    kind: str          # "rss" | "scrape"
    url: str
    lang: str          # "ko" | "en"
    scrape_selector: Optional[str] = None  # scrape 전용 CSS 셀렉터(기사 링크)


SOURCES: list[Source] = [
    Source("Anthropic", "rss", "https://www.anthropic.com/rss.xml", "en"),
    Source("DeepMind", "rss", "https://deepmind.google/blog/rss.xml", "en"),
    Source("한국경제 IT", "rss", "https://www.hankyung.com/feed/it", "ko"),
    # 스크래핑(best-effort) — 첫 실행 후 셀렉터/URL 보정 필요할 수 있음
    Source("NAVER IT/과학", "scrape", "https://news.naver.com/section/105", "ko",
           scrape_selector="a.sa_text_title"),
    Source("삼성전자 뉴스룸", "scrape", "https://news.samsung.com/kr/", "ko",
           scrape_selector="a.item__link, .post-item a"),
]


@dataclass
class Article:
    source: str
    title: str
    link: str
    raw_summary: str
    lang: str
    summary3: str = ""   # LLM 3줄 요약(채워지면 사용)


# ─── 외부 호출 (테스트는 monkeypatch) ────────────────


def _http_get(url: str, timeout: int = 15) -> Optional[str]:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (news_bot)"})
        with urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"HTTP 실패 {url}: {e}")
        return None


def _parse_rss(xml_text: str) -> list[dict]:
    """RSS/Atom 최소 파서 (stdlib). feedparser 있으면 우선 사용."""
    items: list[dict] = []
    try:
        import feedparser  # type: ignore
        fp = feedparser.parse(xml_text)
        for e in fp.entries:
            items.append({
                "title": (e.get("title") or "").strip(),
                "link": (e.get("link") or "").strip(),
                "summary": re.sub("<[^>]+>", "", e.get("summary", "") or "").strip(),
            })
        return items
    except ImportError:
        pass
    # stdlib fallback
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        log.warning(f"RSS 파싱 실패: {e}")
        return items
    # RSS 2.0: channel/item ; Atom: entry
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "summary": ""}
        for ch in item:
            ctag = ch.tag.split("}")[-1]
            if ctag == "title":
                d["title"] = (ch.text or "").strip()
            elif ctag == "link":
                d["link"] = (ch.get("href") or ch.text or "").strip()
            elif ctag in ("description", "summary", "content"):
                d["summary"] = re.sub("<[^>]+>", "", (ch.text or "")).strip()
        if d["title"]:
            items.append(d)
    return items


def _clean(text: str) -> str:
    """HTML 엔티티(&nbsp; 등) 해제 + 공백 정리. v3.47"""
    import html
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _fetch_rss_raw(source: Source, limit: int) -> list[Article]:
    xml = _http_get(source.url)
    if not xml:
        return []
    out = []
    for d in _parse_rss(xml)[:limit]:
        if d.get("link"):
            out.append(Article(source.name, _clean(d["title"]), d["link"],
                               _clean(d.get("summary", ""))[:400], source.lang))
    return out


def _fetch_scrape_raw(source: Source, limit: int) -> list[Article]:
    """best-effort HTML 스크래핑. bs4 사용. 셀렉터 변동 시 0건 → 로그."""
    html = _http_get(source.url)
    if not html:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("bs4 미설치 — 스크래핑 skip")
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[Article] = []
    for sel in (source.scrape_selector or "a").split(","):
        for a in soup.select(sel.strip()):
            title = a.get_text(strip=True)
            link = a.get("href", "")
            if not title or not link:
                continue
            if link.startswith("/"):
                from urllib.parse import urljoin
                link = urljoin(source.url, link)
            out.append(Article(source.name, title[:200], link, "", source.lang))
            if len(out) >= limit:
                break
        if out:
            break
    if not out:
        log.warning(f"스크래핑 0건 (셀렉터 확인 필요): {source.name}")
    return out


def _summarize_via_llm(articles: list[Article], topic: str = "IT/AI") -> None:
    """기사 묶음을 한 번의 LLM 호출로 3줄 요약. 각 article.summary3 채움.

    실패 시 raw_summary로 graceful fallback. 외부 호출 — 테스트는 monkeypatch.
    """
    if not articles:
        return
    numbered = "\n".join(
        f"{i+1}. [{a.source}] {a.title}\n   {a.raw_summary[:200]}"
        for i, a in enumerate(articles)
    )
    prompt = (
        f"다음 {topic} 기사들을 각각 한국어 3줄 이내로 핵심만 요약해라. "
        "영문 기사는 한국어로 번역 요약. 형식은 정확히 '번호. 요약' 한 줄씩.\n\n"
        + numbered
    )
    try:
        body = json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": 0.3, "num_ctx": 16384, "num_predict": 1200},
        }).encode("utf-8")
        req = Request(f"{OLLAMA_URL}/api/chat",
                      data=body, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=300) as r:
            text = json.loads(r.read().decode("utf-8"))["message"]["content"]
        _apply_summaries(articles, text)
    except Exception as e:
        log.warning(f"LLM 요약 실패({e}) — 원문 요약 사용")
        for a in articles:
            a.summary3 = a.raw_summary[:150]


def _apply_summaries(articles: list[Article], text: str) -> None:
    """LLM 출력('1. ...')을 번호 매칭해 각 article에 적용."""
    by_num: dict[int, str] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.*)", line)
        if m:
            cur = int(m.group(1))
            by_num[cur] = m.group(2).strip()
        elif cur and line.strip():
            by_num[cur] += " " + line.strip()
    for i, a in enumerate(articles):
        a.summary3 = by_num.get(i + 1, a.raw_summary[:150]).strip()


# ─── dedup (롤링, 디스크 영속) ───────────────────────


def load_seen() -> list[str]:
    try:
        if _SEEN_PATH.exists():
            return list(json.loads(_SEEN_PATH.read_text(encoding="utf-8")).get("links", []))
    except Exception:
        pass
    return []


def save_seen(links: list[str]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        trimmed = links[-_SEEN_CAP:]
        _SEEN_PATH.write_text(json.dumps({"links": trimmed}, ensure_ascii=False),
                              encoding="utf-8")
    except Exception as e:
        log.debug(f"news_seen 저장 실패: {e}")


def filter_unseen(articles: list[Article], seen: list[str]) -> tuple[list[Article], list[str]]:
    seen_set = set(seen)
    fresh = []
    for a in articles:
        if a.link not in seen_set:
            fresh.append(a)
            seen_set.add(a.link)
    return fresh, list(seen) + [a.link for a in fresh]


# ─── 수집·실행 ──────────────────────────────────────


def collect(sources: Optional[list[Source]] = None,
            per_source: int = DEFAULT_PER_SOURCE) -> list[Article]:
    srcs = sources if sources is not None else SOURCES
    out: list[Article] = []
    for s in srcs:
        try:
            items = (_fetch_rss_raw(s, per_source) if s.kind == "rss"
                     else _fetch_scrape_raw(s, per_source))
            out.extend(items)
        except Exception as e:
            log.warning(f"소스 수집 실패 {s.name}: {e}")
    return out


def format_digest(articles: list[Article], title: str = "IT/AI 뉴스 다이제스트") -> str:
    if not articles:
        return ""
    lines = [f"📰 *{title}* ({datetime.now():%m/%d %H:%M})\n"]
    by_src: dict[str, list[Article]] = {}
    for a in articles:
        by_src.setdefault(a.source, []).append(a)
    for src, arts in by_src.items():
        lines.append(f"*【{src}】*")
        for a in arts:
            body = a.summary3 or a.raw_summary[:120]
            lines.append(f"• [{a.title}]({a.link})")
            if body:
                lines.append(f"  {body}")
        lines.append("")
    return "\n".join(lines).strip()


def run(sources: Optional[list[Source]] = None,
        per_source: int = DEFAULT_PER_SOURCE,
        summarize: bool = True,
        use_dedup: bool = True) -> tuple[str, list[Article]]:
    """(메시지, 기사리스트) 반환. 텔레그램/라우터 entrypoint."""
    articles = collect(sources, per_source)
    if use_dedup:
        seen = load_seen()
        articles, updated = filter_unseen(articles, seen)
    else:
        updated = None
    if not articles:
        return "", []
    articles = articles[:SUMMARY_MAX_ITEMS]
    if summarize:
        _summarize_via_llm(articles)
    if use_dedup and updated is not None:
        save_seen(updated)
    return format_digest(articles), articles


# ─── v3.47 키워드 뉴스 (예: 하나은행) ─────────────────

KEYWORD_DEFAULT_LIMIT = 3      # 키워드 뉴스 1회 발송 건수
KEYWORD_FETCH_POOL = 30        # dedup 전 후보 수
KEYWORD_DUP_THRESHOLD = 0.4    # 제목 유사도(바이그램 겹침) 이상이면 같은 사건으로 봄


def _title_key(title: str, query: str) -> set[str]:
    """' - 언론사' 꼬리와 검색어를 뺀 제목의 글자 2-gram 집합."""
    t = re.sub(r"\s+-\s+[^-]+$", "", title).replace(query, "")
    t = re.sub(r"[^0-9A-Za-z가-힣]", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _is_near_dup(a: str, b: str, query: str) -> bool:
    ka, kb = _title_key(a, query), _title_key(b, query)
    if not ka or not kb:
        return False
    return len(ka & kb) / min(len(ka), len(kb)) >= KEYWORD_DUP_THRESHOLD


def pick_distinct(articles: list[Article], query: str, limit: int) -> tuple[list[Article], list[Article]]:
    """같은 사건을 다룬 기사(다른 언론사 중복)는 1건만 남겨 limit건 선택.
    반환: (선택, 중복으로 건너뛴 기사)."""
    picked: list[Article] = []
    skipped: list[Article] = []
    for a in articles:
        if len(picked) >= limit:
            break
        if any(_is_near_dup(a.title, p.title, query) for p in picked):
            skipped.append(a)
        else:
            picked.append(a)
    return picked, skipped


def keyword_source(query: str) -> Source:
    """구글 뉴스 RSS 검색(최근 1일, 한국어) 소스."""
    from urllib.parse import quote
    url = (f"https://news.google.com/rss/search?q={quote(query)}+when:1d"
           f"&hl=ko&gl=KR&ceid=KR:ko")
    return Source(f"구글뉴스 '{query}'", "rss", url, "ko")


def run_keyword(query: str = "하나은행",
                limit: int = KEYWORD_DEFAULT_LIMIT,
                summarize: bool = False,
                use_dedup: bool = True) -> tuple[str, list[Article]]:
    """키워드 기사 최신 limit건 → (메시지, 기사리스트). 이미 보낸 기사는 제외."""
    articles = _fetch_rss_raw(keyword_source(query), KEYWORD_FETCH_POOL)
    seen = load_seen() if use_dedup else []
    if use_dedup:
        articles, _ = filter_unseen(articles, seen)
    articles, skipped = pick_distinct(articles, query, limit)
    if not articles:
        return "", []
    # 구글 뉴스 RSS 설명은 '제목 + 언론사'뿐이라 본문이 없다.
    # 제목만으로 LLM 요약을 만들면 근거 없는 내용이 섞이므로 기본은 요약 생략, 설명도 비움.
    for a in articles:
        a.raw_summary = ""
    if summarize:
        _summarize_via_llm(articles, topic=f"'{query}' 관련")
    if use_dedup:
        # 보낸 기사 + 같은 사건 중복 기사까지 기록 (내일 같은 사건 재발송 방지)
        save_seen(list(seen) + [a.link for a in articles + skipped])
    return format_digest(articles, title=f"{query} 뉴스 {len(articles)}건"), articles


# ─── CLI (진단) ─────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="소스별 수신 건수만 출력")
    ap.add_argument("--no-summary", action="store_true")
    ap.add_argument("--no-dedup", action="store_true")
    args = ap.parse_args()
    if args.check:
        for s in SOURCES:
            items = (_fetch_rss_raw(s, 5) if s.kind == "rss" else _fetch_scrape_raw(s, 5))
            print(f"{s.name:16} {s.kind:7} {len(items)}건  {s.url}")
            for a in items[:2]:
                print(f"    - {a.title[:60]}")
    else:
        msg, arts = run(summarize=not args.no_summary, use_dedup=not args.no_dedup)
        print(msg or "(신규 기사 없음)")
