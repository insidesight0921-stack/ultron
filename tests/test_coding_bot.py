"""
v3.10 coding_bot 단위 테스트.

- _call_qwen / _call_claude는 monkeypatch (외부 의존성 격리)
- _build_user_message: language + files 포맷
- ACTION_SYSTEM_PROMPTS 4개 다 있음
- run() 분기: action 검증, content 검증, mode fast/accurate
- 외부 호출 실패 시 fallback (accurate → qwen으로 안전 전환)
- 라우터 _validate_coding_args
- 시스템 프롬프트 size sanity
"""
from __future__ import annotations
from unittest.mock import patch

import pytest

import coding_bot as cb
import router


# ─── 기본 사양 ───────────────────────────────────────


def test_action_prompts_complete():
    for action in ("design", "code", "debug", "review"):
        assert action in cb.ACTION_SYSTEM_PROMPTS
        assert "한국어" in cb.ACTION_SYSTEM_PROMPTS[action]


def test_valid_actions_set():
    assert cb.VALID_ACTIONS == {"design", "code", "debug", "review"}


# ─── _build_user_message ─────────────────────────────


def test_build_user_message_simple():
    msg = cb._build_user_message("문제 설명", None, None)
    assert "문제 설명" in msg


def test_build_user_message_with_language():
    msg = cb._build_user_message("문제", "python", None)
    assert "언어: python" in msg
    assert "문제" in msg


def test_build_user_message_with_files():
    files = [
        {"name": "foo.py", "text": "def hello():\n    pass"},
        {"name": "bar.js", "text": "const x = 1;"},
    ]
    msg = cb._build_user_message("리뷰", None, files)
    assert "foo.py" in msg
    assert "bar.js" in msg
    assert "```python" in msg  # py 확장자 → python
    assert "```javascript" in msg
    assert "def hello()" in msg


def test_build_user_message_skips_empty_text_files():
    files = [
        {"name": "real.py", "text": "x = 1"},
        {"name": "empty.py", "text": ""},
    ]
    msg = cb._build_user_message("리뷰", None, files)
    assert "real.py" in msg
    assert "empty.py" not in msg


def test_build_user_message_unknown_extension_no_lang_hint():
    files = [{"name": "weird.xyz", "text": "abc"}]
    msg = cb._build_user_message("x", None, files)
    assert "weird.xyz" in msg
    # 코드블록은 있어야 (빈 lang hint)
    assert "```\nabc\n```" in msg


# ─── run() 분기 ──────────────────────────────────────


def test_run_unknown_action():
    msg, _ = cb.run("dance", "x")
    assert "❌" in msg
    assert "알 수 없는" in msg


def test_run_empty_content():
    msg, _ = cb.run("design", "")
    assert "❌" in msg


def test_run_fast_uses_qwen(monkeypatch):
    captured = {}
    def fake_qwen(messages, num_predict=-1, num_ctx=16384):
        captured["called"] = True
        captured["msgs"] = messages
        captured["num_predict"] = num_predict
        return "qwen 응답"
    monkeypatch.setattr(cb, "_call_qwen", fake_qwen)
    monkeypatch.setattr(cb, "_call_claude",
                        lambda m, system: pytest.fail("Claude 호출되면 안 됨"))

    msg, _ = cb.run("code", "피보나치 함수", mode="fast")
    assert captured.get("called")
    assert "qwen 응답" in msg
    assert "Qwen" in msg
    assert "[code]" in msg
    # fast → num_predict 짧게
    assert captured["num_predict"] == cb.QWEN_NUM_PREDICT_FAST


def test_run_accurate_uses_claude(monkeypatch):
    captured = {}
    def fake_claude(messages, system):
        captured["called"] = True
        captured["system"] = system
        return "claude 응답"
    monkeypatch.setattr(cb, "_call_claude", fake_claude)

    msg, _ = cb.run("debug", "TypeError 발생", mode="accurate")
    assert captured.get("called")
    assert "claude 응답" in msg
    assert "Claude" in msg
    # debug 시스템 프롬프트가 들어갔는지
    assert "디버깅 전문가" in captured["system"]


def test_run_accurate_fallback_to_qwen_on_no_api_key(monkeypatch):
    """ANTHROPIC_API_KEY 없으면 RuntimeError → qwen으로 자동 fallback."""
    def claude_no_key(messages, system):
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    monkeypatch.setattr(cb, "_call_claude", claude_no_key)
    monkeypatch.setattr(cb, "_call_qwen",
                        lambda m, num_predict=-1, num_ctx=16384: "qwen fallback 응답")

    msg, _ = cb.run("code", "함수 작성", mode="accurate")
    assert "qwen fallback 응답" in msg
    assert "fallback" in msg
    assert "ANTHROPIC_API_KEY" in msg


