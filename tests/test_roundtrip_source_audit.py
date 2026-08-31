"""test_roundtrip_source_audit.py — 성과 집계가 원본 표본을 쓰지 못하게 한다.

라운드트립을 만드는 함수가 두 개다.

  `compute_roundtrips`        기록 그대로. **오늘의 실제 위험**을 보는 곳용
                              (슬롯 일일 손실 한도 — 가격이 틀렸어도 손실은 났다)
  `roundtrips_for_analysis`   품질 규칙 적용. **성과·전략 분석**용

**2026-08-31: 성과 경로 세 곳이 원본을 쓰고 있었다.** `deep_report`(+LLM 총평),
`paper_ui._get_myquant_tags`, `private_read_store.get_paper_myquant_tags`.

차이가 작지 않다.

    원본 79건   PF 0.76   누적 −5,923,267원
    필터 63건   PF 0.97   누적   −535,292원   (제외 16건 합계 −5,387,975원)

제외분이 손실의 90%다. 그 표본으로 LLM 총평을 만들면 **시장이 낸 손실이 아니라
가격 오류가 낸 손실을 놓고 전략을 진단한다.** 두 함수는 이름이 비슷하고 둘 다
정상 동작하므로, 잘못 쓰여도 결과가 그럴듯하게 나온다 — 사람 눈으로는 안 잡힌다.
그래서 목록으로 고정한다.
"""
from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# `compute_roundtrips`(원본)를 쓰는 것이 **맞는** 곳. 여기 없는데 원본을 쓰면 실패한다.
RAW_ALLOWED = {
    # 오늘의 실제 위험 — 가격이 틀렸든 아니든 그 손실로 한도가 차야 한다
    "slot_hard_stop.py",
    # 정의부와 그 자신을 호출하는 래퍼
    "trade_analytics.py",
}


def _calls_in(path: Path) -> set[str]:
    """파일 안에서 호출되는 이름(속성 호출 포함)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


def test_only_the_risk_paths_use_the_unfiltered_sample():
    offenders = sorted(
        p.name for p in SCRIPTS.glob("*.py")
        if p.name not in RAW_ALLOWED and "compute_roundtrips" in _calls_in(p))
    assert not offenders, (
        f"성과 집계가 원본 표본을 쓴다: {offenders}. "
        "품질 규칙을 적용하려면 `roundtrips_for_analysis`를 쓰고, "
        "정말 원본이 맞다면 이 테스트의 RAW_ALLOWED에 **이유와 함께** 추가하라")


def test_the_allowlist_entries_still_exist():
    """지워진 파일이 목록에 남아 있으면 관문이 조용히 헐거워진다."""
    for name in RAW_ALLOWED:
        assert (SCRIPTS / name).exists(), f"RAW_ALLOWED에 없는 파일: {name}"


def test_the_deep_report_passes_the_exclusion_count_to_the_llm():
    """제외 건수를 모르면 모델은 남은 표본에 대해 남의 손실을 진단한다."""
    src = (SCRIPTS / "trade_analytics.py").read_text(encoding="utf-8")
    body = src[src.index("def deep_report("):]
    body = body[:body.index("\ndef ", 10)]
    assert "roundtrips_for_analysis" in body
    assert "n_excluded=len(dropped)" in body


def test_the_llm_is_told_not_to_invent_numbers():
    """이 총평의 유일한 실패 모드는 없는 숫자를 만들어 내는 것이다."""
    import trade_analytics as ta
    rules = ta._LLM_RULES
    assert "만들어 내지 말" in rules and "자료 없음" in rules
    assert "추정하지" in rules


def test_the_prompt_carries_the_rules_and_the_exclusion_note():
    import trade_analytics as ta
    captured = {}

    def fake_llm(prompt: str) -> str:
        captured["p"] = prompt
        return "요약"

    rts = [{"pnl": 100.0, "cost": 1000.0, "ret": 0.1, "reason": "익절",
            "slot": "키움", "ticker": "005930", "name": "삼성전자",
            "buy_at": "2026-08-01 10:00:00", "sell_at": "2026-08-05 10:00:00",
            "hold_days": 4, "qty": 1, "buy_notes": None}]
    ta.llm_summary(rts, fake_llm, None, n_excluded=16)
    assert "만들어 내지 말" in captured["p"]
    assert "16건" in captured["p"]


def test_no_exclusion_means_no_misleading_note():
    """0건인데 '제외한 뒤의 수치'라고 적으면 없는 정제를 주장하게 된다."""
    import trade_analytics as ta
    captured = {}
    rts = [{"pnl": 100.0, "cost": 1000.0, "ret": 0.1, "reason": "익절",
            "slot": "키움", "ticker": "005930", "name": "삼성전자",
            "buy_at": "2026-08-01 10:00:00", "sell_at": "2026-08-05 10:00:00",
            "hold_days": 4, "qty": 1, "buy_notes": None}]
    ta.llm_summary(rts, lambda p: captured.setdefault("p", p) or "x", None)
    assert "제외한 뒤" not in captured["p"]
