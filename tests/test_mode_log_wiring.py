"""test_mode_log_wiring.py — 기록이 실제로 남는가, 그리고 기록 실패가 답을 막지 않는가."""
from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BOT = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
ROUTER = (SCRIPTS / "router.py").read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def _func(src: str, name: str) -> str:
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} 없음")


def _returned_keys(src: str, func: str) -> set:
    """그 함수가 돌려주는 dict의 **키 이름들**.

    `_code_only`로는 볼 수 없다 — dict 키는 문자열 토큰이라 통째로 걸러진다.
    (처음에 그렇게 짜서 헛실패했다. 검사 도구도 대상에 맞춰야 한다.)
    """
    keys = set()
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.FunctionDef) and node.name == func):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                for k in sub.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
    return keys


def test_the_router_returns_the_pre_override_mode():
    """최종 mode만 돌려주면 안전망을 떼도 되는지 영원히 모른다."""
    body = _code_only(_func(ROUTER, "route"))
    assert "llm_mode" in body
    assert {"llm_mode", "overridden"} <= _returned_keys(ROUTER, "route")


def test_the_bot_records_every_routing_decision():
    body = _code_only(_func(BOT, "handle_text"))
    assert "mode_log" in body or "_ml" in body
    assert "MODE_LOG_FILE" in body


def test_a_recording_failure_does_not_block_the_answer():
    """기록은 부가 기능이다 — 여기서 예외가 나면 사용자가 답을 못 받는다."""
    body = _func(BOT, "handle_text")
    idx = body.find("mode_log")
    assert idx > 0
    window = body[max(0, idx - 400):idx + 500]
    assert "try:" in window and "except" in window


def test_the_report_command_is_registered():
    names = set()
    for node in ast.walk(ast.parse(BOT)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "CommandHandler" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Name)):
            names.add(node.args[1].id)
    assert "cmd_mode_report" in names


def test_the_log_is_private_not_shareable():
    """질문 원문이 들어간다 — 공유 캐시에 두면 안 된다.

    **정의를 찾아야 한다.** 첫 등장은 사용처일 수 있다(처음에 그래서 헛실패했다).
    """
    assign = [ln for ln in BOT.splitlines()
              if ln.startswith("MODE_LOG_FILE") and "=" in ln]
    assert assign, "MODE_LOG_FILE 정의를 찾지 못했다"
    assert "private_state_dir" in assign[0]
    assert "shareable" not in assign[0]
