"""test_idle_cash.py — 유휴 슬롯 자본 파킹 판단 (hermetic, 순수 함수만).

2026-08-29 실측: IPO 슬롯 2,000만원이 개설(2026-05-10) 이후 3.5개월간 거래
0건이었다. 슬롯 자본은 성과 집계에 잡히는데 수익은 0이라 전체 수익률을
구조적으로 끌어내린다.
"""
from __future__ import annotations

from datetime import date

import idle_cash as ic

TODAY = date(2026, 8, 29)


def _plan(**kw):
    base = dict(parked_qty=0, cash=20_000_000, subscriptions=[], today=TODAY,
                price=100_000)
    base.update(kw)
    return ic.plan_idle_action(**base)


# ─── 날짜 읽기 ───────────────────────────────────────


def test_a_yyyymmdd_string_is_read():
    assert ic._to_date("20260918") == date(2026, 9, 18)


def test_an_unreadable_date_is_none_not_today():
    """오늘로 대체하면 '청약이 임박했다'는 없는 사실이 생긴다."""
    for bad in ("", None, "2026-09-18", "abcdefgh", "20261332"):
        assert ic._to_date(bad) is None


def test_past_subscriptions_are_ignored():
    assert ic.next_subscription(["20260101", "20260918"], TODAY) == date(2026, 9, 18)


def test_the_nearest_one_wins():
    assert ic.next_subscription(["20260918", "20260905"], TODAY) == date(2026, 9, 5)


def test_no_upcoming_is_none():
    assert ic.next_subscription(["20260101"], TODAY) is None


# ─── 매수 ────────────────────────────────────────────


def test_no_upcoming_subscription_means_buy():
    plan = _plan()
    assert plan["action"] == "buy" and plan["qty"] == 200


def test_a_distant_subscription_still_allows_buying():
    plan = _plan(subscriptions=["20260930"])      # D-32
    assert plan["action"] == "buy"


def test_a_near_subscription_blocks_buying():
    plan = _plan(subscriptions=["20260905"])      # D-7 < 매수 임계 10
    assert plan["action"] == "hold" and "곧 필요한 자금" in plan["reason"]


def test_a_missing_price_blocks_buying():
    """매수는 fail-closed — 시세를 모르면 사지 않는다."""
    assert _plan(price=None)["action"] == "hold"
    assert _plan(price=0)["action"] == "hold"


def test_too_little_cash_is_not_parked():
    plan = _plan(cash=500_000)
    assert plan["action"] == "hold" and "최소" in plan["reason"]


def test_cash_below_one_share_is_not_parked():
    plan = _plan(cash=1_500_000, price=2_000_000)
    assert plan["action"] == "hold"


def test_an_unreadable_schedule_blocks_buying():
    """'청약이 없다'와 '일정을 확인하지 못했다'는 다르다."""
    plan = _plan(subscriptions=["20260930", "깨진값"])
    assert plan["action"] == "hold" and "읽지 못함" in plan["reason"]


# ─── 매도 ────────────────────────────────────────────


def test_an_imminent_subscription_sells():
    plan = _plan(parked_qty=200, subscriptions=["20260902"])   # D-4
    assert plan["action"] == "sell" and plan["qty"] == 200


def test_the_boundary_day_sells():
    plan = _plan(parked_qty=200, subscriptions=["20260903"])   # D-5 == 임계
    assert plan["action"] == "sell"


def test_a_distant_subscription_holds():
    plan = _plan(parked_qty=200, subscriptions=["20260918"])   # D-20
    assert plan["action"] == "hold"


def test_an_unreadable_schedule_sells_when_holding():
    """보유 중의 미확인은 위험이 한쪽이다 — 청약이 코앞일 수도 있다."""
    plan = _plan(parked_qty=200, subscriptions=["깨진값"])
    assert plan["action"] == "sell" and "보수적으로" in plan["reason"]


