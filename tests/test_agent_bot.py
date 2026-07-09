"""agent_bot 단위 테스트 — 루프·파싱·도구·샌드박스·step cap (mock LLM)."""
from __future__ import annotations
import json
import pytest

import agent_bot as ab


@pytest.fixture(autouse=True)
def _fresh_tools():
    ab.clear_tools()
    ab.register_builtin_tools()
    yield
    ab.clear_tools()


def _seq_llm(*responses):
    it = iter(responses)
    return lambda prompt: next(it)


# ─── JSON 파싱 ──────────────────────────────────────


def test_parse_clean_json():
    a = ab._parse_action('{"final":"답"}')
    assert a == {"final": "답"}


def test_parse_embedded_json():
    a = ab._parse_action('잡설 {"final":"답"} 뒤')
    assert a["final"] == "답"


def test_parse_garbage_none():
    assert ab._parse_action("no json here") is None


# ─── 루프 ───────────────────────────────────────────


def test_run_immediate_final():
    out = ab.run("아무거나", llm=_seq_llm('{"thought":"t","final":"끝났다"}'))
    assert out == "끝났다"


def test_run_tool_then_final():
    ab.register_tool(ab.Tool("echo", "에코", lambda args: f"에코:{args.get('x')}"))
    out = ab.run("x 에코해", llm=_seq_llm(
        '{"action":{"tool":"echo","args":{"x":"hi"}}}',
        '{"final":"완료"}',
    ))
    assert out == "완료"


def test_run_unknown_tool_recovers():
    out = ab.run("t", llm=_seq_llm(
        '{"action":{"tool":"없는도구","args":{}}}',
        '{"final":"복구됨"}',
    ))
    assert out == "복구됨"


def test_run_parse_fail_then_final():
    out = ab.run("t", llm=_seq_llm("쓰레기출력", '{"final":"ok"}'))
    assert out == "ok"


def test_run_step_cap():
    # 항상 도구만 호출 → step cap 도달
    ab.register_tool(ab.Tool("noop", "무동작", lambda args: "관찰값"))
    llm = lambda p: '{"action":{"tool":"noop","args":{}}}'
    out = ab.run("무한", max_steps=3, llm=llm)
    assert "최대 단계" in out and "관찰값" in out


def test_run_tool_exception_graceful():
    def boom(args):
        raise RuntimeError("터짐")
    ab.register_tool(ab.Tool("boom", "터지는도구", boom))
    out = ab.run("t", llm=_seq_llm(
        '{"action":{"tool":"boom","args":{}}}',
        '{"final":"그래도 끝"}',
    ))
    assert out == "그래도 끝"


def test_run_empty_task():
    assert "비어" in ab.run("")


def test_run_llm_failure():
    def boom(p):
        raise ConnectionError("no ollama")
    out = ab.run("t", llm=boom)
    assert "오류" in out


# ─── 샌드박스 안전 도구 ─────────────────────────────


def test_read_file_within_sandbox(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("안녕", encoding="utf-8")
    monkeypatch.setattr(ab, "ALLOWED_ROOTS", [tmp_path])
    assert ab._tool_read_file({"path": str(f)}) == "안녕"


def test_read_file_outside_sandbox_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "ALLOWED_ROOTS", [tmp_path])
    # /etc/passwd 같은 외부 경로 차단
    assert "오류" in ab._tool_read_file({"path": "/etc/hostname"})


def test_read_file_path_traversal_blocked(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("비밀", encoding="utf-8")
    monkeypatch.setattr(ab, "ALLOWED_ROOTS", [root])
    # root 기준 상위 탈출 시도
    assert "오류" in ab._tool_read_file({"path": "../secret.txt"})


def test_list_files_sandbox(tmp_path, monkeypatch):
    (tmp_path / "x.md").write_text("1", encoding="utf-8")
    (tmp_path / "y.md").write_text("2", encoding="utf-8")
    monkeypatch.setattr(ab, "ALLOWED_ROOTS", [tmp_path])
    out = ab._tool_list_files({"dir": str(tmp_path), "pattern": "*.md"})
    assert "x.md" in out and "y.md" in out


def test_list_files_outside_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "ALLOWED_ROOTS", [tmp_path])
    assert "오류" in ab._tool_list_files({"dir": "/etc"})


def test_register_and_list_tools():
    ab.register_tool(ab.Tool("custom", "설명", lambda a: "x"))
    names = [t.name for t in ab.list_tools()]
    assert "custom" in names and "read_file" in names


# ─── 복합 명령 감지 (자동 위임) ──────────────────────


def test_compound_true():
    assert ab.is_compound_command("오늘 신호 정리하고 IT 뉴스도 요약해줘")
    assert ab.is_compound_command("공모주 스캔하고 관련 뉴스 찾아줘")


def test_compound_false_single():
    assert not ab.is_compound_command("공모주 스캔해줘")
    assert not ab.is_compound_command("IT 뉴스 요약해줘")


def test_compound_false_question():
    assert not ab.is_compound_command("리스크 관리가 뭐야?")
    assert not ab.is_compound_command("안녕")


def test_compound_needs_conjunction():
    # 동사 2개여도 연결어 없으면 단일로 간주(보수적)
    assert not ab.is_compound_command("뉴스")
    assert not ab.is_compound_command("")


# ─── inbox 쓰기 도구 (쓰기는 inbox만) ────────────────


def test_write_inbox_creates_file(tmp_path, monkeypatch):
    inbox = tmp_path / "raw" / "inbox"
    monkeypatch.setattr(ab, "INBOX_DIR", inbox)
    out = ab._tool_write_inbox({"title": "투자 메모", "content": "삼성 실적 주목"})
    assert "저장됨" in out
    files = list(inbox.glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "삼성 실적 주목"
    assert "투자_메모" in files[0].name


def test_write_inbox_empty_content(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "INBOX_DIR", tmp_path / "inbox")
    assert "오류" in ab._tool_write_inbox({"content": "  "})


def test_write_inbox_sanitizes_title(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(ab, "INBOX_DIR", inbox)
    ab._tool_write_inbox({"title": "../../해킹/시도", "content": "x"})
    f = list(inbox.glob("*.md"))[0]
    # 파일은 반드시 inbox 안에만
    assert f.parent == inbox
    assert "/" not in f.name and ".." not in f.name


def test_register_inbox_tool():
    ab.clear_tools()
    ab.register_inbox_tool()
    assert "write_inbox" in [t.name for t in ab.list_tools()]
    ab.clear_tools()
    ab.register_builtin_tools()
