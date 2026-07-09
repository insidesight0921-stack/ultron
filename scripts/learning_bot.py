#!/usr/bin/env python3
"""
학습봇 (learning_bot) — refine_raw 강화: NEW / MERGE / SKIP 머지 판단.

기존 refine_raw는 "같은 제목 wiki 있으면 SKIP" 한 줄 로직만 있었다.
하지만 현실에서는:
  - 제목은 다르지만 주제가 거의 같은 노트가 자주 생긴다
    (예: "정찰병_진입_전략" vs "정찰병_규칙")
  - 동일 주제에 새 정보가 추가된 경우 → 단순 SKIP은 정보 손실

learning_bot.decide_action()은:
  1. 정제 결과(`refined`)와 제안 저장 경로(`target`)를 받음
  2. 동일 파일명 wiki가 이미 있으면 → MERGE 후보 (해당 파일 기존 본문과 합침)
  3. 그렇지 않으면 RAG로 매우 유사한 노트 검색 (cosine 거리 < SIMILAR_THRESHOLD)
     - 후보 발견 → MERGE (해당 파일에 통합)
     - 후보 없음 → NEW
  4. MERGE인 경우 Gemma 4 31B에게 "기존 본문 + 새 정보"를 통합 본문으로
     재작성하도록 요청 (중복 제거, 시간순 보존, 출처 둘 다 유지)

반환: ("NEW"|"MERGE"|"SKIP", target_path, merged_content_or_None, debug_info)
  - NEW: target 그대로 저장
  - MERGE: target에 merged_content 덮어쓰기 (or 다른 경로일 수 있음 → 첫 인자 경로 사용)
  - SKIP: 저장 자체를 건너뜀 (내용이 본질적으로 동일 — LLM이 판단)

설계 요약:
- LLM 호출은 MERGE 결정 후 통합 본문 생성 1회로 한정 (정제는 이미 refine_raw에서 했음)
- 임베딩 검색은 "정제된 본문 요약+제목"을 쿼리로 사용
- MERGE/SKIP 임계치는 보수적으로 (오인 머지보다 NEW가 안전)
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.request import Request, urlopen

# refine_raw / ask 와 같은 LanceDB·임베딩 모델 사용
from ask import retrieve, LLM_MODEL

OLLAMA_URL = "http://127.0.0.1:11434"
KEEP_ALIVE = "30m"

# RAG 후보 중 cosine 거리가 이 값 미만이면 "같은 주제" 후보로 본다.
# bge-m3 정규화 cosine: 0.0 = 동일, 0.2 이하 = 매우 유사, 0.4 이상 = 다른 주제.
SIMILAR_THRESHOLD = 0.18

log = logging.getLogger("learning_bot")


# ─── LLM 호출 헬퍼 ───────────────────────────────────


def _chat(messages: list[dict], temperature: float = 0.2) -> str:
    body = json.dumps(
        {
            "model": LLM_MODEL,
            "messages": messages,
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": temperature, "num_ctx": 32768},
        }
    ).encode("utf-8")
    req = Request(
        f"{OLLAMA_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"]


# ─── 후보 검색 ───────────────────────────────────────


def find_similar_note(
    refined: dict,
    target_path: Path,
    threshold: float = SIMILAR_THRESHOLD,
    k: int = 5,
) -> Path | None:
    """RAG로 매우 유사한 기존 노트 1개 찾기. 자기 자신은 제외.

    검색 쿼리는 (제목 + 요약 + 본문 앞 600자). 결과가 임계치 미만 거리면 그
    파일을 머지 후보로 반환. 같은 파일명이 검색돼도 OK — 호출 측에서
    이미 target_path 존재 검사를 먼저 하므로.
    """
    title = (refined.get("title") or "").strip()
    summary = (refined.get("summary") or "").strip()
    content = (refined.get("content") or "").strip()
    query = f"{title}\n{summary}\n{content[:600]}"

    try:
        chunks = retrieve(query, k=k)
    except Exception as e:
        log.warning(f"RAG 검색 실패 (그래도 NEW로 진행): {e}")
        return None

    if not chunks:
        return None

    # 동일 파일(target과 같은 경로)는 제외하고 가장 가까운 다른 파일을 찾는다.
    # target_path는 .../wiki/{domain}/{file}.md 형태이므로 'wiki' 폴더의 부모를
    # vault root로 추론. 못 찾으면 안전하게 target_path.parent로 fallback.
    vault_root = None
    for parent in target_path.parents:
        if parent.name == "wiki":
            vault_root = parent.parent
            break
    if vault_root is None:
        vault_root = target_path.parent

    target_str = str(target_path).lower()
    for c in chunks:
        cand = c.get("file", "")
        if not cand:
            continue
        # 'wiki/투자/foo.md' → 절대경로화 비교
        cand_path = (vault_root / cand) if not Path(cand).is_absolute() else Path(cand)
        if str(cand_path).lower() == target_str:
            continue
        dist = c.get("_distance", 1.0)
        if dist <= threshold:
            log.info(f"🔗 머지 후보 발견: {cand} (거리 {dist:.3f} ≤ {threshold})")
            return cand_path
    return None


# ─── 본문 병합 ───────────────────────────────────────


MERGE_SYSTEM_PROMPT = """당신은 현준의 위키 노트 병합 도우미입니다.

기존 위키 노트와 새 정제 결과를 합쳐 하나의 통합 노트 본문(📖 내용 섹션 텍스트)으로 재작성합니다.

