"""
learning_bot 단위 테스트.

LLM 호출(_chat) + RAG 검색(retrieve)은 모두 monkeypatch로 가짜화.
순수 결정 로직만 검증한다:
  - 동명 wiki 존재 → MERGE
  - RAG 거리 < threshold → MERGE
  - 거리 ≥ threshold → NEW
  - LLM 머지 실패 → SKIP (동명) 또는 NEW로 fallback (유사)
  - render_merged_wiki: 📖 내용 섹션만 교체, 다른 섹션 보존
"""
from __future__ import annotations
from pathlib import Path

import pytest

import learning_bot as lb


# ─── render_merged_wiki ──────────────────────────────


def test_render_merged_wiki_replaces_only_content():
    existing = (
        "# 제목\n\n"
        "## 📌 요약\n원래 요약\n\n"
        "## 📖 내용\n원래 본문\n\n"
        "## 🏷️ 태그\n#투자/원칙\n\n"
        "## 📅 메타\n생성일: 2026-04-01\n"
    )
    out = lb.render_merged_wiki(existing, "통합된 새 본문")
    assert "## 📌 요약\n원래 요약" in out
    assert "## 🏷️ 태그\n#투자/원칙" in out
    assert "## 📅 메타\n생성일: 2026-04-01" in out
    assert "통합된 새 본문" in out
    assert "원래 본문" not in out  # 교체됐어야 함


def test_render_merged_wiki_when_no_content_section():
    # 📖 내용 섹션이 없으면 끝에 추가
    existing = "# 제목\n\n## 📌 요약\n뭐\n"
    out = lb.render_merged_wiki(existing, "새 본문")
    assert "## 📖 내용 (병합)" in out
    assert "새 본문" in out


# ─── decide_action: 동명 파일 존재 ───────────────────


def test_decide_action_existing_filename_merges(tmp_path, monkeypatch):
    target = tmp_path / "investing_rule.md"
    target.write_text(
        "# 투자 규칙\n\n"
        "## 📌 요약\n초기 요약\n\n"
        "## 📖 내용\n원래 본문\n",
        encoding="utf-8",
    )

    refined = {"title": "투자 규칙", "summary": "새 요약", "content": "추가 정보"}

    # _chat 가짜 — 머지된 본문 반환
    def fake_chat(messages, temperature=0.2):
        return "통합 본문 (가짜 LLM 출력)"

    monkeypatch.setattr(lb, "_chat", fake_chat)

    action, final, merged_text, debug = lb.decide_action(refined, target)
    assert action == "MERGE"
    assert final == target
    assert merged_text is not None
    assert "통합 본문" in merged_text
    assert debug["same_filename_exists"] is True


def test_decide_action_existing_filename_merge_failure_skips(tmp_path, monkeypatch):
    target = tmp_path / "rule.md"
    target.write_text("# x\n\n## 📖 내용\n원본\n", encoding="utf-8")

    def boom(messages, temperature=0.2):
        raise RuntimeError("LLM 죽음")

    monkeypatch.setattr(lb, "_chat", boom)

    refined = {"title": "x", "summary": "", "content": "y"}
    action, final, merged_text, debug = lb.decide_action(refined, target)
    assert action == "SKIP"
    assert merged_text is None
    assert "merge_error" in debug


# ─── decide_action: RAG 유사 노트 ────────────────────


def test_decide_action_no_similar_returns_new(tmp_path, monkeypatch):
    target = tmp_path / "novel.md"
    refined = {"title": "신규 주제", "summary": "x", "content": "y"}

    monkeypatch.setattr(lb, "retrieve", lambda q, k=5: [])

    action, final, merged_text, debug = lb.decide_action(refined, target)
    assert action == "NEW"
    assert final == target
    assert merged_text is None
    assert debug["similar_found"] is False


def test_decide_action_similar_above_threshold_returns_new(tmp_path, monkeypatch):
    target = tmp_path / "wiki" / "투자" / "신주제.md"
    refined = {"title": "신주제", "summary": "x", "content": "y"}

    fake_chunk = {
        "file": "wiki/투자/다른주제.md",
        "section": "📖 내용",
        "content": "전혀 다른 얘기",
        "_distance": 0.45,  # threshold 0.18 위
    }
    monkeypatch.setattr(lb, "retrieve", lambda q, k=5: [fake_chunk])

    action, _, _, debug = lb.decide_action(refined, target)
    assert action == "NEW"
    assert debug.get("similar_found") in (False, None)


