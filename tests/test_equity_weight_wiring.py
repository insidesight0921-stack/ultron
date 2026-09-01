"""test_equity_weight_wiring.py — 70/30 목표가 실제 매수 예산까지 가는지 (소스 검사).

**2026-08-31까지 `compute_weight_recommendation`의 주식 70% 권고는 매수 경로
어디에서도 읽히지 않았다.** `kium_bot`·`paper_ui`·`weekly_kium_scan`의 출력
문구에만 쓰였다. 규칙이 있는 것과 규칙이 도는 것은 다르다.
"""
from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BOT = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")


def _func(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} 없음")


def _code_only(src: str) -> str:
    """주석·독스트링을 뺀 실행 코드만.

    옛 배분식은 **왜 바꿨는지 설명하는 주석**에 그대로 적혀 있다. 문자열만
    찾으면 그 주석에 걸려 통과하지 못한다(처음에 그랬다).
    """
    import io
    import tokenize

    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


BOT_CODE = _code_only(BOT)


def test_the_buy_paths_use_the_budget_helper():
    """예전 식 `현금 / len(results)`가 남아 있으면 규칙이 우회된다."""
    assert BOT.count("_equity_budget(") >= 3          # 정의 1 + 호출 2
    assert "slot_cap / len ( results )" not in BOT_CODE
    assert "available_cap / len ( new_recs )" not in BOT_CODE


def test_the_weight_comes_from_the_recommendation_rule():
    body = _func(BOT, "_equity_weight_now")
    assert "compute_weight_recommendation" in body


def test_the_index_series_comes_from_the_indices_cache():
    """종목 일봉 캐시에는 지수가 없다 — 거기서 찾으면 늘 '데이터 부족'이 된다."""
    body = _func(BOT, "_equity_weight_now")
    assert "_kospi_series" in body
    assert 'load_series("1001"' not in body


def test_the_reason_is_carried_not_swallowed():
    """'판단해서 70%'와 '몰라서 70%'는 다르다."""
    body = _func(BOT, "_equity_weight_now")
    assert "reason" in body
    assert "권고 조회 실패" in body


def test_the_budget_helper_falls_back_instead_of_blocking_buys():
    """예산 산정이 깨졌다고 매수가 통째로 멈추면 더 나쁘다."""
    body = _func(BOT, "_equity_budget")
    assert "except Exception" in body and "옛 방식" in body


def test_the_second_pass_is_applied_in_both_paths():
    assert BOT.count("_apply_second_pass(") >= 3      # 정의 1 + 호출 2


def test_the_budget_log_uses_fstrings_not_percent_comma():
    """`%,.0f`는 printf 스타일에 없다 — 실행 시점에 메시지가 통째로 사라진다."""
    for name in ("_equity_budget", "_apply_second_pass"):
        body = _func(BOT, name)
        assert "%,.0f" not in body and "%,d" not in body


def test_the_weight_rule_still_returns_seventy_above_the_ma():
    import kium_bot as kb
    rising = list(range(1, 301))                      # 계속 오르는 지수
    assert kb.compute_weight_recommendation(kospi_close=rising)["equity_weight"] == 0.70


def test_the_weight_rule_drops_below_the_ma():
    import kium_bot as kb
    falling = list(range(300, 0, -1))
    assert kb.compute_weight_recommendation(kospi_close=falling)["equity_weight"] == 0.50


def test_a_short_series_is_not_judged():
    """200일이 안 되면 200일선을 낼 수 없다 — 판단했다고 하면 안 된다."""
    import kium_bot as kb
    rec = kb.compute_weight_recommendation(kospi_close=list(range(1, 50)))
    assert rec["kospi_above_ma"] is None
    assert "데이터 부족" in rec["reason"]


# ─── VKOSPI 항의 예산 연결 (v3.64) ──────────────────
#
# 임계값을 재기 전까지는 일부러 연결하지 않았다(a2fc820). 407일 분포를 재고
# `>60.6 / <20.7`로 바꾼 뒤에야 연결한다. 아래는 **연결이 풀리는 것**과
# **낡은 값이 흘러드는 것**을 둘 다 막는다.


def test_the_vkospi_term_reaches_the_budget():
    """출력 문구에만 쓰이던 항이 예산까지 오는지."""
    body = _func(BOT, "_equity_weight_now")
    code = _code_only(body)
    assert "_vkospi_latest" in code
    assert "vkospi = vk" in code.replace(" ,", ",")


def test_a_stale_vkospi_is_dropped_not_used():
    """수집이 멈추면 캐시는 '조용한 옛날 값'을 계속 준다 — 그게 더 위험하다."""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(SCRIPTS))
    import proxy_indicators as pi
    assert pi.VKOSPI_STALE_DAYS <= 7
    # 캐시가 있든 없든, 아주 먼 미래를 기준일로 주면 반드시 None이어야 한다.
    value, _ = pi._vkospi_latest(today="20990101")
    assert value is None


def test_a_missing_vkospi_is_named_in_the_reason():
    """'판단해서 70%'와 '몰라서 70%'는 다르다 — 근거 문구에 남아야 한다."""
    body = _func(BOT, "_equity_weight_now")
    assert "VKOSPI 미반영" in body
