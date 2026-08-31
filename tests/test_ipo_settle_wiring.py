"""test_ipo_settle_wiring.py — IPO 정산 배선 (소스 검사, hermetic).

**IPO는 등급 산출까지만 되고 그 뒤가 비어 있었다.** 상장일이 지나도 수익률이
채워지지 않아, 매력지수 등급이 맞았는지 확인할 수단이 없었다.
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


def _code(src: str) -> str:
    return " ".join(t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
                    if t.type not in (tokenize.COMMENT, tokenize.STRING))


def test_the_settle_job_is_registered():
    assert 'name="ipo_settle"' in BOT and "ipo_settle_job," in BOT


def test_something_finally_calls_ipo_close():
    """이 배선이 없어서 상장 결과가 한 번도 기록되지 않았다."""
    assert "ipo_close" in _code(_func("ipo_settle_job"))


def test_the_offer_price_is_recorded_so_the_return_can_be_computed():
    """`ipo_close`는 factors의 final_price로 수익률을 낸다 — 없으면 None이 된다."""
    body = _func("handle_ipo_paper_callback")
    assert '"final_price": r.get("final_price")' in body


def test_settlement_does_not_need_an_allocation():
    """배정을 몰라서 못 하는 일과, 배정 없이도 할 수 있는 일을 구분한다."""
    code = _code(_func("ipo_settle_job"))
    for token in ("estimate_allocation", "assumed_qty", "plan_subscription"):
        assert token not in code, token


def test_an_unresolved_ticker_does_not_close_the_record():
    """상장 직후엔 종목코드가 매핑에 없을 수 있다 — 다음 회차에 다시 시도한다."""
    body = _func("ipo_settle_job")
    assert "종목코드 미확인" in body and "continue" in body


def test_a_missing_price_does_not_close_the_record():
    """기준가를 모르는데 닫으면 수익률 0%인 가짜 기록이 남는다."""
    assert "기준가 미확보" in _func("ipo_settle_job")


def test_the_strategy_comes_from_the_wiki():
    assert "current_strategy" in _code(_func("ipo_settle_job"))


def test_the_ohlcv_loader_reads_the_high_too():
    """전략 B는 상장 당일 고가가 기준이라 종가만으로는 안 된다."""
    body = _func("_load_ohlcv_payload")
    assert "고가" in body


def test_the_alert_says_the_return_is_allocation_independent():
    """'수익률'만 보이면 배정받은 결과로 오해할 수 있다."""
    assert "배정수량과 무관한" in _func("ipo_settle_job")


# ─── 화면 배선 (탭 B 측정 기준) ──────────────────────

UI = (SCRIPTS / "paper_ui.py").read_text(encoding="utf-8")


def test_the_strategy_endpoint_exists():
    assert '@app.get("/api/ipo/strategy")' in UI


def test_the_screen_states_which_basis_the_returns_use():
    """전략 A(종가)와 B(고가)는 같은 종목에서도 수치가 크게 다르다.
    기준을 안 밝히면 서로 다른 기준의 수치가 한 표에 섞인다."""
    assert "ipo-strategy-note" in UI and "loadIpoStrategy" in UI
    assert "기록된 수익률은" in UI


def test_the_screen_warns_about_mixing_bases():
    assert "과거 수치와 섞이지 않도록" in UI


def test_a_failed_read_still_says_what_will_be_used():
    """읽기 실패를 침묵으로 두면 어떤 기준으로 측정 중인지 알 수 없다."""
    assert "기본값 A" in UI


def test_the_endpoint_reads_the_wiki_not_a_hardcoded_value():
    body = UI[UI.index('@app.get("/api/ipo/strategy")'):]
    body = body[:body.index("\n@app.")]
    assert "current_strategy" in body and "WIKI_RELATIVE" in body
