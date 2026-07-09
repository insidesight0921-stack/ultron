#!/usr/bin/env python3
"""
Wiki LanceDB 인덱서.

- wiki/ 폴더의 .md 파일을 H2(##) 섹션 단위로 청크 분할
- 각 청크를 nomic-embed-text로 임베딩 (Ollama API)
- LanceDB에 저장 + FTS 인덱스 생성
- 재실행 시 mtime 비교로 변경 파일만 재인덱싱

사용:
    python index_wiki.py                # 증분 인덱싱 (변경된 파일만)
    python index_wiki.py --rebuild      # 전체 재구성
    python index_wiki.py --stats        # 인덱스 통계만 출력
    python index_wiki.py --search "쿼리" # 빠른 테스트 검색
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

import lancedb
import numpy as np
import pyarrow as pa

# ─── 경로 설정 ──────────────────────────────────────
HOME = Path.home()
PROJECT = HOME / "울트론" / "ai-agent"
VAULT = HOME / "울트론" / "obsidian-vault"
WIKI = VAULT / "wiki"
DB_PATH = PROJECT / "data" / "lancedb"
DB_PATH.mkdir(parents=True, exist_ok=True)

OLLAMA_URL = "http://127.0.0.1:11434"
EMBED_MODEL = "bge-m3"        # 다국어 임베딩 (한국어 포함). 1024-dim
EMBED_DIM = 1024
TABLE_NAME = "wiki_chunks"


# ─── Markdown 청킹 ──────────────────────────────────

H2_PATTERN = re.compile(r"^## ", re.MULTILINE)
TAG_PATTERN = re.compile(r"#([\w/가-힣]+)")
LINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")

# 순수 메타 섹션 — RAG 검색 대상에서 제외 (정보 가치 낮고 잘못된 매칭 유발)
SKIP_SECTIONS = {
    "🏷️ 태그",
    "🔗 연결 노트",
    "📂 분류",
    "📅 메타",
}

MIN_CHUNK_CHARS = 80  # 너무 짧은 청크는 의미 모호 → 스킵


def split_by_h2_then_h3(md_text: str) -> list[tuple[str, str]]:
    """
    1차: H2(##)로 분할, 메타 섹션 제외
    2차: '📖 내용' 같은 큰 본문 섹션은 H3(###)로 추가 분할
    반환: [(섹션 경로, 본문), ...]
    """
    parts = re.split(r"^(## .+)$", md_text, flags=re.MULTILINE)
    if not parts:
        return []

    raw_sections: list[tuple[str, str]] = []
    if parts[0].strip():
        # 헤더 H1만 있는 인트로면 짧을 가능성 → 본문 길이로 판단
        raw_sections.append(("__intro__", parts[0].strip()))

    for i in range(1, len(parts), 2):
        header = parts[i].strip().removeprefix("## ").strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if header in SKIP_SECTIONS:
            continue
        if header or body:
            raw_sections.append((header, body))

    # 2차 분할: H3 있는 섹션은 sub-section 단위로 쪼갬
    final_sections: list[tuple[str, str]] = []
    for section, body in raw_sections:
        h3_parts = re.split(r"^(### .+)$", body, flags=re.MULTILINE)
        if len(h3_parts) <= 1:
            final_sections.append((section, body))
            continue

        # H3 이전 본문 (intro)
        if h3_parts[0].strip():
            final_sections.append((section, h3_parts[0].strip()))

        # 각 H3 sub-section
        for j in range(1, len(h3_parts), 2):
            sub_header = h3_parts[j].strip().removeprefix("### ").strip()
            sub_body = h3_parts[j + 1].strip() if j + 1 < len(h3_parts) else ""
            if sub_header or sub_body:
                final_sections.append((f"{section} / {sub_header}", sub_body))

    return final_sections


# 후방 호환 별칭
split_by_h2 = split_by_h2_then_h3


def extract_tags(text: str) -> list[str]:
    """#태그 추출 (계층형 #투자/매매규칙 포함)"""
    raw = TAG_PATTERN.findall(text)
    # 코드블록 안 # 제거 (간단히 ```으로 시작하는 라인 무시)
    return list(set(t for t in raw if not t.isdigit()))


def extract_links(text: str) -> list[str]:
    """[[연결 노트]] 추출"""
    return list(set(LINK_PATTERN.findall(text)))


# ─── Ollama 임베딩 ──────────────────────────────────

def embed(text: str, model: str = EMBED_MODEL) -> list[float]:
    """Ollama embeddings API 호출 + L2 정규화 (코사인 유사도 안정화)"""
    body = json.dumps({"model": model, "prompt": text}).encode("utf-8")
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


def check_ollama() -> bool:
    """Ollama 서버 살아있는지 확인"""
    try:
        with urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        models = [m["name"] for m in data.get("models", [])]
        if not any(EMBED_MODEL in m for m in models):
            print(f"❌ {EMBED_MODEL} 모델 없음. 먼저: ollama pull {EMBED_MODEL}")
            return False
        return True
    except Exception as e:
        print(f"❌ Ollama 연결 실패: {e}")
        print("   ollama serve가 실행 중인지 확인 (Ollama 앱이 켜져 있어야 함)")
        return False


# ─── LanceDB 스키마 + 연결 ──────────────────────────

SCHEMA = pa.schema([
    pa.field("id", pa.string()),                              # 파일경로#청크번호
    pa.field("file", pa.string()),                            # 상대 경로 (wiki/...)
    pa.field("section", pa.string()),                         # H2 섹션 제목
    pa.field("content", pa.string()),                         # 청크 본문
    pa.field("tags", pa.list_(pa.string())),                  # #태그
    pa.field("links", pa.list_(pa.string())),                 # [[연결]]
    pa.field("mtime", pa.float64()),                          # 파일 수정 시각 (epoch)
    pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),    # 임베딩
])


def connect_table(rebuild: bool = False) -> "lancedb.table.Table":
    db = lancedb.connect(str(DB_PATH))
    if rebuild and TABLE_NAME in db.table_names():
        print(f"🔄 기존 테이블 {TABLE_NAME} 삭제 (--rebuild)")
        db.drop_table(TABLE_NAME)
    if TABLE_NAME not in db.table_names():
        table = db.create_table(TABLE_NAME, schema=SCHEMA)
    else:
        table = db.open_table(TABLE_NAME)
    return table


# ─── 파일 처리 ──────────────────────────────────────

def process_file(md_path: Path) -> list[dict]:
    """단일 .md 파일 → 청크 리스트 (H2 + H3 2단계 분할)"""
    text = md_path.read_text(encoding="utf-8")
    sections = split_by_h2_then_h3(text)
    if not sections:
        return []

    rel_path = str(md_path.relative_to(VAULT))
    mtime = md_path.stat().st_mtime
    file_tags = extract_tags(text)
    file_links = extract_links(text)
    title = md_path.stem  # 파일명 (확장자 제외)

    records = []
    idx = 0
    for section, body in sections:
        # 청크 텍스트: 파일 제목을 항상 포함해서 컨텍스트 제공
        if section == "__intro__":
            chunk_text = f"[{title}]\n{body}"
        else:
            chunk_text = f"[{title}] {section}\n\n{body}"

        # 너무 짧은 청크는 임베딩 노이즈 — 스킵
        if len(chunk_text.strip()) < MIN_CHUNK_CHARS:
            continue

        records.append({
            "id": f"{rel_path}#{idx}",
            "file": rel_path,
            "section": section,
            "content": chunk_text,
            "tags": file_tags,
            "links": file_links,
            "mtime": mtime,
            "vector": [],  # 나중에 채움
        })
        idx += 1
    return records


def needs_reindex(table, file_path: str, mtime: float) -> bool:
    """기존 인덱스의 mtime과 비교"""
    try:
        existing = table.search().where(f"file = '{file_path}'").limit(1).to_list()
        if not existing:
            return True
        return existing[0]["mtime"] < mtime
    except Exception:
        return True


def index_all(rebuild: bool = False) -> tuple[int, int, int]:
    """wiki/ 전체 인덱싱. 반환: (스캔, 임베딩, 스킵)"""
    if not check_ollama():
        sys.exit(1)

    table = connect_table(rebuild=rebuild)
    md_files = sorted(WIKI.rglob("*.md"))
    if not md_files:
        print(f"⚠️ {WIKI}에 .md 파일 없음")
        return 0, 0, 0

    scanned, embedded, skipped = 0, 0, 0
    new_records = []

    print(f"📂 wiki 스캔: {len(md_files)}개 파일")
    for md in md_files:
        scanned += 1
        rel = str(md.relative_to(VAULT))
        mtime = md.stat().st_mtime

        if not rebuild and not needs_reindex(table, rel, mtime):
            skipped += 1
            continue

        # 변경됨 — 기존 청크 제거 후 재처리
        if not rebuild:
            try:
                table.delete(f"file = '{rel}'")
            except Exception:
                pass

        records = process_file(md)
        if not records:
            continue

        for rec in records:
            try:
                rec["vector"] = embed(rec["content"])
                embedded += 1
                new_records.append(rec)
            except Exception as e:
                print(f"  ❌ 임베딩 실패 {rec['id']}: {e}")

        print(f"  ✅ {rel} ({len(records)} chunks)")

    if new_records:
        table.add(new_records)

    print(f"\n📊 결과: 스캔 {scanned} / 임베딩 {embedded} / 스킵 {skipped}")
    print(f"📍 LanceDB: {DB_PATH}")
    return scanned, embedded, skipped


# ─── 빠른 검색 (디버그용) ────────────────────────────

def quick_search(query: str, k: int = 5) -> None:
    table = connect_table()
    qvec = embed(query)
    results = table.search(qvec).metric("cosine").limit(k).to_list()

    print(f"\n🔍 쿼리: {query}")
    print("=" * 60)
    for i, r in enumerate(results, 1):
        score = r.get("_distance", 0)
        print(f"\n[{i}] {r['file']} :: {r['section']}  (거리 {score:.3f})")
        # 청크 본문 첫 200자만
        snippet = r["content"][:200].replace("\n", " ")
        print(f"    {snippet}...")


def show_stats() -> None:
    table = connect_table()
    n = table.count_rows()
    print(f"📊 인덱스 통계")
    print(f"  - 청크 수: {n}")
    if n > 0:
        # 파일별 청크 수 집계
        rows = table.to_pandas()[["file", "section"]]
        files = rows["file"].value_counts()
        print(f"  - 파일 수: {len(files)}")
        print(f"  - 평균 청크/파일: {n/len(files):.1f}")
        print(f"\n  파일별:")
        for f, cnt in files.items():
            print(f"    {cnt:3d} chunks  {f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="전체 테이블 삭제 후 재구성")
    ap.add_argument("--stats", action="store_true", help="현재 인덱스 통계만 출력")
    ap.add_argument("--search", help="빠른 검색 테스트 (인덱싱 없이)")
    args = ap.parse_args()

    start = time.time()
    if args.stats:
        show_stats()
    elif args.search:
        quick_search(args.search)
    else:
        index_all(rebuild=args.rebuild)
    print(f"\n⏱  소요: {time.time()-start:.1f}s")


if __name__ == "__main__":
    main()
