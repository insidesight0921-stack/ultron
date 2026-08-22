#!/usr/bin/env python3
"""
raw → wiki 자동 정제 (Gemma 4 31B).

흐름:
  1. raw/ 파일 읽기
  2. 기존 wiki RAG 검색 → 관련 노트 Top-5 컨텍스트 제공
  3. Gemma 4가 wiki_template 형식으로 정제 + 태그/분류/연결 자동 생성
  4. (옵션) 사람 확인 후 wiki/{domain}/{filename}.md 저장
  5. 저장 후 LanceDB 재인덱싱 (index_wiki.py 호출)

사용:
    # 단일 파일 정제 (대화형 — 결과 확인 후 저장 여부 결정)
    python refine_raw.py raw/투자/test_idea.md

    # 자동 저장 (확인 단계 생략, 자동화용)
    python refine_raw.py raw/투자/test_idea.md --auto

    # raw/ 전체 미정제 파일 일괄
    python refine_raw.py --all

    # 정제만 해보고 저장 안 함 (드라이런)
    python refine_raw.py raw/투자/test_idea.md --dry-run
"""
from __future__ import annotations
import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import lancedb
import numpy as np

from storage_paths import PATHS

HOME = Path.home()
PROJECT = HOME / "울트론" / "ai-agent"
VAULT = HOME / "울트론" / "obsidian-vault"
RAW = VAULT / "raw"
WIKI = VAULT / "wiki"
TEMPLATE_PATH = VAULT / "templates" / "wiki_template.md"
DB_PATH = PATHS.rag_dir
PROCESSED_LOG = PATHS.raw_processed

OLLAMA_URL = "http://127.0.0.1:11434"
EMBED_MODEL = "bge-m3"
LLM_MODEL = "gemma4:31b"
TABLE_NAME = "wiki_chunks"
KEEP_ALIVE = "30m"

# 도메인 → wiki 폴더 매핑
DOMAIN_FOLDERS = {
    "투자": "투자",
    "코딩": "코딩",
    "금융": "금융",
    "학습": "학습",
    "비즈니스": "비즈니스",
}


SYSTEM_PROMPT = """당신은 현준의 노트 정제 도우미입니다.

raw 메모를 받아서 아래 형식의 정제된 위키 노트로 변환합니다.

## 출력 형식 (반드시 이 JSON 구조로만 응답)
```json
{
  "title": "노트 제목 (간결하게 핵심 명사구)",
  "domain": "투자|코딩|금융|학습|비즈니스 중 하나",
  "type": "개념|전략|규칙|아이디어 중 하나",
  "summary": "한두 줄 요약 (📌 요약에 들어갈 텍스트)",
  "content": "정제된 본문 (📖 내용에 들어갈 마크다운. 원문 의도 보존하면서 구조화)",
  "tags": ["#투자/기술적분석", "#투자/전략"],
  "linked_notes": ["관련 위키 노트 제목 (현재 vault에 있는 것만)"]
}
```

규칙:
1. 원문에 없는 사실을 절대 추가하지 마세요. 추측이나 일반론 금지.
2. 원문이 짧으면 짧게, 본문이 코드/표면 그대로 보존.
3. 태그는 계층형 (#투자/매매규칙). 2~5개 적정.
4. 연결 노트는 [참조 노트] 섹션에 있는 제목만 사용. 없으면 빈 리스트.
5. 응답은 ```json ... ``` 코드블록 안에 JSON만. 다른 설명 금지.
"""


# ─── Ollama 헬퍼 ────────────────────────────────────

def embed(text: str) -> list[float]:
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = Request(f"{OLLAMA_URL}/api/embeddings", data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    vec = np.array(data["embedding"], dtype=np.float32)
    norm = np.linalg.norm(vec)
    return (vec / norm).tolist() if norm else vec.tolist()


def chat(messages: list[dict], stream: bool = False) -> str:
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": messages,
        "stream": stream,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0.2, "num_ctx": 32768},
    }).encode("utf-8")
    req = Request(f"{OLLAMA_URL}/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=300) as r:
        if stream:
            out = []
            for line in r:
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line.decode("utf-8"))
                    msg = chunk.get("message", {}).get("content", "")
                    out.append(msg)
                    print(msg, end="", flush=True)
                    if chunk.get("done"):
                        print()
                except json.JSONDecodeError:
                    pass
            return "".join(out)
        else:
            return json.loads(r.read().decode("utf-8"))["message"]["content"]


# ─── RAG 검색 ───────────────────────────────────────

def find_related_notes(query: str, k: int = 5) -> list[dict]:
    """기존 wiki에서 관련 노트 검색 → 연결 추천용"""
    db = lancedb.connect(str(DB_PATH))
    if TABLE_NAME not in db.table_names():
        return []
    table = db.open_table(TABLE_NAME)
    qvec = embed(query)
    return table.search(qvec).metric("cosine").limit(k).to_list()


def existing_note_titles() -> list[str]:
    """현재 wiki/ 의 모든 노트 제목 (확장자 제외)"""
    return sorted({p.stem for p in WIKI.rglob("*.md")})