def test_selling_uses_the_whole_position():
    plan = _plan(parked_qty=137, subscriptions=["20260902"])
    assert plan["qty"] == 137


# ─── 채터링 ──────────────────────────────────────────


def test_the_buy_threshold_is_wider_than_the_sell_threshold():
    """같은 임계값을 쓰면 청약이 하나 잡힐 때마다 사고팔기를 반복한다."""
    assert ic.BUY_CLEAR_DAYS > ic.SELL_LEAD_DAYS


def test_the_gap_band_neither_buys_nor_sells():
    """매도 임계와 매수 임계 사이(D-6~D-10)에서는 상태를 유지한다."""
    for day in ("20260904", "20260908"):          # D-6, D-10
        assert _plan(parked_qty=0, subscriptions=[day])["action"] == "hold"
        assert _plan(parked_qty=200, subscriptions=[day])["action"] == "hold"


def test_a_buy_is_not_immediately_undone():
    """매수 직후 같은 일정으로 매도가 나오면 수수료만 나간다."""
    subs = ["20260930"]
    bought = _plan(subscriptions=subs)
    assert bought["action"] == "buy"
    assert _plan(parked_qty=bought["qty"], subscriptions=subs)["action"] == "hold"


# ─── 결제 지연 모사 ──────────────────────────────────


def test_the_lead_covers_t_plus_two_settlement():
    """paper_db는 매도 즉시 현금을 올려 주지만 실제 대금은 T+2에 들어온다.

    paper에서 D-1에 팔아도 되게 두면 **실전에서 증거금을 못 내는 전략이
    paper에서는 멀쩡해 보인다.**
    """
    assert ic.SELL_LEAD_DAYS >= 3


# ─── 표시 ────────────────────────────────────────────


def test_the_summary_names_the_action_and_reason():
    text = ic.format_plan(_plan(parked_qty=200, subscriptions=["20260902"]))
    assert "매도" in text and "200주" in text and "D-4" in text


def test_the_summary_of_a_hold_has_no_quantity():
    text = ic.format_plan(_plan(subscriptions=["20260905"]))
    assert "유지" in text and "주 ·" not in text


# ─── 배선 (소스 수준) ────────────────────────────────

from pathlib import Path  # noqa: E402

SRC = (Path(__file__).resolve().parents[1] / "scripts" / "telegram_bot.py").read_text(
    encoding="utf-8")


def _job_body():
    i = SRC.index("async def idle_cash_job")
    return SRC[i:SRC.index("\nasync def ", i + 10)]


def test_the_job_is_registered():
    assert "idle_cash_job," in SRC and 'name="idle_cash"' in SRC


def test_the_job_runs_only_during_market_hours():
    """장외에 매수하면 그 시각 '현재가'가 당일 종가다 — 살 수 없는 가격이다."""
    body = _job_body()
    assert "weekday() >= 5" in body and "hour=9, minute=30" in body


def test_the_schedule_lookup_skips_dart_enrichment():
    """필요한 것은 날짜뿐이다 — 종목당 문서 3건을 받을 이유가 없다."""
    body = _job_body()
    assert "fetch_ipo_schedule" in body and "ipo_scan" not in body


def test_execution_refuses_without_a_price():
    body = _job_body()
    assert "시세 미확보" in body


def test_the_direct_path_goes_through_the_write_lock():
    """Private write가 잠긴 상태에서 원본 DB에 직접 쓰면 안 된다."""
    body = _job_body()
    assert "_direct_paper_write" in body
    assert "_pdb.record_buy(" not in body and "_pdb.record_sell(" not in body


def test_the_writer_is_declared_in_the_inventory():
    """사람 승인 없이 도는 자동 경로는 목록에 명시한다."""
    inv = (Path(__file__).resolve().parents[1] / "scripts"
           / "paper_trade_identity.py").read_text(encoding="utf-8")
    assert 'key="telegram-idle-cash"' in inv
    assert 'function="idle_cash_job"' in inv