규칙:
1. 두 입력에 동일한 사실이 있으면 한 번만. 충돌하면 둘 다 명시 (날짜와 함께).
2. 시간 순서 정보는 보존 (예: "2026-04 ~~원칙 → 2026-05 변경됨").
3. 새 정보를 누락하지 마세요. 기존 정보도 누락하지 마세요.
4. 출력은 통합 본문(마크다운)만. JSON, 코드블록, 메타 헤더(##) 같은 건 출력하지 마세요.
5. 추측·일반론 금지. 두 입력에 명시된 내용만.
"""


def merge_content(existing_text: str, refined: dict) -> str:
    """기존 wiki 노트 전체 텍스트 + 새 정제 결과 → 통합 본문 마크다운."""
    new_summary = (refined.get("summary") or "").strip()
    new_content = (refined.get("content") or "").strip()
    new_title = (refined.get("title") or "").strip()

    user_msg = (
        "## 기존 위키 노트 (전체 텍스트)\n"
        f"```\n{existing_text}\n```\n\n"
        "## 새 정제 결과\n"
        f"제목: {new_title}\n"
        f"요약: {new_summary}\n\n"
        f"본문:\n```\n{new_content}\n```\n\n"
        "위 두 입력을 통합한 노트 본문(📖 내용 섹션에 들어갈 마크다운)만 출력하세요. 다른 텍스트 금지."
    )

    raw = _chat(
        [
            {"role": "system", "content": MERGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
    )
    # 혹시 코드블록을 감쌌으면 벗기기
    m = re.search(r"```(?:markdown|md)?\s*(.+?)\s*```", raw, re.DOTALL)
    return m.group(1).strip() if m else raw.strip()


def render_merged_wiki(existing_text: str, merged_body: str) -> str:
    """기존 wiki 파일의 메타 섹션(태그/연결/분류/메타)은 유지하고 📖 내용만 교체."""
    # "## 📖 내용" 섹션을 찾아서 다음 ## 직전까지 교체
    pattern = re.compile(r"(## 📖 내용\s*\n)(.*?)(?=\n## |\Z)", re.DOTALL)
    if pattern.search(existing_text):
        replaced = pattern.sub(lambda m: f"{m.group(1)}{merged_body}\n", existing_text, count=1)
        return replaced
    # 패턴 못 찾으면 안전하게 끝에 추가
    return existing_text.rstrip() + "\n\n## 📖 내용 (병합)\n" + merged_body + "\n"


# ─── 메인 entrypoint ─────────────────────────────────


def decide_action(
    refined: dict,
    proposed_target: Path,
    *,
    threshold: float = SIMILAR_THRESHOLD,
) -> tuple[str, Path, str | None, dict]:
    """정제 결과 → ("NEW"|"MERGE"|"SKIP", final_target, merged_text_or_None, debug)

    proposed_target: refine_raw가 원래 저장하려던 경로 (도메인 폴더 + safe_filename).
    final_target:    실제 저장할 경로.
                     NEW   → proposed_target
                     MERGE → 유사 노트 경로 (proposed_target과 다를 수 있음)
                     SKIP  → proposed_target (사용 안 함)

    merged_text:
      NEW   → None  (refine_raw가 render_wiki로 새로 작성)
      MERGE → 머지된 wiki 파일 전체 텍스트 (그대로 file.write_text)
      SKIP  → None
    """
    debug: dict = {"threshold": threshold}

    # 1. 같은 파일명 이미 존재 → 그 파일에 머지
    if proposed_target.exists():
        debug["same_filename_exists"] = True
        existing = proposed_target.read_text(encoding="utf-8")
        try:
            merged_body = merge_content(existing, refined)
        except Exception as e:
            log.warning(f"머지 본문 생성 실패 → SKIP 처리: {e}")
            return ("SKIP", proposed_target, None, {**debug, "merge_error": str(e)})
        merged_text = render_merged_wiki(existing, merged_body)
        return ("MERGE", proposed_target, merged_text, debug)

    # 2. RAG로 유사 노트 검색
    similar = find_similar_note(refined, proposed_target, threshold=threshold)
    if similar is None or not similar.exists():
        debug["similar_found"] = False
        return ("NEW", proposed_target, None, debug)

    debug["similar_found"] = True
    debug["similar_path"] = str(similar)

    existing = similar.read_text(encoding="utf-8")
    try:
        merged_body = merge_content(existing, refined)
    except Exception as e:
        log.warning(f"머지 본문 생성 실패 → NEW로 fallback: {e}")
        debug["merge_error"] = str(e)
        return ("NEW", proposed_target, None, debug)

    merged_text = render_merged_wiki(existing, merged_body)
    return ("MERGE", similar, merged_text, debug)


# ─── CLI 테스트 ──────────────────────────────────────


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="learning_bot 단독 테스트")
    ap.add_argument("title")
    ap.add_argument("--summary", default="")
    ap.add_argument("--content", default="")
    ap.add_argument(
        "--target",
        required=True,
        help="제안 저장 경로 (예: ~/울트론/obsidian-vault/wiki/투자/foo.md)",
    )
    ap.add_argument("--threshold", type=float, default=SIMILAR_THRESHOLD)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    refined = {
        "title": args.title,
        "summary": args.summary,
        "content": args.content,
    }
    target = Path(args.target).expanduser()
    action, final, merged, debug = decide_action(refined, target, threshold=args.threshold)
    print(f"action  = {action}")
    print(f"final   = {final}")
    print(f"merged? = {bool(merged)}")
    print(f"debug   = {json.dumps(debug, ensure_ascii=False)}")
    if merged:
        print("---- merged text (앞 500자) ----")
        print(merged[:500])


if __name__ == "__main__":
    _cli()
