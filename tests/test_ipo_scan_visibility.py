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
    assert body.index("if not results:") < body.index("ipo_diagnose_scan(")


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


# ─── 진단 함수 (2026-08-28) ──────────────────────────
#
# "수집 0건"이라고 판단하고 경고를 붙였는데, 실제로 맥에서 돌려 보니 수집은
# 되고 있었다(10건). 막힌 곳은 등급 산출이었다 — 10건 전원 [?] 산출불가.
# 두 실패는 고쳐야 할 곳이 다르므로 구분한다.

import ipo_bot

MIN = {"A++", "A+", "A"}


def _r(name, grade, **scores):
    row = {"corp_name": name, "grade": grade}
    row.update(scores)
    return row


def test_no_results_is_its_own_verdict():
    assert ipo_bot.diagnose_scan([], MIN)["verdict"] == "no_results"


def test_all_ungraded_is_not_the_same_as_no_a_grade():
    """전원 산출불가는 '매력 없음'이 아니라 '판단을 못 한 것'이다."""
    d = ipo_bot.diagnose_scan([_r("가", "?"), _r("나", "?")], MIN)
    assert d["verdict"] == "all_ungraded" and d["n_ungraded"] == 2


def test_graded_but_below_the_bar_is_normal_operation():
    d = ipo_bot.diagnose_scan([_r("가", "B"), _r("나", "C")], MIN)
    assert d["verdict"] == "no_hot" and d["grades"] == ["B", "C"]


def test_one_graded_candidate_stops_it_being_all_ungraded():
    """일부만 산출불가면 채점 자체는 돌고 있다 — 경고 대상이 아니다."""
    assert ipo_bot.diagnose_scan([_r("가", "?"), _r("나", "B")], MIN)["verdict"] == "no_hot"


def test_hot_wins_over_everything():
    d = ipo_bot.diagnose_scan([_r("가", "?"), _r("나", "A+")], MIN)
    assert d["verdict"] == "hot" and [r["corp_name"] for r in d["hot"]] == ["나"]


def test_a_missing_grade_key_counts_as_ungraded():
    assert ipo_bot.diagnose_scan([{"corp_name": "가"}], MIN)["verdict"] == "all_ungraded"


def test_missing_factors_names_what_to_fix():
    """'산출불가'만으로는 어느 파서를 고칠지 알 수 없다."""
    rows = [_r("가", "?", underwriter_score=15.0, band_score=None, demand_score=None,
               float_score=None, size_score=None)]
    gaps = ipo_bot.missing_factors(rows)
    assert "공모가밴드" in gaps and "주관사" not in gaps


def test_missing_factors_needs_every_row_to_be_empty():
    """한 종목만 비어 있는 것은 파서 고장이 아니라 그 종목의 사정이다."""
    rows = [_r("가", "?", band_score=None), _r("나", "B", band_score=12.0)]
    assert "공모가밴드" not in ipo_bot.missing_factors(rows)


def test_missing_factors_of_nothing_is_nothing():
    assert ipo_bot.missing_factors([]) == []


# ─── 배선 ────────────────────────────────────────────


def test_ungraded_streak_is_wired_into_the_weekly_job():
    body = _job((SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8"))
    assert "_ungraded_weeks" in body and "all_ungraded" in body


def test_ungraded_alert_says_it_is_not_a_lack_of_appeal():
    """'등급 없음'으로 읽히면 정상으로 오해한다."""
    body = _job((SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8"))
    assert "매력 없음이 아닙니다" in body


def test_a_graded_scan_clears_the_ungraded_streak():
    body = _job((SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8"))
    assert 'flag.pop("_ungraded_weeks", None)' in body
