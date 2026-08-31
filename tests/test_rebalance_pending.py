"""test_rebalance_pending.py — 리밸런싱 승인 대기·재알림 (hermetic).

2026-07 콴텍 리밸런싱이 통째로 사라진 사건에서 나왔다. 로그 실측:

    06-18 21:05 푸시 → 21:05 실행(8건)
    07-05 21:05 푸시 → **실행 없음. 거부도 없음**
    08-03 12:55 푸시 → 15:27 실행(8건)

플래그는 "푸시했다"만 기록했으므로 7월도 처리된 달처럼 보였고 다시 알리지
않았다. 그동안 자동청산은 계속 돌아 콴텍 슬롯 투입률이 11.5%가 됐다.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import rebalance_pending as rp

UID = 7004216259
JUL = "2026-07"


def _flag_pushed_on(day: date) -> dict:
    return rp.mark_pushed({}, JUL, UID, day)


# ─── 재알림 판정 ─────────────────────────────────────


def test_the_july_case_gets_a_second_notice():
    """이 케이스가 통과하면 재알림 로직이 있으나 마나다."""
    flag = _flag_pushed_on(date(2026, 7, 5))
    assert rp.needs_push(flag, JUL, UID, date(2026, 7, 5)) is False   # 같은 날엔 조용
    assert rp.needs_push(flag, JUL, UID, date(2026, 7, 8)) is True    # 3일 뒤 다시


def test_an_executed_month_is_never_renotified():
    flag = rp.mark_executed(_flag_pushed_on(date(2026, 7, 5)), JUL, UID)
    assert rp.needs_push(flag, JUL, UID, date(2026, 7, 31)) is False


def test_skipping_counts_as_a_decision():
    """'이번 달 건너뜀'을 누른 것도 사람이 판단한 것이다 — 다시 조르지 않는다."""
    flag = rp.mark_executed(_flag_pushed_on(date(2026, 7, 5)), JUL, UID)
    assert rp.needs_push(flag, JUL, UID, date(2026, 7, 20)) is False


def test_a_month_never_pushed_is_pushed():
    assert rp.needs_push({}, JUL, UID, date(2026, 7, 1)) is True


def test_nagging_stops_after_the_cap():
    """매일 조르면 소음이 되고, 소음은 안 보게 된다."""
    flag = {}
    day = date(2026, 7, 5)
    for _ in range(rp.MAX_RENOTIFY + 1):
        assert rp.needs_push(flag, JUL, UID, day) is True
        flag = rp.mark_pushed(flag, JUL, UID, day)
        day = date(day.year, day.month, day.day + rp.RENOTIFY_AFTER_DAYS)
    assert rp.needs_push(flag, JUL, UID, day) is False


def test_another_user_is_judged_separately():
    flag = rp.mark_executed(_flag_pushed_on(date(2026, 7, 5)), JUL, UID)
    assert rp.needs_push(flag, JUL, 999, date(2026, 7, 5)) is True


# ─── 옛 형식 ─────────────────────────────────────────


def test_the_old_flag_format_is_read_not_discarded():
    flag = rp.normalize_flag({"2026-06": [UID], "2026-07": [UID]})
    assert flag["2026-07"]["pushed"] == [UID]


def test_an_old_entry_does_not_claim_it_was_executed():
    """옛 기록은 '푸시됨'까지만 안다 — 실행됐다고 적으면 없는 사실이 생긴다."""
    flag = rp.normalize_flag({"2026-07": [UID]})
    assert flag["2026-07"]["executed"] == []
    assert rp.unexecuted_months(flag, UID) == ["2026-07"]


def test_a_new_format_entry_survives_normalization():
    raw = {"2026-08": {"pushed": [UID], "executed": [UID],
                       "pushes": {str(UID): ["2026-08-03"]}}}
    flag = rp.normalize_flag(raw)
    assert flag["2026-08"]["executed"] == [UID]
    assert rp.push_count(flag, "2026-08", UID) == 1


def test_garbage_entries_are_dropped_not_crashed():
    assert rp.normalize_flag({"2026-07": "이상한값"}) == {}


def test_unexecuted_months_lists_only_the_stuck_ones():
    flag = rp.normalize_flag({"2026-06": [UID], "2026-07": [UID], "2026-08": [UID]})
    flag = rp.mark_executed(flag, "2026-06", UID)
    flag = rp.mark_executed(flag, "2026-08", UID)
    assert rp.unexecuted_months(flag, UID) == ["2026-07"]


# ─── 승인 대기 영속화 ────────────────────────────────


class _Rec:
    def __init__(self, ticker, name, price):
        self.ticker = ticker
        self.name = name
        self.composite_score = 1.0
        self.raw_factors = {}
        self.z_factors = {}
        self.current_price = price


def test_pending_survives_a_restart(tmp_path: Path):
    """메모리에만 두면 재시작으로 사라진다 — 7월이 그렇게 넘어갔다."""
    p = tmp_path / "pending.json"
    assert rp.save_pending(p, UID, JUL, [_Rec("005930", "삼성전자", 251000.0)])
    got = rp.load_pending(p, UID, JUL)
    assert len(got) == 1 and got[0]["ticker"] == "005930"
    assert got[0]["current_price"] == 251000.0


def test_dict_recommendations_are_stored_too(tmp_path: Path):
    p = tmp_path / "pending.json"
    rp.save_pending(p, UID, JUL, [{"ticker": "000660", "name": "SK하이닉스"}])
    assert rp.load_pending(p, UID, JUL)[0]["name"] == "SK하이닉스"


def test_another_users_pending_is_not_returned(tmp_path: Path):
    p = tmp_path / "pending.json"
    rp.save_pending(p, UID, JUL, [{"ticker": "005930"}])
    assert rp.load_pending(p, 999, JUL) == []


def test_a_different_month_is_not_executed(tmp_path: Path):
    """지난달 추천을 이번 달에 실행하면 없는 판단으로 매수하게 된다."""
    p = tmp_path / "pending.json"
    rp.save_pending(p, UID, JUL, [{"ticker": "005930"}])
    assert rp.load_pending(p, UID, "2026-08") == []


def test_a_missing_file_is_empty_not_an_error(tmp_path: Path):
    assert rp.load_pending(tmp_path / "nope.json", UID, JUL) == []


def test_a_broken_file_is_empty_not_an_error(tmp_path: Path):
    p = tmp_path / "pending.json"
    p.write_text("{깨진", encoding="utf-8")
    assert rp.load_pending(p, UID, JUL) == []


def test_clearing_is_idempotent(tmp_path: Path):
    p = tmp_path / "pending.json"
    rp.save_pending(p, UID, JUL, [{"ticker": "005930"}])
    rp.clear_pending(p)
    rp.clear_pending(p)
    assert not p.exists()