# ─── LLM 정제 ───────────────────────────────────────

def parse_llm_json(text: str) -> dict:
    """LLM 출력에서 JSON 추출 (코드블록 처리 포함)"""
    # ```json ... ``` 블록 우선
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # 그 외에는 첫 { 부터 마지막 } 까지
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("LLM 응답에서 JSON 추출 실패")


def refine(raw_text: str, raw_filename: str) -> dict:
    """raw 메모 → 정제된 dict"""
    # 관련 노트 검색
    related = find_related_notes(raw_text, k=5)
    related_titles = list({Path(r["file"]).stem for r in related})

    # LLM 입력 구성
    titles_csv = ", ".join(existing_note_titles())
    related_section = "\n".join(
        f"- [{Path(r['file']).stem}] {r['section']}: {r['content'][:200]}..."
        for r in related
    ) or "(없음)"

    user_msg = (
        f"## raw 메모\n파일명: {raw_filename}\n```\n{raw_text}\n```\n\n"
        f"## 참조 노트 (RAG 검색 Top-5, 연결 후보)\n{related_section}\n\n"
        f"## vault 전체 노트 제목 목록\n{titles_csv}\n\n"
        f"위 raw 메모를 정제해서 JSON으로 출력하세요."
    )

    response = chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ], stream=False)

    return parse_llm_json(response)


# ─── 위키 노트 작성 ─────────────────────────────────

def render_wiki(refined: dict) -> str:
    """정제 dict → wiki 마크다운"""
    today = time.strftime("%Y-%m-%d")
    title = refined["title"]
    domain = refined.get("domain", "학습")
    type_ = refined.get("type", "개념")
    summary = refined.get("summary", "")
    content = refined.get("content", "")
    tags = refined.get("tags", [])
    links = refined.get("linked_notes", [])

    tags_str = " ".join(t if t.startswith("#") else f"#{t}" for t in tags)
    links_str = "\n".join(f"- [[{l}]]" for l in links) or "- "

    return f"""# {title}

## 📌 요약
{summary}

## 📖 내용
{content}

## 🏷️ 태그
{tags_str}

## 🔗 연결 노트
{links_str}

## 📂 분류
- 도메인: {domain}
- 유형: {type_}

## 📅 메타
- 생성일: {today}
- 출처: raw 자동 정제 (Gemma 4 31B)
"""


def safe_filename(title: str) -> str:
    """파일명에 부적합한 문자 제거"""
    s = re.sub(r"[\\/:*?\"<>|]", "_", title)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s[:80]  # 최대 80자


def save_to_wiki(refined: dict, raw_path: Path) -> tuple[Path, str]:
    """정제 결과를 wiki/ 적절한 폴더에 저장.

    learning_bot.decide_action()이 NEW/MERGE/SKIP을 결정한다:
      - NEW   → render_wiki()로 새 파일 생성
      - MERGE → 유사/동명 노트의 본문에 통합한 텍스트 덮어쓰기
      - SKIP  → 저장 안 함 (LLM이 머지 본문 생성에 실패한 경우 등)

    반환: (target_path, action)  action ∈ {"NEW", "MERGE", "SKIP"}
    호출 측은 action == "NEW" or "MERGE" 일 때 인덱싱 트리거 권장.
    """
    domain = refined.get("domain", "학습")
    folder = WIKI / DOMAIN_FOLDERS.get(domain, "학습")
    folder.mkdir(parents=True, exist_ok=True)

    filename = safe_filename(refined["title"]) + ".md"
    proposed = folder / filename

    # learning_bot이 RAG 기반 머지/신규 판단
    try:
        from learning_bot import decide_action
        action, target, merged_text, debug = decide_action(refined, proposed)
    except Exception as e:
        # learning_bot 실패 시 기존 동작 (동명 존재면 SKIP, 아니면 NEW)으로 안전 fallback
        print(f"⚠️  learning_bot 실패 → 기본 분기로 fallback: {e}")
        if proposed.exists():
            return (proposed, "SKIP")
        proposed.write_text(render_wiki(refined), encoding="utf-8")
        return (proposed, "NEW")

    if action == "NEW":
        target.write_text(render_wiki(refined), encoding="utf-8")
        return (target, "NEW")
    if action == "MERGE":
        if merged_text:
            target.write_text(merged_text, encoding="utf-8")
            return (target, "MERGE")
        # merged_text 없는 MERGE는 비정상 → SKIP
        return (target, "SKIP")
    # SKIP
    return (target, "SKIP")


def load_processed_log() -> dict:
    if PROCESSED_LOG.exists():
        return json.loads(PROCESSED_LOG.read_text(encoding="utf-8"))
    return {}


def save_processed_log(log: dict) -> None:
    PROCESSED_LOG.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── 인터랙티브 확인 ────────────────────────────────

