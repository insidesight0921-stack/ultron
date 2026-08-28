"""IPO 주간 스캔의 침묵 실패 방지 — 소스 수준 배선 검증."""
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _job(src):
    i = src.index("async def ipo_weekly_scan_job")
    return src[i:src.index("\nasync def ", i + 10)]


def test_zero_collection_is_distinguished_from_no_a_grade():
    """둘 다 '알림 생략'으로 끝나던 탓에 데이터 소스 고장이 몇 달간 묻혔다."""
    body = _job((SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8"))
    assert "if not results:" in body
    assert body.index("if not results:") < body.index("hot = [r for r in results")


def test_zero_collection_notifies_after_a_streak():
    body = _job((SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8"))
    assert "_zero_weeks" in body and "streak >= 2" in body
    assert "데이터 소스 점검 필요" in body


def test_a_successful_collection_clears_the_streak():
    body = _job((SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8"))
    assert 'flag.pop("_zero_weeks", None)' in body


def test_no_a_grade_still_logs_what_was_found():
    """'후보는 있었는데 등급이 낮았다'와 '아무것도 못 받았다'는 다른 상황이다."""
    body = _job((SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8"))
    assert "등급: %s" in body