def test_decide_action_similar_below_threshold_merges(tmp_path, monkeypatch):
    # vault 루트 흉내 — wiki/투자/유사노트.md가 실제 파일로 존재해야 read 가능
    vault_root = tmp_path
    similar_rel = "wiki/투자/유사노트.md"
    similar_path = vault_root / similar_rel
    similar_path.parent.mkdir(parents=True, exist_ok=True)
    similar_path.write_text(
        "# 유사노트\n\n## 📌 요약\nold\n\n## 📖 내용\n기존\n", encoding="utf-8"
    )

    proposed = vault_root / "wiki" / "투자" / "신규제목.md"  # 다른 파일명

    fake_chunk = {
        "file": similar_rel,
        "section": "📖 내용",
        "content": "유사한 내용",
        "_distance": 0.10,  # threshold 0.18 미만 → MERGE
    }
    monkeypatch.setattr(lb, "retrieve", lambda q, k=5: [fake_chunk])
    monkeypatch.setattr(lb, "_chat", lambda msgs, temperature=0.2: "통합된 본문 v2")

    refined = {"title": "신규제목", "summary": "new", "content": "추가"}
    action, final, merged_text, debug = lb.decide_action(refined, proposed)

    assert action == "MERGE"
    assert final == similar_path  # 유사 노트 경로로 저장돼야 함
    assert merged_text is not None
    assert "통합된 본문 v2" in merged_text
    # 다른 메타 섹션은 보존
    assert "## 📌 요약" in merged_text
    assert debug["similar_found"] is True


def test_decide_action_similar_self_excluded(tmp_path, monkeypatch):
    """proposed_target과 같은 경로로 검색되면 후보에서 제외 → NEW."""
    target = tmp_path / "wiki" / "투자" / "self.md"
    target.parent.mkdir(parents=True, exist_ok=True)

    # 같은 경로지만 아직 파일은 없음 → existing 분기 안 탐
    fake_chunk = {
        "file": "wiki/투자/self.md",
        "section": "x",
        "content": "y",
        "_distance": 0.05,
    }
    monkeypatch.setattr(lb, "retrieve", lambda q, k=5: [fake_chunk])

    refined = {"title": "self", "summary": "x", "content": "y"}
    action, _, _, _ = lb.decide_action(refined, target)
    # 자기 자신 제외 + 다른 후보 없음 → NEW
    assert action == "NEW"


def test_decide_action_similar_merge_failure_falls_back_to_new(tmp_path, monkeypatch):
    similar_rel = "wiki/투자/유사.md"
    similar_path = tmp_path / similar_rel
    similar_path.parent.mkdir(parents=True, exist_ok=True)
    similar_path.write_text("# x\n## 📖 내용\n기존\n", encoding="utf-8")

    proposed = tmp_path / "wiki" / "투자" / "신규.md"

    monkeypatch.setattr(
        lb, "retrieve",
        lambda q, k=5: [{"file": similar_rel, "section": "x",
                         "content": "y", "_distance": 0.05}],
    )
    monkeypatch.setattr(
        lb, "_chat",
        lambda msgs, temperature=0.2: (_ for _ in ()).throw(RuntimeError("LLM down")),
    )

    refined = {"title": "신규", "summary": "x", "content": "y"}
    action, _, merged_text, debug = lb.decide_action(refined, proposed)
    assert action == "NEW"  # MERGE 실패 시 NEW로 fallback
    assert merged_text is None
    assert "merge_error" in debug


# ─── find_similar_note 단위 ─────────────────────────


def test_find_similar_note_handles_retrieve_exception(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("LanceDB 안 켜졌음")
    monkeypatch.setattr(lb, "retrieve", boom)
    out = lb.find_similar_note(
        {"title": "x", "summary": "", "content": ""}, tmp_path / "x.md"
    )
    assert out is None


def test_find_similar_note_empty_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(lb, "retrieve", lambda q, k=5: [])
    out = lb.find_similar_note(
        {"title": "x", "summary": "", "content": ""}, tmp_path / "x.md"
    )
    assert out is None