def confirm_refinement(refined: dict, raw_path: Path) -> bool:
    """정제 결과 사람 확인"""
    print("\n" + "=" * 60)
    print(f"📄 원본: {raw_path.name}")
    print("=" * 60)
    print(f"제목:   {refined['title']}")
    print(f"도메인: {refined.get('domain', '?')}")
    print(f"유형:   {refined.get('type', '?')}")
    print(f"태그:   {', '.join(refined.get('tags', []))}")
    print(f"연결:   {', '.join(refined.get('linked_notes', [])) or '(없음)'}")
    print()
    print("📌 요약")
    print(refined.get("summary", ""))
    print()
    print("📖 내용 (처음 500자)")
    content = refined.get("content", "")
    print(content[:500] + ("..." if len(content) > 500 else ""))
    print("=" * 60)

    while True:
        choice = input("저장? [y]es / [n]o / [e]dit 제목: ").strip().lower()
        if choice in ("y", "yes", ""):
            return True
        if choice in ("n", "no"):
            return False
        if choice in ("e", "edit"):
            new_title = input(f"새 제목 (현재: {refined['title']}): ").strip()
            if new_title:
                refined["title"] = new_title
            print(f"제목 변경됨: {refined['title']}")
            continue
        print("y / n / e 중에 입력")


# ─── 처리 함수 ──────────────────────────────────────

def process_one(raw_path: Path, auto: bool, dry_run: bool, log: dict) -> Path | None:
    print(f"\n🔧 정제 중: {raw_path.relative_to(VAULT)}")
    raw_text = raw_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        print("⚠️  빈 파일 — 스킵")
        return None

    try:
        refined = refine(raw_text, raw_path.name)
    except Exception as e:
        print(f"❌ 정제 실패: {e}")
        return None

    if not auto and not dry_run:
        if not confirm_refinement(refined, raw_path):
            print("⏭  건너뜀")
            return None
    elif dry_run:
        confirm_refinement(refined, raw_path)
        print("\n💡 --dry-run 모드: 저장 안 함")
        return None
    else:
        print(f"  → 자동 저장: {refined['title']}")

    target, action = save_to_wiki(refined, raw_path)
    if action == "NEW":
        print(f"✅ 저장됨 (NEW): {target.relative_to(VAULT)}")
    elif action == "MERGE":
        print(f"🔗 병합됨 (MERGE → 기존 노트 갱신): {target.relative_to(VAULT)}")
    else:
        print(f"⏭  스킵 (SKIP): {target.relative_to(VAULT)}")

    # 처리 로그 갱신 (어느 결과든 raw는 processed로 표시 → 재정제 방지)
    log[str(raw_path.relative_to(VAULT))] = {
        "processed_at": time.time(),
        "target": str(target.relative_to(VAULT)),
        "raw_mtime": raw_path.stat().st_mtime,
        "action": action,
    }
    save_processed_log(log)
    # NEW 또는 MERGE면 인덱싱 트리거 (wiki 변경 발생)
    return target if action in ("NEW", "MERGE") else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", help="raw/ 폴더 안의 파일 (절대/상대 경로 모두 OK)")
    ap.add_argument("--all", action="store_true", help="raw/ 의 모든 미처리 파일 일괄")
    ap.add_argument("--auto", action="store_true", help="확인 없이 자동 저장")
    ap.add_argument("--dry-run", action="store_true", help="정제만 하고 저장 안 함")
    ap.add_argument("--reindex", action="store_true", help="저장 후 LanceDB 재인덱싱")
    args = ap.parse_args()

    log = load_processed_log()
    saved_files = []

    if args.all:
        # raw/ 전체 미처리 파일
        all_md = sorted(RAW.rglob("*.md"))
        unprocessed = []
        for p in all_md:
            rel = str(p.relative_to(VAULT))
            entry = log.get(rel)
            if not entry or entry.get("raw_mtime", 0) < p.stat().st_mtime:
                unprocessed.append(p)
        if not unprocessed:
            print("✅ 미처리 raw 파일 없음")
            return
        print(f"📂 {len(unprocessed)}개 파일 처리 시작")
        for p in unprocessed:
            t = process_one(p, args.auto, args.dry_run, log)
            if t:
                saved_files.append(t)

    elif args.file:
        p = Path(args.file)
        if not p.is_absolute():
            # 상대 경로면 VAULT 기준
            cand1 = VAULT / args.file
            cand2 = RAW / args.file
            p = cand1 if cand1.exists() else cand2
        if not p.exists():
            sys.exit(f"❌ 파일 없음: {p}")
        t = process_one(p, args.auto, args.dry_run, log)
        if t:
            saved_files.append(t)

    else:
        ap.print_help()
        sys.exit(1)

    # 자동 재인덱싱
    if saved_files and args.reindex:
        print("\n🔄 LanceDB 재인덱싱...")
        import subprocess
        subprocess.run([
            sys.executable,
            str(Path(__file__).parent / "index_wiki.py")
        ])

    if saved_files:
        print(f"\n📊 총 {len(saved_files)}개 노트 저장")
        for f in saved_files:
            print(f"   - {f.relative_to(VAULT)}")


if __name__ == "__main__":
    main()
