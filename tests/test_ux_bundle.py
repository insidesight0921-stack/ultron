"""
v3.8 운용·UX 묶음 테스트.

- inbox_bot.run() 정상/에러
- router._validate_inbox_args (10자 미만 거부 등)
- knowledge_bot.MODE_PARAMS 일관성 + run() mode 인자 전달
- 라우터 시스템 프롬프트의 4단계 트리거 알고리즘 포함 여부
"""
from __future__ import annotations
from pathlib import Path
import json

import pytest

import inbox_bot as ib
import knowledge_bot as kb
import router


# ─── inbox_bot ───────────────────────────────────────


def test_inbox_run_saves(monkeypatch, tmp_path):
    """save_to_inbox 호출 결과로 메시지에 경로 포함."""
    saved_paths = []

    def fake_save(content, hint, source):
        target = tmp_path / f"{hint}_test.md"
        target.write_text(content, encoding="utf-8")
        saved_paths.append((content, hint, source, target))
        return target

    monkeypatch.setattr(ib, "save_to_inbox", fake_save)
    monkeypatch.setattr(ib, "VAULT", tmp_path)

    msg, sources = ib.run("삼성전자 26.4Q 영업이익 컨센 상회", hint="투자_관찰", chat_id="111")
    assert "✅" in msg
    assert "투자_관찰" in msg
    assert sources == []
    # 호출 인자 검증
    assert len(saved_paths) == 1
    content, hint, source, _ = saved_paths[0]
    assert content == "삼성전자 26.4Q 영업이익 컨센 상회"
    assert hint == "투자_관찰"
    assert "chat_id=111" in source


def test_inbox_run_empty_content():
    msg, _ = ib.run("")
    assert "❌" in msg
    msg2, _ = ib.run("   ")
    assert "❌" in msg2


def test_inbox_run_default_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(ib, "save_to_inbox",
                        lambda c, h, s: tmp_path / f"{h}.md")
    monkeypatch.setattr(ib, "VAULT", tmp_path)
    # hint 없음 → "메모"로 default
    captured = {}
    def fake_save(c, h, s):
        captured["hint"] = h
        return tmp_path / f"{h}.md"
    monkeypatch.setattr(ib, "save_to_inbox", fake_save)
    ib.run("이건 그냥 자유 형식 메모입니다")
    assert captured["hint"] == "메모"


def test_inbox_run_save_failure(monkeypatch):
    def boom(c, h, s):
        raise OSError("디스크 가득 참")
    monkeypatch.setattr(ib, "save_to_inbox", boom)
    msg, _ = ib.run("저장될 내용 - 충분히 길게 적어둠")
    assert "❌" in msg
    assert "디스크" in msg


# ─── router._validate_inbox_args ────────────────────


def test_validate_inbox_args_normal():
    v = router._validate_inbox_args(
        {"content": "삼성전자 영업이익 컨센 상회 - 메모", "hint": "투자"}
    )
    assert v == {"content": "삼성전자 영업이익 컨센 상회 - 메모", "hint": "투자"}


def test_validate_inbox_args_no_hint():
    v = router._validate_inbox_args({"content": "충분히 긴 메모 내용입니다"})
    assert "content" in v
    assert "hint" not in v  # hint 없으면 누락


def test_validate_inbox_args_too_short():
    """10자 미만은 reject — 라우터 오분류 방지."""
    assert router._validate_inbox_args({"content": "짧음"}) is None
    assert router._validate_inbox_args({"content": "12345"}) is None


def test_validate_inbox_args_strips():
    v = router._validate_inbox_args(
        {"content": "  여백이 있는 충분히 긴 내용  ", "hint": "  투자  "}
    )
    assert v["content"] == "여백이 있는 충분히 긴 내용"
    assert v["hint"] == "투자"


def test_validate_inbox_args_missing_content():
    assert router._validate_inbox_args({"hint": "x"}) is None


def test_router_KNOWN_TOOLS_includes_inbox():
    assert "inbox_bot" in router.KNOWN_TOOLS


