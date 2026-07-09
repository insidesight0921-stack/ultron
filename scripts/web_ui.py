#!/usr/bin/env python3
"""
RAG Web UI — Claude 스타일 채팅 인터페이스 + 마스터 라우팅 + 멀티턴 (v3.12).

브라우저에서 http://localhost:8080 (또는 Tailscale IP).

기능:
- 마스터 라우터 → 7개 도구 자동 분기
  (knowledge / schedule / finance / invest / inbox / coding / respond_directly)
- thread_id 기반 멀티턴 메모리 (in-memory deque maxlen=12)
- 스트리밍 응답 (NDJSON) — route → sources → delta → done
- 🚀 fast / 🔍 accurate 모드 자동 판단 + 도구 배지 표시
- 마크다운 렌더링 + 출처 접기/펼치기 + 다크모드 + 모바일 반응형
- ✏️ 메모 추가 모달 — raw/inbox/에 저장 → watch_raw 자동 정제
- 🧹 이 thread 대화 메모리 초기화 버튼

사용:
    python web_ui.py                    # 기본: localhost:8080
    python web_ui.py --port 9000
    python web_ui.py --host 0.0.0.0     # Tailscale로 모바일 접속 시
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from pathlib import Path
from urllib.request import Request as URLRequest, urlopen

import lancedb
import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

HOME = Path.home()
PROJECT = HOME / "울트론" / "ai-agent"
DB_PATH = PROJECT / "data" / "lancedb"

# 공용 모듈 (텔레그램 봇과 동일한 라우팅 스택)
sys.path.insert(0, str(PROJECT / "scripts"))
from inbox import save_to_inbox, RAW_INBOX, VAULT  # noqa: E402
from memory import ChatMemory  # noqa: E402
from router import route as router_route, MASTER_MODEL  # noqa: E402
from knowledge_bot import (  # noqa: E402
    MODE_PARAMS as KB_MODE_PARAMS,
    SYSTEM_PROMPT as KB_SYSTEM_PROMPT,
)
from schedule_bot import run as schedule_run  # noqa: E402
from finance_bot import run as finance_run  # noqa: E402
from invest_bot import run as invest_run  # noqa: E402
from kium_bot import run as kium_run  # noqa: E402
from inbox_bot import run as inbox_bot_run  # noqa: E402
from coding_bot import run as coding_run  # noqa: E402
from ask import retrieve as rag_retrieve, build_context as rag_build_context  # noqa: E402

OLLAMA_URL = "http://127.0.0.1:11434"
EMBED_MODEL = "bge-m3"
LLM_MODEL = "gemma4:31b"
TABLE_NAME = "wiki_chunks"
KEEP_ALIVE = "30m"
DEFAULT_K = 6

# 멀티턴 메모리 (thread_id 단위, in-memory, 봇 재시작 시 휘발)
WEB_THREAD_DEFAULT = "web-default"
ROUTER_HISTORY_TURNS = 6
KNOWLEDGE_HISTORY_TURNS = 6
memory = ChatMemory(max_turns=12)

log = logging.getLogger("web_ui")


SYSTEM_PROMPT = """당신은 현준의 개인 투자 비서입니다.

규칙:
1. 아래 [참조 노트] 섹션의 내용만 근거로 답변하세요. 노트에 없는 내용을 추측하거나 만들어내지 마세요.
2. 답변은 한국어로 간결하게. 핵심부터 답하고 부연 설명은 그 다음.
3. 노트 간 정보가 충돌하면 명시적으로 언급하세요.
4. 답변 본문에는 [출처: ...] 표기를 넣지 마세요. 출처는 시스템이 자동으로 표시합니다.
5. 노트에 명시되지 않은 질문이면 "wiki에 관련 노트가 없음"이라고 답하고, 어떤 노트를 작성하면 좋을지 제안하세요.
"""


# ─── RAG 함수 ────────────────────────────────────────

def embed(text: str) -> list[float]:
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = URLRequest(
        f"{OLLAMA_URL}/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    vec = np.array(data["embedding"], dtype=np.float32)
    norm = np.linalg.norm(vec)
    return (vec / norm).tolist() if norm else vec.tolist()


def retrieve(query: str, k: int = DEFAULT_K) -> list[dict]:
    db = lancedb.connect(str(DB_PATH))
    if TABLE_NAME not in db.table_names():
        return []
    table = db.open_table(TABLE_NAME)
    qvec = embed(query)
    return table.search(qvec).metric("cosine").limit(k).to_list()


def build_context(chunks: list[dict]) -> str:
    if not chunks:
        return "(관련 노트 없음)"
    parts = [
        f"### 노트 {i}: {c['file']} :: {c['section']}\n{c['content']}\n"
        for i, c in enumerate(chunks, 1)
    ]
    return "\n---\n".join(parts)


# ─── FastAPI ─────────────────────────────────────────

app = FastAPI(title="현준의 RAG 비서")


@app.get("/api/health")
async def health():
    db = lancedb.connect(str(DB_PATH))
    n = (
        db.open_table(TABLE_NAME).count_rows()
        if TABLE_NAME in db.table_names()
        else 0
    )
    try:
        with urlopen(f"{OLLAMA_URL}/api/tags", timeout=2) as r:
            tags = json.loads(r.read().decode("utf-8"))
        models = [m["name"] for m in tags.get("models", [])]
        ollama_ok = any(LLM_MODEL.split(":")[0] in m for m in models)
    except Exception:
        ollama_ok = False
    return {"ok": True, "model": LLM_MODEL, "chunks": n, "ollama_ok": ollama_ok}


@app.post("/api/note")
async def add_note(request: Request):
    """raw/inbox/에 메모 저장. watch_raw가 자동 정제 → wiki/."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    content = (body.get("content") or "").strip()
    title = (body.get("title") or "").strip()

    if not content:
        return JSONResponse({"error": "empty content"}, status_code=400)
    if len(content) > 50000:
        return JSONResponse({"error": "content too large (50000 chars limit)"}, status_code=413)

    prefix = title or "메모"
    full_content = f"# {title}\n\n{content}" if title else content

    try:
        target = save_to_inbox(full_content, prefix, "web_ui modal")
    except Exception as e:
        return JSONResponse({"error": f"save failed: {e}"}, status_code=500)

    return {
        "ok": True,
        "path": str(target.relative_to(VAULT)),
        "size": len(content),
    }