def test_run_both_fail(monkeypatch):
    monkeypatch.setattr(cb, "_call_claude",
                        lambda m, system: (_ for _ in ()).throw(RuntimeError("KEY 없음")))
    monkeypatch.setattr(cb, "_call_qwen",
                        lambda m, **kw: (_ for _ in ()).throw(RuntimeError("ollama down")))
    msg, _ = cb.run("code", "함수", mode="accurate")
    assert "❌" in msg
    assert "양쪽 실패" in msg


def test_run_unknown_mode_treated_as_accurate(monkeypatch):
    captured = {}
    def fake_claude(messages, system):
        captured["called"] = True
        return "ok"
    monkeypatch.setattr(cb, "_call_claude", fake_claude)
    monkeypatch.setattr(cb, "_call_qwen",
                        lambda m, **kw: pytest.fail("Qwen 호출되면 안 됨"))
    cb.run("code", "x", mode="ULTRA")
    assert captured.get("called")


def test_run_with_files_passed_to_message(monkeypatch):
    captured = {}
    def fake_claude(messages, system):
        captured["msgs"] = messages
        return "ok"
    monkeypatch.setattr(cb, "_call_claude", fake_claude)

    files = [{"name": "x.py", "text": "print('hi')"}]
    cb.run("review", "이거 어때", files=files, mode="accurate")
    user_msg = captured["msgs"][0]["content"]
    assert "x.py" in user_msg
    assert "print('hi')" in user_msg


def test_run_qwen_includes_system_in_messages(monkeypatch):
    """Qwen은 system을 별도 인자가 아니라 messages 첫 항목으로 받음."""
    captured = {}
    def fake_qwen(messages, **kw):
        captured["msgs"] = messages
        return "ok"
    monkeypatch.setattr(cb, "_call_qwen", fake_qwen)
    cb.run("design", "x", mode="fast")
    assert captured["msgs"][0]["role"] == "system"
    assert "아키텍트" in captured["msgs"][0]["content"]
    assert captured["msgs"][1]["role"] == "user"


def test_run_claude_takes_system_separately(monkeypatch):
    captured = {}
    def fake_claude(messages, system):
        captured["system"] = system
        captured["msgs"] = messages
        return "ok"
    monkeypatch.setattr(cb, "_call_claude", fake_claude)
    cb.run("review", "x", mode="accurate")
    assert "리뷰어" in captured["system"]
    # messages는 user만 (system 없음)
    assert all(m["role"] != "system" for m in captured["msgs"])


# ─── 라우터 _validate_coding_args ───────────────────


def test_validate_coding_args_minimal():
    v = router._validate_coding_args({"action": "code", "content": "함수 작성"})
    assert v == {"action": "code", "content": "함수 작성"}


def test_validate_coding_args_full():
    v = router._validate_coding_args({
        "action": "review", "content": "리뷰 요청", "language": "Python",
        "files": [{"name": "a.py", "text": "x = 1"}],
    })
    assert v["language"] == "python"  # lower
    assert v["files"] == [{"name": "a.py", "text": "x = 1"}]


def test_validate_coding_args_unknown_action():
    assert router._validate_coding_args({"action": "buy", "content": "x"}) is None


def test_validate_coding_args_no_content():
    assert router._validate_coding_args({"action": "design"}) is None
    assert router._validate_coding_args({"action": "design", "content": ""}) is None


def test_validate_coding_args_drops_empty_files():
    v = router._validate_coding_args({
        "action": "review", "content": "x",
        "files": [{"name": "empty.py", "text": ""},
                  {"name": "real.py", "text": "y"}],
    })
    assert len(v["files"]) == 1
    assert v["files"][0]["name"] == "real.py"


def test_validate_coding_args_files_not_list():
    v = router._validate_coding_args({
        "action": "code", "content": "x", "files": "not a list",
    })
    # files 무시 + action/content는 정상
    assert "files" not in v
    assert v["content"] == "x"


def test_validate_coding_args_strips_whitespace():
    v = router._validate_coding_args({
        "action": "  Debug  ", "content": "  TypeError  ", "language": "  Python  ",
    })
    assert v["action"] == "debug"
    assert v["content"] == "TypeError"
    assert v["language"] == "python"


def test_router_KNOWN_TOOLS_includes_coding():
    assert "coding_bot" in router.KNOWN_TOOLS


# ─── 라우터 시스템 프롬프트 sanity ──────────────────


def test_router_prompt_has_coding_section():
    sp = router._build_system_prompt()
    assert "coding_bot" in sp
    for action in ("design", "code", "debug", "review"):
        assert action in sp


def test_router_prompt_size_under_limit():
    """프롬프트가 너무 크면 26B context 부담. v3.41 기준 13000자 이내(news_bot 추가)."""
    sp = router._build_system_prompt()
    assert len(sp) < 13000, f"프롬프트 {len(sp)}자, 13000자 초과"