# ─── knowledge_bot mode ──────────────────────────────


def test_kb_mode_params_keys():
    assert "fast" in kb.MODE_PARAMS
    assert "accurate" in kb.MODE_PARAMS


def test_kb_mode_fast_smaller_than_accurate():
    fast = kb.MODE_PARAMS["fast"]
    acc = kb.MODE_PARAMS["accurate"]
    assert fast["k"] < acc["k"]
    assert fast["num_ctx"] <= acc["num_ctx"]


def test_kb_run_default_accurate_mode(monkeypatch):
    """mode 인자 없으면 accurate. retrieve를 가짜로 빈 리스트 반환하게 해서 빠르게 끝내기."""
    monkeypatch.setattr(kb, "retrieve", lambda q, k=6: [])
    msg, chunks = kb.run("아무 질문")
    assert "wiki에 관련 노트가 없" in msg
    # 기본 mode=accurate라 k=6이 들어가야 함 (k 인자 미명시 시)


def test_kb_run_explicit_fast_mode(monkeypatch):
    """fast 모드에서 retrieve가 받는 k는 3이어야 (mode 기본값)."""
    received_k = []
    monkeypatch.setattr(kb, "retrieve", lambda q, k=6: received_k.append(k) or [])
    kb.run("질문", mode="fast")
    assert received_k == [3]


def test_kb_run_explicit_accurate_mode(monkeypatch):
    received_k = []
    monkeypatch.setattr(kb, "retrieve", lambda q, k=6: received_k.append(k) or [])
    kb.run("질문", mode="accurate")
    assert received_k == [6]


def test_kb_run_unknown_mode_falls_back_to_accurate(monkeypatch):
    received_k = []
    monkeypatch.setattr(kb, "retrieve", lambda q, k=6: received_k.append(k) or [])
    kb.run("질문", mode="ULTRA")
    assert received_k == [6]  # unknown → accurate fallback


def test_kb_run_explicit_k_overrides_mode(monkeypatch):
    received_k = []
    monkeypatch.setattr(kb, "retrieve", lambda q, k=6: received_k.append(k) or [])
    kb.run("질문", k=10, mode="fast")
    assert received_k == [10]  # 명시적 k가 우선


# ─── 라우터 시스템 프롬프트: 4단계 트리거 ───────────


def test_router_prompt_has_4_levels():
    sp = router._build_system_prompt()
    for lvl in ("Level 1", "Level 2", "Level 3", "Level 4"):
        assert lvl in sp, f"{lvl} 누락"


def test_router_prompt_has_explicit_keywords():
    sp = router._build_system_prompt()
    # accurate 트리거
    for kw in ("정확하게", "자세히", "매매", "포지션", "리스크"):
        assert kw in sp, f"accurate 키워드 '{kw}' 누락"
    # fast 트리거
    for kw in ("빠르게", "간단히", "요약"):
        assert kw in sp, f"fast 키워드 '{kw}' 누락"


def test_router_prompt_inbox_bot_section():
    sp = router._build_system_prompt()
    assert "inbox_bot" in sp
    # 잘못된 예 가이드도 포함 (오분류 방지)
    assert "잘못된 예" in sp or "보수적 분기" in sp


def test_router_prompt_size_reasonable():
    """프롬프트가 너무 길면 26B context 부담. v3.23 기준 12000자 이내 (도구·기능 늘면 한도 갱신)."""
    sp = router._build_system_prompt()
    assert len(sp) < 12000, f"프롬프트 크기 {len(sp)}자, 12000자 초과"


# ─── coding_bot 분기 보강 (v3.10 후속, 텔레그램 실측 보정) ─────────


def test_router_prompt_has_coding_verbs():
    """판단 규칙 1번에 코딩 동사가 명시돼야 — '짜줘/만들어줘' 등이 wiki로 빠지는 오분류 방지."""
    sp = router._build_system_prompt()
    for verb in ("짜줘", "만들어줘", "구현해", "리팩토링", "스크립트"):
        assert verb in sp, f"코딩 동사 '{verb}' 누락 — 라우터가 코드 요청을 knowledge_bot으로 오분류 가능"


