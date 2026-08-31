"""test_equity_drift_wiring.py — 목표 비중 추종이 배선됐는지 (소스 검사, hermetic).

사용자 결정(2026-08-31): **알림만, 신규 매수만 차단 · 콴텍·키움만 ·
상향 보충은 다음 정기 리밸런싱에서만.** 이 세 가지가 코드에서 지켜지는지 본다.
"""
from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BOT = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")


def _func(name: str) -> str:
    tree = ast.parse(BOT)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(BOT, node) or ""
    raise AssertionError(f"{name} 없음")


def _code_only(src: str) -> str:
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


# ─── 무엇을 하지 않는가 ──────────────────────────────


def test_the_job_never_sells():
    """강제 매도는 되돌릴 수 없다 — 사람 판단에 남긴다."""
    code = _code_only(_func("equity_drift_job"))
    for forbidden in ("record_sell", "_execute_private_paper_write", "record_buy"):
        assert forbidden not in code, forbidden


def test_the_snapshot_never_writes():
    code = _code_only(_func("_equity_drift_snapshot"))
    for forbidden in ("record_sell", "record_buy", "_direct_paper_write"):
        assert forbidden not in code, forbidden


def test_only_quant_and_kium_are_tracked():
    """IPO 슬롯 현금은 청약 증거금이고, 마이퀀트는 살 종목이 없다."""
    import re
    m = re.search(r'EQUITY_DRIFT_SLOTS = \(([^)]*)\)', BOT)
    assert m and set(re.findall(r'"([^"]+)"', m.group(1))) == {"콴텍", "키움"}


# ─── 알림 소음 ───────────────────────────────────────


def test_the_alert_only_fires_on_a_state_change():
    """매일 같은 말을 보내면 사람은 그 알림을 안 보게 된다."""
    code = _code_only(_func("equity_drift_job"))
    assert "state_key" in code
    assert "previous" in code


def test_a_failed_state_write_is_reported_not_swallowed():
    """상태를 못 남기면 같은 알림이 반복된다 — 조용히 넘기면 원인을 못 찾는다."""
    body = _func("equity_drift_job")
    assert "상태 저장 실패" in body


# ─── 매수 차단 ───────────────────────────────────────


def test_both_buy_paths_check_the_over_target_block():
    assert BOT.count("_equity_block_line(") >= 3      # 정의 1 + 호출 2


def test_the_block_message_distinguishes_over_target_from_no_cash():
    """예산 0 → 수량 0 → '배정금액 부족'은 증상이지 원인이 아니다."""
    body = _func("_equity_block_line")
    assert "목표" in body and "신규 매수를 건너뜁니다" in body
    assert "기존 보유는 그대로" in body


def test_the_block_clears_new_names_but_not_positions():
    """초과 시 신규만 비운다 — 보유 청산 코드가 붙으면 안 된다."""
    code = _code_only(BOT)
    assert "new_results = [ ]" in code and "new_recs = [ ]" in code


# ─── 등록 ────────────────────────────────────────────


def test_the_job_is_registered():
    assert 'name="equity_drift"' in BOT
    assert "equity_drift_job," in BOT


def test_the_job_skips_weekends():
    assert "weekday ( ) >= 5" in _code_only(_func("equity_drift_job"))
