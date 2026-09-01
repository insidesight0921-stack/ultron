"""test_emergency_wiring.py — 긴급 규칙이 실제로 돌고, 실제로 매수를 막는가.

**규칙이 있는 것과 규칙이 도는 것은 다르다.** 이 프로젝트에서 70/30 목표가
넉 달 동안 출력 문구로만 존재했고, 오늘도 VKOSPI 예산 연결과 환율 변화율이
각각 "고쳤다고 말했지만 안 고친" 상태로 발견됐다.
"""
from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BOT = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")


def _code_only(src: str) -> str:
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


def _repeating_job_names(src: str) -> set:
    names = set()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_repeating"
                and node.args and isinstance(node.args[0], ast.Name)):
            names.add(node.args[0].id)
    return names


def test_the_job_is_registered():
    """함수만 있고 등록이 없으면 영원히 안 돈다."""
    assert "emergency_job" in _repeating_job_names(BOT)


def test_the_budget_path_actually_reads_the_emergency_state():
    """예산에 실리지 않으면 차단 문구가 영원히 뜨지 않는다."""
    body = _code_only(_func(BOT, "_equity_budget"))
    assert "_emergency_now" in body
    assert "emergency" in body


def test_new_buys_are_blocked_by_the_emergency_not_only_by_drift():
    body = _code_only(_func(BOT, "_equity_block_line"))
    assert "emergency" in body
    assert "blocks_new_buys" in body


def test_the_emergency_reason_comes_before_the_drift_reason():
    """둘 다 걸리면 사람이 취할 다음 행동이 다르다 — 급한 쪽을 먼저 부른다."""
    body = _func(BOT, "_equity_block_line")
    assert body.index("emergency") < body.index("drift =")


def test_the_check_does_not_touch_the_network():
    """매수 경로에서 불린다 — 외부가 느리면 매수가 같이 멈춘다."""
    body = _code_only(_func(BOT, "_emergency_now"))
    for forbidden in ("requests", "urlopen", "fetch_vkospi_latest",
                      "collect_market_index"):
        assert forbidden not in body, f"네트워크 호출 흔적: {forbidden}"


def test_a_failed_check_is_unknown_not_calm():
    """판정 실패를 '정상'으로 두면 수집이 멈춘 날 조용히 안전해진다.

    (2026-09-01 정비: 문자열 슬라이스를 tokenize하던 방식은 except 뒤에 코드가
    생기자 IndentationError로 깨졌다 — 검사도 AST로 한다.)
    """
    tree = ast.parse(BOT)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "_emergency_now":
            handlers = [h for sub in ast.walk(node)
                        for h in getattr(sub, "handlers", [])]
            assert handlers, "except 블록이 없다"
            for h in handlers:
                calls = [c for c in ast.walk(h) if isinstance(c, ast.Call)
                         and isinstance(c.func, ast.Attribute)
                         and c.func.attr == "assess"]
                if calls:
                    c = calls[0]
                    assert all(isinstance(a, ast.Constant) and a.value is None
                               for a in c.args), "실패 시 assess(None, None)이어야 한다"
                    return
    raise AssertionError("_emergency_now의 except에서 assess 호출을 못 찾았다")


def test_the_job_only_speaks_on_a_transition():
    """경보는 평균 13.7일 이어진다 — 매일 보내면 안 보게 된다."""
    body = _code_only(_func(BOT, "emergency_job"))
    assert "transition" in body
    assert "state_key" in body


def test_nothing_in_the_wiring_sells():
    body = _code_only(_func(BOT, "emergency_job")) + _code_only(_func(BOT, "_emergency_now"))
    for forbidden in ("execute_trade", "sell", "close_position"):
        assert forbidden not in body, f"매도 경로 흔적: {forbidden}"


# ─── 2026-09-01 리뷰에서 나온 배선 보강 ─────────────


def test_a_hold_keeps_the_previous_level_in_the_state_file():
    """hold에서 '정상'을 저장하면 다음 턴이 이미 해제된 걸로 안다."""
    body = _code_only(_func(BOT, "emergency_job"))
    assert "level_to_save" in body
    assert 'kind ==' in _func(BOT, "emergency_job")


def test_the_buy_path_consults_the_saved_state_during_an_outage():
    body = _code_only(_func(BOT, "_emergency_now"))
    assert "effective" in body
    assert "EMERGENCY_STATE_FILE" in body


def test_the_vkospi_ledger_writers_share_a_lock():
    """잡과 승인 콜백이 같은 원장을 읽고-고치고-쓴다 — 마지막 쓰기가 이기면
    승인 항목이 조용히 사라질 수 있다."""
    for fn in ("vkospi_threshold_review_job", "handle_vkospi_threshold_callback"):
        body = _code_only(_func(BOT, fn))
        assert "_VKOSPI_LEDGER_LOCK" in body, fn


# ─── 재확인에서 나온 것 (2026-09-01 2차) ────────────


def test_an_identical_observation_is_always_silent():
    """hold는 매 턴 kind가 나온다 — `kind is None` 게이트만으로는 수집이 끊긴
    동안 30분마다 「판단 보류」가 간다. 내가 1차 리뷰에서 넣은 코드가 만든
    버그이고, 상태 기계 시나리오 재생으로 잡았다."""
    body = _func(BOT, "emergency_job")
    code = _code_only(body)
    assert "key == previous_key" in code.replace("==", " == ").replace("  ", " ")
    # 게이트가 kind 조건과 결합돼 있으면 안 된다
    idx = code.find("key == previous_key")
    assert "kind is None and" not in code[max(0, idx - 60):idx]


def test_a_held_block_line_still_names_a_reason():
    """held 상태는 트리거가 비어 있다 — 사유 없는 차단 문구는 '왜 안 되지'를
    만든다."""
    body = _func(BOT, "_equity_block_line")
    assert "미확보" in body