def test_router_prompt_has_coding_negative_example():
    """coding_bot 섹션에 '잘못된 예'(피보나치 짜줘 → knowledge_bot ❌)가 있어야."""
    sp = router._build_system_prompt()
    assert "잘못된 예" in sp
    assert "피보나치 짜줘" in sp
    # wiki에 알고리즘 노트 없음을 라우터가 인지해야
    assert "알고리즘" in sp or "wiki/에는" in sp


def test_router_prompt_coding_examples_include_simple_verbs():
    """§6 예시에 동사형 단순 입력이 포함돼야 (피보나치 짜줘, 정렬 함수 만들어줘 등)."""
    sp = router._build_system_prompt()
    # 동사형 예시
    assert "피보나치 N번째 빠르게 짜줘" in sp or "정렬 함수 만들어줘" in sp
    # 자료구조/언어 일반 알고리즘이 coding_bot 예시에 들어가야
    assert "이진 탐색" in sp or "URL 파싱" in sp or "스택 클래스" in sp


# ─── 텔레그램 코드 파일 첨부 (B-3, v3.13) ─────────────


def test_telegram_code_file_extensions_registered():
    """주요 코드 확장자가 ALLOWED_FILE_EXTS에 들어있어야."""
    pytest = __import__("pytest")
    try:
        import telegram_bot as tb
    except ImportError as e:
        pytest.skip(f"telegram_bot import 불가 (venv 의존성): {e}")

    # 신규 코드 카테고리
    must_have = {".py", ".js", ".ts", ".go", ".rs", ".java",
                 ".sh", ".sql", ".yaml", ".json", ".html"}
    assert must_have <= tb.CODE_FILE_EXTS, (
        f"코드 확장자 누락: {must_have - tb.CODE_FILE_EXTS}"
    )
    # 통합 ALLOWED_FILE_EXTS에도 합쳐져야
    assert must_have <= tb.ALLOWED_FILE_EXTS
    # 기존 텍스트/PDF는 그대로
    assert {".md", ".txt"} <= tb.TEXT_FILE_EXTS
    assert {".pdf"} <= tb.PDF_FILE_EXTS


def test_telegram_ext_to_lang_mapping():
    pytest = __import__("pytest")
    try:
        import telegram_bot as tb
    except ImportError as e:
        pytest.skip(f"telegram_bot import 불가: {e}")

    # 핵심 매핑
    assert tb._ext_to_lang(".py") == "python"
    assert tb._ext_to_lang(".js") == "javascript"
    assert tb._ext_to_lang(".ts") == "typescript"
    assert tb._ext_to_lang(".go") == "go"
    assert tb._ext_to_lang(".rs") == "rust"
    assert tb._ext_to_lang(".sh") == "bash"
    assert tb._ext_to_lang(".sql") == "sql"
    assert tb._ext_to_lang(".yaml") == "yaml"
    assert tb._ext_to_lang(".yml") == "yaml"  # alias
    assert tb._ext_to_lang(".json") == "json"
    # 알 수 없는 확장자 → 빈 문자열
    assert tb._ext_to_lang(".unknown") == ""
    assert tb._ext_to_lang("") == ""


def test_telegram_code_text_pdf_categories_are_disjoint():
    """카테고리는 서로 겹치지 않아야 (분기 일관성)."""
    pytest = __import__("pytest")
    try:
        import telegram_bot as tb
    except ImportError as e:
        pytest.skip(f"telegram_bot import 불가: {e}")
    assert tb.TEXT_FILE_EXTS.isdisjoint(tb.CODE_FILE_EXTS)
    assert tb.TEXT_FILE_EXTS.isdisjoint(tb.PDF_FILE_EXTS)
    assert tb.CODE_FILE_EXTS.isdisjoint(tb.PDF_FILE_EXTS)
