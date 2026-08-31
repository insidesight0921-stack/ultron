"""test_failure_tab_wiring.py — 탭 D가 화면까지 배선됐는지 (hermetic, 소스 검사).

수치를 잘 내도 화면이 **근거의 한계를 같이 싣지 않으면** 이 탭은 해롭다.
"원인 분석"이라는 이름 자체가 "원인을 찾았다"로 읽히기 때문이다.
"""
from __future__ import annotations

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _ui() -> str:
    return (SCRIPTS / "paper_ui.py").read_text(encoding="utf-8")


def test_the_endpoint_exists():
    assert '@app.get("/api/failure-analysis")' in _ui()


def test_the_tab_button_and_pane_exist():
    ui = _ui()
    assert 'data-tab="tab-failure"' in ui and 'id="tab-failure"' in ui
    assert "loadFailureAnalysis" in ui


def test_the_endpoint_uses_the_quality_filtered_sample():
    ui = _ui()
    body = ui[ui.index('@app.get("/api/failure-analysis")'):]
    body = body[:body.index("\ndef _hold_bucket")]
    assert "roundtrips_for_analysis" in body
    assert "compute_roundtrips" not in body


def test_the_llm_narrative_is_off_by_default():
    """총평이 없어도 수치는 나와야 한다 — 로컬 모델 호출은 수십 초 걸린다."""
    ui = _ui()
    assert "async def api_failure_analysis(llm: int = 0)" in ui


def test_the_screen_says_it_does_not_name_a_cause():
    ui = _ui()
    assert "원인을 지목해 주지 않습니다" in ui


def test_the_screen_shows_the_random_split_evidence():
    """'우연 범위'라는 판정만 두면 왜 그렇게 엄격한지 알 수 없다."""
    ui = _ui()
    assert "무작위로 섞었을 때와 구분되는지" in ui
    assert "50.0%" in ui


def test_the_screen_explains_why_the_control_axes_are_excluded():
    ui = _ui()
    assert "FAILURE_KIND_NOTE" in ui
    for kind in ("time:", "tauto:", "outcome:"):
        assert kind in ui


def test_the_screen_warns_that_llm_numbers_are_generated():
    """총평에 새 숫자가 보이면 그것은 계산된 값이 아니다."""
    ui = _ui()
    assert "위 표의 숫자만" in ui and "생성된 값" in ui


def test_the_hold_bucket_does_not_invent_zero_days():
    import sys
    sys.path.insert(0, str(SCRIPTS))
    import paper_ui
    assert paper_ui._hold_bucket({"hold_days": None}) == "미상"
    assert paper_ui._hold_bucket({"hold_days": 0}) == "1일 이하"
    assert paper_ui._hold_bucket({"hold_days": 21}) == "21일 이상"
