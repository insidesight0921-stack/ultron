#!/usr/bin/env python3
"""
지식봇 (knowledge_bot) — wiki RAG 래퍼 + 멀티턴 지원.

흐름:
  query (라우터가 명료화한 query) + history
    → ask.py.retrieve(query, k=6) → wiki 청크 Top-K
    → ask.py.build_context() 로 컨텍스트 구성
    → Gemma 4 31B Dense에 [system_prompt, *history, user_query] 주입
    → 답변 + 청크 메타 반환

라우터(router.py)와 함께 4단계 라우팅 아키텍처의 첫 도구.
다음 세션에 추가될 도구들(schedule_bot, finance_bot 등)도 같은 (answer, sources) 인터페이스 따름.
"""
from __future__ import annotations
import json
import logging
from urllib.request import Request, urlopen

# ask.py의 RAG 함수 재사용
from ask import retrieve, build_context, LLM_MODEL

OLLAMA_URL = "http://127.0.0.1:11434"
KEEP_ALIVE = "30m"

log = logging.getLogger("knowledge_bot")


SYSTEM_PROMPT = """당신은 현준의 개인 투자 비서입니다.

규칙:
1. 아래 [참조 노트] 섹션의 내용만 근거로 답변하세요. 노트에 없는 내용을 추측하거나 만들어내지 마세요.
2. 답변은 한국어로 간결하게. 핵심부터 답하고 부연 설명은 그 다음.
3. 노트 간 정보가 충돌하면 명시적으로 언급하세요.
4. 답변 본문에는 [출처: ...] 표기를 넣지 마세요. 출처는 시스템이 자동으로 표시합니다.
5. 노트에 명시되지 않은 질문이면 "wiki에 관련 노트가 없음"이라고 답하고, 어떤 노트를 작성하면 좋을지 제안하세요.
6. 직전 대화 흐름이 있으면 자연스럽게 이어가세요.
"""


# mode별 파라미터 — fast는 컨텍스트·출력 길이 줄여 응답 시간 단축
MODE_PARAMS = {
    "fast":     {"k": 3, "num_ctx": 16384, "num_predict": 600,  "temperature": 0.3},
    "accurate": {"k": 6, "num_ctx": 32768, "num_predict": -1,   "temperature": 0.3},
}


def run(
    query: str,
    history: list[dict] | None = None,
    k: int | None = None,
    mode: str = "accurate",
) -> tuple[str, list[dict]]:
    """질의 → (답변 텍스트, 검색된 청크 리스트).

    mode ∈ {"fast", "accurate"}.
      fast:     k=3, num_ctx=16384, num_predict=600 — 빠른 답변 (~15-25초)
      accurate: k=6, num_ctx=32768, 무제한 num_predict — 풀 추론 (~50-80초)
    k 인자가 명시되면 mode의 기본값보다 우선.
    """
    mode = (mode or "accurate").strip().lower()
    if mode not in MODE_PARAMS:
        mode = "accurate"
    params = MODE_PARAMS[mode]
    effective_k = k if k is not None else params["k"]

    # 1. RAG 검색
    try:
        chunks = retrieve(query, k=effective_k)
    except Exception as e:
        log.exception("RAG retrieve 실패")
        return (f"검색 중 오류가 발생했습니다: {e}", [])

    if not chunks:
        return (
            "wiki에 관련 노트가 없습니다.\n"
            "텔레그램에서 /note 또는 raw/inbox/ 에 메모를 추가하면 자동 정제 후 인덱싱됩니다.",
            [],
        )

    # 2. 컨텍스트 + 히스토리 구성
    context = build_context(chunks)
    user_prompt = f"[참조 노트]\n{context}\n\n---\n\n[질문]\n{query}"

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    # 3. LLM 호출 (비스트리밍, mode별 옵션)
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {
            "temperature": params["temperature"],
            "num_ctx": params["num_ctx"],
            "num_predict": params["num_predict"],
        },
    }).encode("utf-8")

    req = Request(
        f"{OLLAMA_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=300) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.exception("LLM 호출 실패")
        return (f"답변 생성 중 오류가 발생했습니다: {e}", chunks)

    answer = (resp.get("message", {}).get("content") or "").strip()
    return (answer, chunks)


# ─── CLI 테스트 ──────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--mode", default="accurate", choices=["fast", "accurate"])
    args = ap.parse_args()
    answer, chunks = run(args.query, mode=args.mode)
    print(answer)
    print(f"\n--- 참조한 청크 {len(chunks)}개 (mode={args.mode}) ---")
    for c in chunks:
        print(f"  · {c['file']} :: {c.get('section', '')} (거리 {c.get('_distance', 0):.3f})")