# ─── 마스터 라우팅 + 멀티턴 (v3.12) ─────────────────


def _stream_knowledge_bot(query: str, history: list[dict] | None, mode: str):
    """knowledge_bot 로직을 스트리밍으로 직접 호출.

    knowledge_bot.run()은 비스트리밍이지만 web_ui는 토큰 스트리밍이 UX에 핵심이라
    동일 로직(retrieve → build_context → 31B chat)을 stream=True로 재구현.
    """
    mode = (mode or "accurate").strip().lower()
    if mode not in KB_MODE_PARAMS:
        mode = "accurate"
    params = KB_MODE_PARAMS[mode]
    k = params["k"]

    try:
        chunks = rag_retrieve(query, k=k)
    except Exception as e:
        yield {"type": "error", "message": f"검색 실패: {e}"}
        return

    if not chunks:
        yield {
            "type": "answer",
            "text": (
                "wiki에 관련 노트가 없습니다.\n"
                "텔레그램 /note 또는 ✏️ 메모 모달로 raw/inbox/에 추가하면 자동 정제 후 인덱싱됩니다."
            ),
        }
        yield {"type": "done"}
        return

    yield {
        "type": "sources",
        "chunks": [
            {
                "file": c["file"],
                "section": c.get("section", ""),
                "distance": float(c.get("_distance", 0)),
                "preview": c["content"][:200],
            }
            for c in chunks
        ],
    }

    context = rag_build_context(chunks)
    user_prompt = f"[참조 노트]\n{context}\n\n---\n\n[질문]\n{query}"
    messages: list[dict] = [{"role": "system", "content": KB_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    body = json.dumps(
        {
            "model": LLM_MODEL,
            "messages": messages,
            "stream": True,
            "keep_alive": KEEP_ALIVE,
            "options": {
                "temperature": params["temperature"],
                "num_ctx": params["num_ctx"],
                "num_predict": params["num_predict"],
            },
        }
    ).encode("utf-8")

    try:
        req = URLRequest(
            f"{OLLAMA_URL}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=300) as r:
            for line in r:
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                msg = chunk.get("message", {}).get("content", "")
                if msg:
                    yield {"type": "delta", "text": msg}
                if chunk.get("done"):
                    ec = chunk.get("eval_count", 0)
                    ed = chunk.get("eval_duration", 1) / 1e9
                    yield {
                        "type": "done",
                        "duration": chunk.get("total_duration", 0) / 1e9,
                        "tokens": ec,
                        "tps": (ec / ed) if ed else 0,
                    }
    except Exception as e:
        yield {"type": "error", "message": f"LLM 오류: {e}"}


def process_chat(thread_id: str, query: str):
    """라우팅 + 도구 호출 + 메모리 갱신을 한 번에 수행하는 generator.

    yield 되는 이벤트 종류:
      route   라우터가 선택한 tool/mode/args
      sources knowledge_bot RAG 청크 (출처 표시용)
      delta   스트리밍 텍스트 토큰 (knowledge_bot 전용)
      answer  비스트리밍 도구의 최종 답변 (schedule/finance/invest/inbox/coding/respond)
      done    완료 (선택적으로 tokens/duration/tps 포함)
      error   처리 실패
    """
    thread_id = (thread_id or WEB_THREAD_DEFAULT).strip() or WEB_THREAD_DEFAULT

    # 1. 라우터
    history = memory.history(thread_id, n=ROUTER_HISTORY_TURNS)
    try:
        action = router_route(query, history)
    except Exception as e:
        yield {"type": "error", "message": f"라우팅 실패: {e}"}
        return

    tool = action.get("tool", "knowledge_bot")
    args = action.get("args", {}) or {}
    mode = (action.get("mode") or "accurate").strip().lower()

    yield {"type": "route", "tool": tool, "mode": mode, "args": args}

    answer_text = ""

    # 2. 도구 분기 (텔레그램 핸들러와 동일한 로직)
    try:
        if tool == "respond_directly":
            answer_text = (args.get("answer") or "").strip() or "(빈 응답)"
            yield {"type": "answer", "text": answer_text}
            yield {"type": "done"}

        elif tool == "knowledge_bot":
            rewritten = (args.get("query") or query).strip() or query
            kb_history = memory.history(thread_id, n=KNOWLEDGE_HISTORY_TURNS)
            for evt in _stream_knowledge_bot(rewritten, kb_history, mode):
                if evt.get("type") == "delta":
                    answer_text += evt.get("text", "")
                elif evt.get("type") == "answer":
                    answer_text = evt.get("text", "")
                yield evt

        elif tool == "schedule_bot":
            ans, _ = schedule_run(**{**args, "chat_id": thread_id})
            answer_text = ans
            yield {"type": "answer", "text": ans}
            yield {"type": "done"}

        elif tool == "finance_bot":
            ans, _ = finance_run(**args)
            answer_text = ans
            yield {"type": "answer", "text": ans}
            yield {"type": "done"}

        elif tool == "invest_bot":
            ans, _ = invest_run(mode=mode, **args)
            answer_text = ans
            yield {"type": "answer", "text": ans}
            yield {"type": "done"}

        elif tool == "kium_bot":
            ans, _ = kium_run(**args)
            answer_text = ans
            yield {"type": "answer", "text": ans}
            yield {"type": "done"}

        elif tool == "inbox_bot":
            ans, _ = inbox_bot_run(**{**args, "chat_id": thread_id, "mode": mode})
            answer_text = ans
            yield {"type": "answer", "text": ans}
            yield {"type": "done"}

        elif tool == "coding_bot":
            ans, _ = coding_run(mode=mode, **args)
            answer_text = ans
            yield {"type": "answer", "text": ans}
            yield {"type": "done"}

        else:
            yield {"type": "error", "message": f"미지원 도구: {tool}"}
            return

    except Exception as e:
        log.exception(f"{tool} 실행 실패")
        yield {"type": "error", "message": f"{tool} 오류: {e}"}
        return

    # 3. 메모리 갱신 (사용자 + 봇 답변)
    if answer_text and answer_text != "(빈 응답)":
        memory.add(thread_id, "user", query)
        memory.add(thread_id, "assistant", answer_text)


@app.post("/api/chat")
async def chat(request: Request):
    """라우팅 + 멀티턴 응답. NDJSON 스트림."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    query = (body.get("query") or "").strip()
    thread_id = (body.get("thread_id") or WEB_THREAD_DEFAULT).strip() or WEB_THREAD_DEFAULT
    if not query:
        return JSONResponse({"error": "empty query"}, status_code=400)

    def gen():
        for evt in process_chat(thread_id, query):
            yield json.dumps(evt, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/memory")
async def get_memory(thread_id: str = WEB_THREAD_DEFAULT):
    h = memory.history(thread_id)
    return {"thread_id": thread_id, "history": h, "turns": len(h)}


@app.post("/api/memory/clear")
async def clear_memory(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    thread_id = (body.get("thread_id") or WEB_THREAD_DEFAULT).strip() or WEB_THREAD_DEFAULT
    cleared = memory.clear(thread_id)
    return {"ok": True, "thread_id": thread_id, "cleared_turns": cleared}


@app.post("/api/ask")
async def ask(request: Request):
    body = await request.json()
    query = body.get("query", "").strip()
    k = int(body.get("k", DEFAULT_K))
    if not query:
        return JSONResponse({"error": "empty query"}, status_code=400)

    def generate():
        # 1. 검색
        try:
            chunks = retrieve(query, k=k)
        except Exception as e:
            yield json.dumps({"type": "error", "message": f"검색 실패: {e}"}, ensure_ascii=False) + "\n"
            return

        yield json.dumps(
            {
                "type": "sources",
                "chunks": [
                    {
                        "file": c["file"],
                        "section": c["section"],
                        "distance": float(c.get("_distance", 0)),
                        "preview": c["content"][:200],
                    }
                    for c in chunks
                ],
            },
            ensure_ascii=False,
        ) + "\n"

        # 2. LLM 스트리밍
        context = build_context(chunks)
        user_prompt = f"[참조 노트]\n{context}\n\n---\n\n[질문]\n{query}"
        llm_body = json.dumps(
            {
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": True,
                "keep_alive": KEEP_ALIVE,
                "options": {"temperature": 0.3, "num_ctx": 32768},
            }
        ).encode("utf-8")

        try:
            req = URLRequest(
                f"{OLLAMA_URL}/api/chat",
                data=llm_body,
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=300) as r:
                for line in r:
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    msg = chunk.get("message", {}).get("content", "")
                    if msg:
                        yield json.dumps(
                            {"type": "delta", "text": msg}, ensure_ascii=False
                        ) + "\n"
                    if chunk.get("done"):
                        ec = chunk.get("eval_count", 0)
                        ed = chunk.get("eval_duration", 1) / 1e9
                        yield json.dumps(
                            {
                                "type": "done",
                                "duration": chunk.get("total_duration", 0) / 1e9,
                                "tokens": ec,
                                "tps": (ec / ed) if ed else 0,
                            },
                            ensure_ascii=False,
                        ) + "\n"
        except Exception as e:
            yield json.dumps(
                {"type": "error", "message": f"LLM 오류: {e}"}, ensure_ascii=False
            ) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ─── 단일 HTML 페이지 ────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>현준의 RAG 비서</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Source+Serif+Pro:wght@600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root {
    --bg: #faf9f7;
    --surface: #ffffff;
    --surface-2: #f4f1ec;
    --border: #e6e2dc;
    --text: #1f1f1f;
    --text-muted: #71717a;
    --user-bubble: #f0ebe4;
    --accent: #c96442;
    --accent-hover: #b55a3a;
    --code-bg: #f4f1ec;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
    --max-width: 760px;
    --radius: 16px;
    --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    --font-serif: 'Source Serif Pro', Georgia, serif;
    --font-mono: 'SF Mono', SFMono-Regular, Menlo, Monaco, monospace;
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1a1a1a;
      --surface: #242424;
      --surface-2: #2c2c2c;
      --border: #3a3a3a;
      --text: #ececec;
      --text-muted: #a3a3a3;
      --user-bubble: #2c2a27;
      --accent: #d97442;
      --accent-hover: #e88452;
      --code-bg: #2c2c2c;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
    }
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  html, body {
    height: 100%;
    overflow: hidden;
  }

  body {
    font-family: var(--font-sans);
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    font-size: 15px;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }

  #app {
    display: flex;
    flex-direction: column;
    height: 100vh;
    height: 100dvh;
    max-width: var(--max-width);
    margin: 0 auto;
    background: var(--bg);
  }

  /* Header */
  header {
    padding: 14px 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
    flex-shrink: 0;
  }

  .logo {
    font-weight: 600;
    font-size: 15px;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .logo-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent);
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .status {
    font-size: 12px;
    color: var(--text-muted);
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #cbd5e1;
  }

  .status.online .status-dot {
    background: #22c55e;
    box-shadow: 0 0 4px rgba(34, 197, 94, 0.4);
  }

  .status.offline .status-dot {
    background: #dc2626;
  }

  .header-btn {
    background: none;
    border: 1px solid var(--border);
    color: var(--text-muted);
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 12px;
    font-family: inherit;
    cursor: pointer;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .header-btn:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  /* Messages */
  main {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 24px 24px 8px;
    scroll-behavior: smooth;
    -webkit-overflow-scrolling: touch;
  }

  .welcome {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 70vh;
    text-align: center;
    padding: 24px;
  }

  .welcome h1 {
    font-family: var(--font-serif);
    font-size: 32px;
    font-weight: 600;
    margin-bottom: 8px;
    letter-spacing: -0.02em;
  }

  .welcome .subtitle {
    color: var(--text-muted);
    margin-bottom: 36px;
    max-width: 480px;
    font-size: 15px;
  }

  .examples {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
    width: 100%;
    max-width: 540px;
  }

  .example-btn {
    padding: 14px 18px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    cursor: pointer;
    font-size: 14px;
    font-family: inherit;
    text-align: left;
    color: var(--text);
    transition: all 0.15s ease;
    box-shadow: var(--shadow);
  }

  .example-btn:hover {
    border-color: var(--accent);
    transform: translateY(-1px);
  }

  .example-btn:active {
    transform: translateY(0);
  }

  /* Message bubbles */
  .message {
    margin-bottom: 24px;
    display: flex;
    flex-direction: column;
    max-width: 92%;
  }
  .message.user { align-self: flex-end; align-items: flex-end; }
  .message.assistant {
    align-self: flex-start;
    align-items: flex-start;
    max-width: 100%;
    width: 100%;
  }
  .message-content {
    padding: 12px 16px;
    border-radius: var(--radius);
    word-wrap: break-word;
    overflow-wrap: anywhere;
  }
  .message.user .message-content {
    background: var(--user-bubble);
    border-bottom-right-radius: 4px;
  }
  .message.assistant .message-content {
    background: transparent;
    padding: 0;
  }

  /* Markdown styles */
  .message-content p { margin-bottom: 12px; }
  .message-content p:last-child { margin-bottom: 0; }
  .message-content ul, .message-content ol { margin: 8px 0 12px; padding-left: 24px; }
  .message-content li { margin-bottom: 4px; }
  .message-content li > p { margin-bottom: 4px; }
  .message-content h1, .message-content h2, .message-content h3 {
    margin: 16px 0 8px; font-weight: 600; line-height: 1.3;
  }
  .message-content h1 { font-size: 18px; }
  .message-content h2 { font-size: 16px; }
  .message-content h3 { font-size: 15px; }
  .message-content code {
    background: var(--code-bg); padding: 2px 6px;
    border-radius: 4px; font-size: 0.9em; font-family: var(--font-mono);
  }
  .message-content pre {
    background: var(--code-bg); padding: 12px 14px;
    border-radius: 10px; overflow-x: auto; margin: 12px 0;
    font-size: 13px; line-height: 1.5;
  }
  .message-content pre code { background: transparent; padding: 0; }
  .message-content strong { font-weight: 600; }
  .message-content em { font-style: italic; }
  .message-content blockquote {
    border-left: 3px solid var(--border);
    padding-left: 14px; margin: 12px 0; color: var(--text-muted);
  }
  .message-content table {
    border-collapse: collapse; margin: 12px 0; font-size: 13px; width: 100%;
  }
  .message-content th, .message-content td {
    border: 1px solid var(--border); padding: 6px 10px; text-align: left;
  }
  .message-content th { background: var(--surface-2); font-weight: 600; }
  .message-content a { color: var(--accent); text-decoration: none; }
  .message-content a:hover { text-decoration: underline; }

  /* Cursor */
  .cursor {
    display: inline-block; width: 8px; height: 14px;
    background: var(--text); animation: blink 1s ease-in-out infinite;
    margin-left: 2px; vertical-align: text-bottom; border-radius: 1px;
  }
  @keyframes blink { 0%,50% { opacity: 1; } 51%,100% { opacity: 0; } }

  /* Route badge */
  .route-badge {
    display: inline-flex; align-items: center; gap: 6px;
    margin: 0 0 8px 0; padding: 3px 10px;
    background: var(--surface-2); border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 11.5px; color: var(--text-muted);
    font-family: var(--font-mono);
  }
  .route-badge .mode {
    font-weight: 600; color: var(--accent);
  }

  /* Sources */
  .sources {
    margin-top: 14px; padding: 10px 14px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; font-size: 12px; color: var(--text-muted);
    box-shadow: var(--shadow);
  }
  .sources summary {
    cursor: pointer; font-weight: 500; user-select: none;
    list-style: none; display: flex; align-items: center; gap: 6px;
  }
  .sources summary::-webkit-details-marker { display: none; }
  .sources summary::before {
    content: '▶'; font-size: 10px;
    transition: transform 0.15s; color: var(--text-muted);
  }
  .sources details[open] summary::before { transform: rotate(90deg); }
  .sources details[open] summary { margin-bottom: 10px; }
  .source-item { padding: 8px 0; border-bottom: 1px solid var(--border); }
  .source-item:last-child { border-bottom: none; }
  .source-file { font-weight: 500; color: var(--text); font-size: 12.5px; }
  .source-section { color: var(--text-muted); margin-top: 2px; }
  .source-distance {
    font-family: var(--font-mono); font-size: 11px; color: var(--text-muted);
  }
  .stats-line {
    margin-top: 8px; font-size: 11px;
    color: var(--text-muted); font-family: var(--font-mono);
  }

  /* Input */
  .input-area {
    padding: 14px 24px 20px;
    background: var(--bg);
    flex-shrink: 0;
  }

  #chat-form {
    display: flex; align-items: flex-end; gap: 6px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 18px; padding: 6px 6px 6px 16px;
    transition: border-color 0.15s, box-shadow 0.15s;
    box-shadow: var(--shadow);
  }
  #chat-form:focus-within {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(201, 100, 66, 0.1);
  }

  #query {
    flex: 1; border: none; background: transparent;
    color: var(--text); font: inherit; font-size: 15px;
    resize: none; outline: none; padding: 9px 0;
    max-height: 200px; min-height: 24px; line-height: 1.5;
  }
  #query::placeholder { color: var(--text-muted); }

  .icon-btn {
    width: 34px; height: 34px; border: none;
    background: transparent; color: var(--text-muted);
    border-radius: 50%; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: all 0.15s; flex-shrink: 0;
  }
  .icon-btn:hover { background: var(--surface-2); color: var(--accent); }
  .icon-btn:active { transform: scale(0.95); }
  .icon-btn svg { width: 18px; height: 18px; }

  #send {
    width: 34px; height: 34px; border: none;
    background: var(--accent); color: white;
    border-radius: 50%; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.15s, transform 0.1s; flex-shrink: 0;
  }
  #send:hover:not(:disabled) { background: var(--accent-hover); }
  #send:active:not(:disabled) { transform: scale(0.95); }
  #send:disabled { background: var(--border); cursor: not-allowed; }
  #send svg { width: 16px; height: 16px; }

  .thinking {
    color: var(--text-muted); font-size: 14px; padding: 4px 0;
    display: flex; align-items: center; gap: 6px;
  }
  .thinking::after {
    content: ''; display: inline-block;
    width: 4px; height: 4px; border-radius: 50%;
    background: var(--text-muted);
    animation: pulse 1.4s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,100% { opacity: 0.3; transform: scale(1); }
    50% { opacity: 1; transform: scale(1.4); }
  }

  .error {
    color: #dc2626; background: rgba(220, 38, 38, 0.08);
    padding: 10px 14px; border-radius: 10px; margin: 8px 0;
    font-size: 13px; border: 1px solid rgba(220, 38, 38, 0.2);
  }

  .footer-hint {
    text-align: center; font-size: 11px;
    color: var(--text-muted); margin-top: 8px;
  }
  kbd {
    display: inline-block; padding: 1px 5px;
    border: 1px solid var(--border); border-radius: 4px;
    background: var(--surface); font-family: var(--font-mono); font-size: 10px;
  }

  /* Modal */
  .modal-backdrop {
    position: fixed; inset: 0;
    background: rgba(0, 0, 0, 0.4);
    display: none;
    align-items: flex-end; justify-content: center;
    z-index: 100;
    -webkit-backdrop-filter: blur(2px);
    backdrop-filter: blur(2px);
  }
  .modal-backdrop.open { display: flex; animation: fade-in 0.15s ease; }
  @keyframes fade-in { from { opacity: 0; } to { opacity: 1; } }

  .modal {
    background: var(--bg);
    width: 100%; max-width: 600px;
    max-height: 90vh;
    border-radius: 20px 20px 0 0;
    display: flex; flex-direction: column;
    box-shadow: 0 -2px 20px rgba(0,0,0,0.15);
    animation: slide-up 0.2s ease;
  }
  @keyframes slide-up { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }

  @media (min-width: 720px) {
    .modal-backdrop { align-items: center; }
    .modal { border-radius: 16px; max-height: 80vh; }
  }

  .modal-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 16px 20px; border-bottom: 1px solid var(--border);
  }
  .modal-title { font-weight: 600; font-size: 15px; }
  .modal-close {
    background: none; border: none; color: var(--text-muted);
    font-size: 24px; cursor: pointer; line-height: 1;
    padding: 0 4px;
  }
  .modal-close:hover { color: var(--text); }

  .modal-body {
    padding: 16px 20px;
    flex: 1; overflow-y: auto;
    display: flex; flex-direction: column; gap: 10px;
  }
  .modal-body label {
    font-size: 12px; color: var(--text-muted);
    font-weight: 500;
  }
  .modal-body input, .modal-body textarea {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px;
    color: var(--text); font: inherit; font-size: 14px;
    outline: none; transition: border-color 0.15s;
  }
  .modal-body input:focus, .modal-body textarea:focus {
    border-color: var(--accent);
  }
  .modal-body textarea {
    min-height: 200px; resize: vertical; line-height: 1.5;
    font-family: var(--font-mono); font-size: 13.5px;
  }
  .modal-hint {
    font-size: 11px; color: var(--text-muted); margin-top: -4px;
  }

  .modal-footer {
    display: flex; justify-content: flex-end; gap: 8px;
    padding: 12px 20px;
    border-top: 1px solid var(--border);
  }
  .btn {
    padding: 8px 16px; border-radius: 10px;
    border: 1px solid var(--border); background: var(--surface);
    color: var(--text); cursor: pointer; font: inherit; font-size: 13px;
    transition: all 0.15s;
  }
  .btn:hover { border-color: var(--text-muted); }
  .btn-primary {
    background: var(--accent); color: white; border-color: var(--accent);
  }
  .btn-primary:hover:not(:disabled) {
    background: var(--accent-hover); border-color: var(--accent-hover);
  }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Toast */
  .toast {
    position: fixed; bottom: 24px; left: 50%;
    transform: translateX(-50%) translateY(100px);
    background: var(--text); color: var(--bg);
    padding: 10px 18px; border-radius: 999px;
    font-size: 13px; box-shadow: 0 4px 12px rgba(0,0,0,0.2);
    z-index: 200; opacity: 0;
    transition: opacity 0.2s, transform 0.2s;
    pointer-events: none;
  }
  .toast.show {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
  }
  .toast.error { background: #dc2626; color: white; }

  /* Mobile */
  @media (max-width: 640px) {
    header { padding: 12px 14px; }
    main { padding: 16px 14px 8px; }
    .input-area { padding: 10px 14px 14px; }
    .examples { grid-template-columns: 1fr; }
    .message { max-width: 100%; }
    .welcome h1 { font-size: 26px; }
    .welcome { min-height: 60vh; }
  }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }
</style>
</head>
<body>
<div id="app">
  <header>
    <div class="logo">
      <span class="logo-dot"></span>
      현준의 RAG 비서
    </div>
    <div class="header-right">
      <button class="header-btn" id="memo-btn" title="메모 추가 (raw/inbox/)">
        ✏️ 메모
      </button>
      <button class="header-btn" id="clear-btn" title="이 대화 메모리 초기화">
        🧹 초기화
      </button>
      <div class="status" id="status">
        <span class="status-dot"></span>
        <span id="status-text">연결 중...</span>
      </div>
    </div>
  </header>

  <main id="messages">
    <div class="welcome" id="welcome">
      <h1>안녕, 현준</h1>
      <p class="subtitle">vault에 적은 투자 원칙과 노트를 바탕으로 답해줄게.</p>
      <div class="examples">
        <button class="example-btn">내 매매 청산 규칙이 뭐야?</button>
        <button class="example-btn">콴텍봇 슬롯 비율은?</button>
        <button class="example-btn">모멘텀 크래시 감지 신호?</button>
        <button class="example-btn">관심 섹터는 뭐가 있지?</button>
      </div>
    </div>
  </main>

  <div class="input-area">
    <form id="chat-form" autocomplete="off">
      <textarea id="query" placeholder="질문 입력..." rows="1" autocomplete="off"></textarea>
      <button type="submit" id="send" aria-label="전송">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <line x1="12" y1="19" x2="12" y2="5"></line>
          <polyline points="5 12 12 5 19 12"></polyline>
        </svg>
      </button>
    </form>
    <div class="footer-hint" id="footer-hint"><kbd>Enter</kbd> 전송 · <kbd>Shift + Enter</kbd> 줄바꿈 · 🚀 fast / 🔍 accurate 자동 판단</div>
  </div>
</div>

<!-- Memo Modal -->
<div class="modal-backdrop" id="memo-modal">
  <div class="modal" role="dialog" aria-labelledby="modal-title">
    <div class="modal-header">
      <div class="modal-title" id="modal-title">✏️ 메모 추가 → raw/inbox/</div>
      <button class="modal-close" id="memo-close" aria-label="닫기">×</button>
    </div>
    <div class="modal-body">
      <label for="memo-title">제목 (선택)</label>
      <input type="text" id="memo-title" placeholder="예) 삼성전자 26.4Q 실적 메모" maxlength="80">
      <label for="memo-content">내용</label>
      <textarea id="memo-content" placeholder="메모 내용 입력... (마크다운 가능)&#10;&#10;5분 idle 또는 30분 batch에 자동 정제되어 wiki/에 인덱싱됩니다."></textarea>
      <div class="modal-hint">
        <kbd>Cmd/Ctrl + Enter</kbd> 저장 · <kbd>Esc</kbd> 닫기
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" id="memo-cancel">취소</button>
      <button class="btn btn-primary" id="memo-save">저장</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
  marked.setOptions({ breaks: true, gfm: true });

  const messagesEl = document.getElementById('messages');
  const formEl = document.getElementById('chat-form');
  const queryEl = document.getElementById('query');
  const sendBtn = document.getElementById('send');
  const statusEl = document.getElementById('status');
  const statusText = document.getElementById('status-text');
  const welcomeEl = document.getElementById('welcome');

  // Memo modal
  const memoBtn = document.getElementById('memo-btn');
  const memoModal = document.getElementById('memo-modal');
  const memoClose = document.getElementById('memo-close');
  const memoCancel = document.getElementById('memo-cancel');
  const memoSave = document.getElementById('memo-save');
  const memoTitleEl = document.getElementById('memo-title');
  const memoContentEl = document.getElementById('memo-content');

  // Toast
  const toastEl = document.getElementById('toast');
  let toastTimer = null;
  function showToast(msg, isError = false) {
    toastEl.textContent = msg;
    toastEl.classList.remove('error');
    if (isError) toastEl.classList.add('error');
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2500);
  }

  let isStreaming = false;
  let isSavingMemo = false;

  async function checkHealth() {
    try {
      const r = await fetch('/api/health');
      const d = await r.json();
      if (d.ok && d.ollama_ok) {
        statusEl.classList.remove('offline');
        statusEl.classList.add('online');
        statusText.textContent = `${d.chunks} chunks · ${d.model}`;
      } else {
        statusEl.classList.add('offline');
        statusText.textContent = 'Ollama 연결 안 됨';
      }
    } catch (e) {
      statusEl.classList.add('offline');
      statusText.textContent = '서버 응답 없음';
    }
  }
  checkHealth();
  setInterval(checkHealth, 30000);

  document.querySelectorAll('.example-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      queryEl.value = btn.textContent;
      autoResize();
      submit();
    });
  });

  function autoResize() {
    queryEl.style.height = 'auto';
    queryEl.style.height = Math.min(queryEl.scrollHeight, 200) + 'px';
  }
  queryEl.addEventListener('input', autoResize);

  queryEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });

  formEl.addEventListener('submit', (e) => { e.preventDefault(); submit(); });

  function escapeHtml(s) {
    const div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
  }

  function addMessage(role, text) {
    const msg = document.createElement('div');
    msg.className = `message ${role}`;
    const content = document.createElement('div');
    content.className = 'message-content';
    if (role === 'user') content.textContent = text;
    else content.innerHTML = marked.parse(text || '');
    msg.appendChild(content);
    messagesEl.appendChild(msg);
    scrollToBottom();
    return msg;
  }

  function scrollToBottom() {
    requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; });
  }

  // ─── Memo modal ─────────────────────────────────

  function openMemoModal() {
    memoModal.classList.add('open');
    setTimeout(() => memoContentEl.focus(), 50);
  }
  function closeMemoModal() {
    memoModal.classList.remove('open');
  }

  memoBtn.addEventListener('click', openMemoModal);
  memoClose.addEventListener('click', closeMemoModal);
  memoCancel.addEventListener('click', closeMemoModal);
  memoModal.addEventListener('click', (e) => {
    if (e.target === memoModal) closeMemoModal();
  });

  document.addEventListener('keydown', (e) => {
    if (memoModal.classList.contains('open')) {
      if (e.key === 'Escape') {
        e.preventDefault();
        closeMemoModal();
      } else if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        saveMemo();
      }
    }
  });

  async function saveMemo() {
    if (isSavingMemo) return;
    const title = memoTitleEl.value.trim();
    const content = memoContentEl.value.trim();
    if (!content) {
      showToast('내용 비어있음', true);
      memoContentEl.focus();
      return;
    }
    isSavingMemo = true;
    memoSave.disabled = true;
    memoSave.textContent = '저장 중...';

    try {
      const r = await fetch('/api/note', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, content }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      showToast(`✅ 저장됨 — ${d.path}`);
      memoTitleEl.value = '';
      memoContentEl.value = '';
      closeMemoModal();
    } catch (e) {
      showToast(`저장 실패: ${e.message}`, true);
    } finally {
      isSavingMemo = false;
      memoSave.disabled = false;
      memoSave.textContent = '저장';
    }
  }

  memoSave.addEventListener('click', saveMemo);

  // ─── 라우팅 배지 / 메모리 초기화 / Chat submit ───

  const TOOL_LABELS = {
    knowledge_bot: '📚 지식봇',
    schedule_bot:  '📅 일정봇',
    finance_bot:   '📊 금융봇',
    invest_bot:    '📈 투자봇',
    inbox_bot:     '📝 메모',
    coding_bot:    '💻 코딩봇',
    kium_bot: '📊 키움봇',
    respond_directly: '💬 즉답',
  };

  // thread_id — localStorage 가능하면 세션 분리, 안 되면 'web-default'
  let THREAD_ID = 'web-default';
  try {
    const saved = localStorage.getItem('rag_thread_id');
    if (saved) THREAD_ID = saved;
    else {
      THREAD_ID = 'web-' + Math.random().toString(36).slice(2, 10);
      localStorage.setItem('rag_thread_id', THREAD_ID);
    }
  } catch (e) { /* localStorage 차단 시 그대로 default */ }

  // 메모리 초기화 버튼
  const clearBtn = document.getElementById('clear-btn');
  clearBtn.addEventListener('click', async () => {
    if (!confirm('이 대화 메모리를 초기화할까?')) return;
    try {
      const r = await fetch('/api/memory/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ thread_id: THREAD_ID }),
      });
      const d = await r.json();
      showToast(`🧹 초기화 완료 (${d.cleared_turns}턴 삭제)`);
    } catch (e) {
      showToast('초기화 실패: ' + e.message, true);
    }
  });

  function makeRouteBadge(tool, mode) {
    const badge = document.createElement('div');
    badge.className = 'route-badge';
    const modeEmoji = mode === 'fast' ? '🚀 fast' : '🔍 accurate';
    const toolLabel = TOOL_LABELS[tool] || tool;
    badge.innerHTML = `<span class="mode">${modeEmoji}</span> · ${toolLabel}`;
    return badge;
  }

  async function submit() {
    if (isStreaming) return;
    const query = queryEl.value.trim();
    if (!query) return;

    if (welcomeEl && welcomeEl.parentElement) welcomeEl.remove();
    addMessage('user', query);
    queryEl.value = '';
    autoResize();

    const assistantEl = addMessage('assistant', '');
    const contentEl = assistantEl.querySelector('.message-content');
    const thinking = document.createElement('div');
    thinking.className = 'thinking';
    thinking.textContent = '🧭 라우팅 중';
    contentEl.appendChild(thinking);

    isStreaming = true;
    sendBtn.disabled = true;

    let fullText = '';
    let sources = null;
    let stats = null;
    let routeInfo = null;

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, thread_id: THREAD_ID })
      });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.trim()) continue;
          let msg;
          try { msg = JSON.parse(line); } catch (err) { continue; }

          if (msg.type === 'route') {
            routeInfo = msg;
            thinking.textContent = (msg.tool === 'knowledge_bot' ? 'wiki 검색 중'
              : msg.tool === 'schedule_bot' ? '일정 처리 중'
              : msg.tool === 'finance_bot'  ? '지표 조회 중'
              : msg.tool === 'invest_bot'   ? '차트 분석 중'
              : msg.tool === 'inbox_bot'    ? '메모 저장 중'
              : msg.tool === 'coding_bot'   ? '코드 처리 중'
              : '응답 생성 중');
            const badge = makeRouteBadge(msg.tool, msg.mode);
            assistantEl.insertBefore(badge, contentEl);
          } else if (msg.type === 'sources') {
            sources = msg.chunks;
            thinking.remove();
            contentEl.innerHTML = '';
            const cursor = document.createElement('span');
            cursor.className = 'cursor';
            cursor.id = 'live-cursor';
            contentEl.appendChild(cursor);
          } else if (msg.type === 'delta') {
            if (thinking.parentElement) thinking.remove();
            fullText += msg.text;
            contentEl.innerHTML = marked.parse(fullText);
            const cursor = document.createElement('span');
            cursor.className = 'cursor';
            cursor.id = 'live-cursor';
            contentEl.appendChild(cursor);
            scrollToBottom();
          } else if (msg.type === 'answer') {
            if (thinking.parentElement) thinking.remove();
            fullText = msg.text;
            contentEl.innerHTML = marked.parse(fullText);
            scrollToBottom();
          } else if (msg.type === 'done') {
            stats = msg;
            const cursor = document.getElementById('live-cursor');
            if (cursor) cursor.remove();
            if (sources && sources.length > 0) {
              const details = document.createElement('details');
              details.className = 'sources';
              const summary = document.createElement('summary');
              summary.textContent = `📚 참조한 노트 ${sources.length}개`;
              details.appendChild(summary);
              for (const s of sources) {
                const item = document.createElement('div');
                item.className = 'source-item';
                item.innerHTML = `
                  <div class="source-file">${escapeHtml(s.file.replace(/^wiki\//, ''))}</div>
                  <div class="source-section">${escapeHtml(s.section)} <span class="source-distance">· 거리 ${s.distance.toFixed(3)}</span></div>
                `;
                details.appendChild(item);
              }
              if (stats && stats.tokens) {
                const statsEl = document.createElement('div');
                statsEl.className = 'stats-line';
                statsEl.textContent = `${stats.tokens} tokens · ${stats.duration.toFixed(1)}s · ${stats.tps.toFixed(1)} tok/s`;
                details.appendChild(statsEl);
              }
              assistantEl.appendChild(details);
            }
          } else if (msg.type === 'error') {
            const cursor = document.getElementById('live-cursor');
            if (cursor) cursor.remove();
            if (thinking.parentElement) thinking.remove();
            contentEl.innerHTML = `<div class="error">❌ ${escapeHtml(msg.message)}</div>`;
          }
        }
      }
    } catch (err) {
      const cursor = document.getElementById('live-cursor');
      if (cursor) cursor.remove();
      if (thinking.parentElement) thinking.remove();
      contentEl.innerHTML = `<div class="error">❌ 네트워크 오류: ${escapeHtml(err.message)}</div>`;
    } finally {
      isStreaming = false;
      sendBtn.disabled = false;
      queryEl.focus();
    }
  }

  queryEl.focus();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML_PAGE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="바인딩 호스트 (기본 127.0.0.1, Tailscale 모바일은 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8080, help="포트 (기본 8080)")
    args = ap.parse_args()

    print(f"\n🌐 RAG Web UI 시작")
    print(f"   브라우저에서: http://{args.host if args.host != '0.0.0.0' else 'localhost'}:{args.port}")
    print(f"   inbox: {RAW_INBOX}")
    print(f"   종료: Ctrl+C\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
