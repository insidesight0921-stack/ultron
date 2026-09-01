"""test_handler_registration.py — main()은 테스트에서 한 번도 실행되지 않는다.

**2026-09-01 사고.** `/파라미터`를 CommandHandler로 등록했더니 봇이 부팅할 때마다
`ValueError: Command '파라미터' is not a valid bot command`로 죽었다. 전체
회귀 2,823건이 전부 통과한 상태였다 — `telegram` 모듈이 없는 환경이라
`main()`이 애초에 실행되지 않았기 때문이다.

**임포트 없이 검사한다.** 등록 코드를 AST로 읽어서 텔레그램이 거부할 이름과
존재하지 않는 콜백을 잡는다. 라이브러리를 못 부르는 곳에서도 도는 검사여야
이 사고가 다시 나지 않는다.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BOT_SRC = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
TREE = ast.parse(BOT_SRC)

# 텔레그램 규칙: 영문 소문자·숫자·밑줄, 1~32자.
VALID = re.compile(r"^[a-z0-9_]{1,32}$")


def _command_registrations() -> list[tuple[str, str]]:
    """[(명령어 이름, 콜백 이름)] — CommandHandler("x", fn) 형태를 모은다."""
    out = []
    for node in ast.walk(TREE):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "CommandHandler"):
            continue
        if not node.args:
            continue
        name = node.args[0]
        cb = node.args[1] if len(node.args) > 1 else None
        out.append((
            name.value if isinstance(name, ast.Constant) else "<동적>",
            cb.id if isinstance(cb, ast.Name) else "<동적>",
        ))
    return out


def _defined_functions() -> set:
    return {n.name for n in ast.walk(TREE)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_command_name_is_accepted_by_telegram():
    """한글·대문자·공백이 들어가면 봇이 **부팅하지 못한다.**"""
    bad = [n for n, _ in _command_registrations()
           if n != "<동적>" and not VALID.match(n)]
    assert not bad, f"텔레그램이 거부할 명령어: {bad}"


def test_every_command_callback_exists():
    """이름을 잘못 적으면 NameError로 부팅이 멈춘다."""
    defined = _defined_functions()
    missing = [(n, cb) for n, cb in _command_registrations()
               if cb != "<동적>" and cb not in defined]
    assert not missing, f"정의되지 않은 콜백: {missing}"


def test_no_command_is_registered_twice():
    """나중 등록이 조용히 무시되어 '왜 안 되지'가 된다."""
    names = [n for n, _ in _command_registrations() if n != "<동적>"]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"중복 등록: {dupes}"


def test_every_callback_query_pattern_compiles():
    """버튼 패턴이 깨지면 등록 시점에 죽는다."""
    for node in ast.walk(TREE):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "CallbackQueryHandler"):
            continue
        for kw in node.keywords:
            if kw.arg == "pattern" and isinstance(kw.value, ast.Constant):
                re.compile(kw.value.value)


def test_every_callback_query_handler_exists():
    defined = _defined_functions()
    missing = []
    for node in ast.walk(TREE):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "CallbackQueryHandler" and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id not in defined):
            missing.append(node.args[0].id)
    assert not missing, f"정의되지 않은 콜백: {missing}"


def test_every_repeating_job_exists():
    """등록만 하고 함수 이름이 틀리면 부팅 때 죽는다."""
    defined = _defined_functions()
    missing = []
    for node in ast.walk(TREE):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_repeating" and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id not in defined):
            missing.append(node.args[0].id)
    assert not missing, f"정의되지 않은 잡: {missing}"


def test_the_korean_alias_is_handled_in_text_not_as_a_command():
    """한글 명령은 CommandHandler로 못 받는다 — 텍스트 경로에 있어야 한다."""
    assert "파라미터" not in {n for n, _ in _command_registrations()}
    for node in ast.walk(TREE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_text":
            body = ast.get_source_segment(BOT_SRC, node) or ""
            assert "파라미터" in body
            break
    else:
        raise AssertionError("handle_text 없음")


# ─── Markdown이 밑줄을 먹는다 (2026-09-01) ──────────
#
# `/파라미터` 첫 출력에서 `risk_on`이 `riskon`으로 보이고 백틱이 그대로 찍혔다.
# Markdown이 밑줄 쌍을 이탤릭으로 해석하면서, 짝이 안 맞는 순간부터 뒤쪽 서식이
# 통째로 어긋난 것이다. **밑줄이 들어가는 내용은 Markdown으로 보내면 안 된다.**


def _sends_with_markdown(func_name: str) -> bool:
    for node in ast.walk(TREE):
        if not (isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
                and node.name == func_name):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            for kw in sub.keywords:
                if (kw.arg == "parse_mode" and isinstance(kw.value, ast.Constant)
                        and str(kw.value.value).lower().startswith("markdown")):
                    return True
        return False
    raise AssertionError(f"{func_name} 없음")


def test_the_parameter_listing_does_not_use_markdown():
    """파라미터 키·라벨에는 밑줄이 들어간다(emergency.kospi_drop, risk_on)."""
    assert _sends_with_markdown("cmd_params") is False


def test_the_parameter_approval_does_not_use_markdown():
    assert _sends_with_markdown("handle_param_callback") is False


def test_the_mode_report_does_not_use_markdown():
    """final_mode·llm_mode 등 밑줄이 든 말이 그대로 나간다."""
    assert _sends_with_markdown("cmd_mode_report") is False


def test_the_emergency_alert_does_not_use_markdown():
    assert _sends_with_markdown("emergency_job") is False
