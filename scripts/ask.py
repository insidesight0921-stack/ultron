#!/usr/bin/env python3
"""
RAG 질의 — wiki 노트 검색 → Gemma 4 31B 답변.

흐름:
  질문 → 임베딩 → LanceDB Top-K 검색 → 컨텍스트 주입 → Gemma 4 → 답변 + 출처

사용:
    python ask.py "내 매매 청산 규칙이 뭐야?"
    python ask.py "콴텍봇 슬롯 비율은?"  --k 8
    python ask.py "모멘텀 크래시 감지 신호" --no-stream
    python ask.py --interactive       # 대화형 모드 (반복 질문)
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import lancedb
import numpy as np

HOME = Path.home()
PROJECT = HOME / "울트론" / "ai-agent"
DB_PATH = PROJECT / "data" / "lancedb"

OLLAMA_URL = "http://127.0.0.1:11434"
EMBED_MODEL = "bge-m3"        # 다국어 (한국어 포함). 인덱싱과 동일해야 함
LLM_MODEL = "gemma4:31b"
TABLE_NAME = "wiki_chunks"

DEFAULT_K = 6  # 검색할 청크 수 (다양성 위해 약간 증가)
KEEP_ALIVE = "30m"  # 모델 메모리 잔존 시간

SYSTEM_PROMPT = """당신은 현준의 개인 투자 비서입니다.

규칙:
1. 아래 [참조 노트] 섹션의 내용만 근거로 답변하세요. 노트에 없는 내용을 추측하거나 만들어내지 마세요.
2. 답변은 한국어로 간결하게. 핵심부터 답하고 부연 설명은 그 다음.
3. 노트 간 정보가 충돌하면 명시적으로 언급하세요.
4. 답변에 사용한 노트 제목을 [출처: 파일명] 형식으로 마지막에 표시하세요.
5. 노트에 명시되지 않은 질문이면 "wiki에 관련 노트가 없음"이라고 답하고, 어떤 노트를 작성하면 좋을지 제안하세요.
"""


def embed(text: str) -> list[float]:
    """L2 정규화 임베딩 (인덱싱과 동일해야 함)"""
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = Request(
        f"{OLLAMA_URL}/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    vec = np.array(data["embedding"], dtype=np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


def retrieve(query: str, k: int = DEFAULT_K) -> list[dict]:
    db = lancedb.connect(str(DB_PATH))
    if TABLE_NAME not in db.table_names():
        sys.exit("❌ 인덱스 없음. 먼저 python index_wiki.py 실행")
    table = db.open_table(TABLE_NAME)
    qvec = embed(query)
    return table.search(qvec).metric("cosine").limit(k).to_list()


def build_context(chunks: list[dict]) -> str:
    """검색된 청크들을 LLM 컨텍스트로 포맷"""
    if not chunks:
        return "(관련 노트 없음)"
    parts = []
    for i, c in enumerate(chunks, 1):
        parts.append(
            f"### 노트 {i}: {c['file']} :: {c['section']}\n"
            f"{c['content']}\n"
        )
    return "\n---\n".join(parts)


def chat_stream(query: str, context: str, model: str = LLM_MODEL) -> None:
    """Ollama /api/chat 스트리밍"""
    user_prompt = (
        f"[참조 노트]\n{context}\n\n"
        f"---\n\n"
        f"[질문]\n{query}"
    )
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
        "keep_alive": KEEP_ALIVE,
        "options": {
            "temperature": 0.3,  # 사실 기반 답변엔 낮게
            "num_ctx": 32768,
        },
    }).encode("utf-8")

    req = Request(
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
                print(msg, end="", flush=True)
            if chunk.get("done"):
                print()
                # 통계
                tot = chunk.get("total_duration", 0) / 1e9
                eval_count = chunk.get("eval_count", 0)
                eval_dur = chunk.get("eval_duration", 1) / 1e9
                tps = eval_count / eval_dur if eval_dur else 0
                print(f"\n  ⏱  {tot:.1f}s, {eval_count} tokens, {tps:.1f} tok/s", file=sys.stderr)


def chat_complete(query: str, context: str, model: str = LLM_MODEL) -> str:
    """비스트리밍 (전체 답변 한 번에)"""
    user_prompt = f"[참조 노트]\n{context}\n\n---\n\n[질문]\n{query}"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0.3, "num_ctx": 32768},
    }).encode("utf-8")
    req = Request(
        f"{OLLAMA_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"]


def ask(query: str, k: int = DEFAULT_K, stream: bool = True, verbose: bool = False) -> None:
    if verbose:
        print(f"🔍 검색 중...", file=sys.stderr)
    chunks = retrieve(query, k=k)
    if not chunks:
        print("(검색 결과 없음 — 인덱스가 비어있을 수 있음)", file=sys.stderr)
        return

    if verbose:
        print(f"📚 Top-{len(chunks)} 노트 검색됨:", file=sys.stderr)
        for i, c in enumerate(chunks, 1):
            d = c.get("_distance", 0)
            print(f"  {i}. {c['file']} :: {c['section']} (거리 {d:.3f})", file=sys.stderr)
        print(file=sys.stderr)

    context = build_context(chunks)
    print(f"\n💬 {query}\n", file=sys.stderr)
    print("─" * 60)

    if stream:
        chat_stream(query, context)
    else:
        print(chat_complete(query, context))


def interactive() -> None:
    """대화형 모드 — 종료: /quit 또는 Ctrl+D"""
    print("RAG 대화 모드. 질문 입력 (종료: /quit)")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료")
            return
        if not q:
            continue
        if q in ("/quit", "/exit", "/q"):
            return
        try:
            ask(q, verbose=True)
        except Exception as e:
            print(f"❌ 오류: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", help="질문 (생략 시 --interactive 필요)")
    ap.add_argument("--k", type=int, default=DEFAULT_K, help=f"검색 청크 수 (기본 {DEFAULT_K})")
    ap.add_argument("--no-stream", action="store_true", help="스트리밍 끔 (전체 답변 후 출력)")
    ap.add_argument("--interactive", "-i", action="store_true", help="대화형 모드")
    ap.add_argument("--verbose", "-v", action="store_true", help="검색 결과 메타 정보 출력")
    args = ap.parse_args()

    if args.interactive:
        interactive()
        return

    if not args.query:
        ap.print_help()
        sys.exit(1)

    ask(args.query, k=args.k, stream=not args.no_stream, verbose=args.verbose)


if __name__ == "__main__":
    main()
