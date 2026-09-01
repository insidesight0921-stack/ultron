"""test_vkospi_review_wiring.py — 분기 재측정이 실제로 돌고, 승인이 실제로 반영되는가.

**규칙이 있는 것과 규칙이 도는 것은 다르다.** 2026-08-31에 `compute_weight_
recommendation`의 70% 권고가 매수 경로 어디에서도 읽히지 않은 채 넉 달을 보냈다.
여기서는 잡 등록·콜백 등록·검증 경로가 소스에 실제로 있는지를 본다(소스 검사).
"""
from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
BOT = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """주석·독스트링을 뺀 실행 코드만. 설명 문장에 걸려 통과하면 안 된다."""
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def _func(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} 없음")


BOT_CODE = _code_only(BOT)


def _repeating_job_names(src: str) -> set[str]:
    """`run_repeating(<함수>, ...)`의 첫 인자 이름들. 문자열 검색은 줄바꿈에
    걸려 헛통과·헛실패를 낸다 — AST로 본다."""
    names = set()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_repeating"
                and node.args and isinstance(node.args[0], ast.Name)):
            names.add(node.args[0].id)
    return names


def test_the_review_job_is_registered():
    """함수만 있고 등록이 없으면 영원히 안 돈다."""
    assert "vkospi_threshold_review_job" in _repeating_job_names(BOT)


def test_the_approval_callback_is_registered():
    """버튼만 만들고 핸들러를 안 붙이면 눌러도 아무 일도 안 난다."""
    assert "handle_vkospi_threshold_callback" in BOT_CODE
    assert "vkospi_th:" in BOT


def test_the_job_does_not_fetch_over_the_network():
    """수집까지 하면 네트워크 실패가 '재측정 안 함'으로 조용히 굳는다."""
    body = _code_only(_func(BOT, "_vkospi_review_snapshot"))
    assert "collect_market_index" not in body
    assert "requests" not in body


def test_a_kept_verdict_sends_nothing():
    """분기마다 '이상 없음'을 보내면 그 알림은 읽히지 않게 된다."""
    body = _func(BOT, "_vkospi_review_locked")
    idx = body.find('"keep"')
    assert idx > 0
    tail = body[idx:idx + 700]
    assert "send_message" not in tail


def test_approval_goes_through_validation_not_straight_to_the_ledger():
    """**버튼은 근거가 아니다** — approve()가 검증을 다시 돌린다."""
    body = _code_only(_func(BOT, "_vkospi_apply_locked"))
    assert "_vtr . approve" in body
    assert "ValueError" in body      # 거부를 잡아서 사용자에게 말한다


def test_a_rejected_approval_says_nothing_changed():
    body = _func(BOT, "_vkospi_apply_locked")
    assert "반영하지 않았습니다" in body


def test_a_corrupt_ledger_stops_the_job_instead_of_overwriting_it():
    """빈 원장으로 덮어쓰면 승인 이력이 사라지고 초기값으로 되돌아간다."""
    body = _func(BOT, "_vkospi_review_locked")
    idx = body.find("load_ledger")
    tail = body[idx:idx + 500]
    assert "return" in tail


def test_the_weight_rule_reads_the_ledger_not_only_the_constants():
    import kium_bot as kb

    assert hasattr(kb, "active_thresholds")
    src = _code_only(_func(
        (SCRIPTS / "kium_bot.py").read_text(encoding="utf-8"),
        "compute_weight_recommendation"))
    assert "active_thresholds ( )" in src


def test_the_wrappers_actually_take_the_lock_before_delegating():
    """2026-09-01 락 리팩터링: 본문이 _locked로 옮겨졌다 — 이 테스트들이 옛
    함수를 계속 보면 아무것도 안 지키게 되므로, 위임 구조 자체를 고정한다."""
    job = _code_only(_func(BOT, "vkospi_threshold_review_job"))
    assert "_VKOSPI_LEDGER_LOCK" in job and "_vkospi_review_locked" in job
    cb = _code_only(_func(BOT, "handle_vkospi_threshold_callback"))
    assert "_VKOSPI_LEDGER_LOCK" in cb and "_vkospi_apply_locked" in cb
