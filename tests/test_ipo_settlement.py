"""test_ipo_settlement.py — IPO 청약·정산 (hermetic, 순수 함수만).

**IPO 슬롯 2,000만원이 개설(2026-05-10) 이후 한 번도 움직이지 않았다.**
`ipo_records`는 0행이고, 구독 버튼을 눌러도 기록만 남고 매매가 없었다.
그리고 **아무것도 `ipo_close`를 부르지 않아** 상장일이 지나도 수익률이 채워지지
않았다 — 매력지수 등급이 맞았는지 영원히 알 수 없는 상태였다.
"""
from __future__ import annotations

import ipo_settlement as s


# ─── 매도 전략 읽기 ──────────────────────────────────


def test_the_wiki_line_is_read():
    assert s.parse_strategy("# 제목\n현재 전략: B\n본문") == "B"


def test_a_decorated_value_still_reads():
    assert s.parse_strategy("현재 전략: A (보수적)") == "A"


def test_an_unknown_strategy_is_refused_not_guessed():
    """오타를 조용히 A로 처리하면 '설정했다고 믿는' 상태가 된다."""
    assert s.parse_strategy("현재 전략: Z") is None
    assert s.parse_strategy("전략 없음") is None


def test_a_missing_file_falls_back_to_the_safest(tmp_path):
    """못 읽었다고 매매를 멈추지 않는다 — 가장 보수적인 A로 판다."""
    assert s.current_strategy(tmp_path) == "A"


def test_the_wiki_is_actually_readable():
    """실제 vault의 파일이 읽히는지 — 배선이 끊기면 늘 A로 굳는다."""
    assert s.current_strategy() in s.VALID_STRATEGIES


def test_each_strategy_has_a_measurement_basis():
    for name in s.VALID_STRATEGIES:
        b = s.strategy_basis(name)
        assert b["price_field"] in ("close", "high", "close_d5")


# ─── 배정수량은 가정 없이 나오지 않는다 ──────────────


def test_allocation_is_not_invented():
    """**첫 구현이 여기서 틀렸다.**

    기관 수요예측 경쟁률로 비례배정을 계산했는데 ① 비례배정에 쓰는 것은
    일반청약 경쟁률이고 ② 개인 배정은 사실상 전부 균등배정에서 나온다
    (청약 500만원·경쟁률 1200:1이면 비례분은 0.13주다). 둘 다 우리가 수집하지
    않는 값이라, 어떤 숫자를 내든 그건 가정이다.
    """
    est = s.estimate_allocation(312)
    assert est["qty"] is None
    assert "계산할 수 없음" in est["basis"]


def test_an_explicit_assumption_is_labelled_as_one():
    est = s.estimate_allocation(312, assumed_qty=10)
    assert est["qty"] == 10 and est["assumed"] is True
    assert "가정" in est["basis"]


def test_an_assumption_cannot_exceed_the_subscription():
    assert s.estimate_allocation(5, assumed_qty=100)["qty"] == 5


def test_subscription_shares_need_a_confirmed_price():
    """밴드 상단으로 대신 계산하면 없는 사실이 생긴다."""
    assert s.subscription_shares(5_000_000, None) is None
    assert s.subscription_shares(5_000_000, 0) is None
    assert s.subscription_shares(5_000_000, 16_000) == 312


def test_a_grade_below_the_bar_is_skipped():
    plan = s.plan_subscription(alloc_amount=5_000_000, final_price=16_000,
                               grade="C", min_grade={"A", "A+"})
    assert plan["action"] == "skip" and "기준 미달" in plan["reason"]


def test_a_plan_without_an_assumption_still_subscribes_but_has_no_quantity():
    """청약 판단과 배정 추정은 다른 문제다 — 후자가 없다고 전자를 막지 않는다."""
    plan = s.plan_subscription(alloc_amount=5_000_000, final_price=16_000,
                               grade="A", min_grade={"A"})
    assert plan["action"] == "subscribe" and plan["est_qty"] is None


# ─── 상장일 정산 ─────────────────────────────────────


def test_a_missing_listing_price_does_not_settle():
    """공모가로 청산하면 수익 0%인 가짜 라운드트립이 남는다."""
    r = s.settle(est_qty=10, final_price=16_000, exit_price=None)
    assert r["ok"] is False and "대체하지 않는다" in r["reason"]


def test_a_normal_settlement_computes_the_return():
    r = s.settle(est_qty=10, final_price=16_000, exit_price=24_000)
    assert r["ok"] and r["ret_pct"] == 50.0 and r["pnl"] == 80_000


def test_settlement_needs_a_quantity():
    assert s.settle(est_qty=0, final_price=16_000, exit_price=24_000)["ok"] is False


# ─── 정산 대상 고르기 ────────────────────────────────


RECS = [
    {"name": "미정산", "listing_date": "20260820", "return_pct": None},
    {"name": "정산완료", "listing_date": "20260820", "return_pct": 12.3},
    {"name": "미래상장", "listing_date": "20260901", "return_pct": None},
    {"name": "상장일없음", "listing_date": None, "return_pct": None},
]


def test_only_unsettled_past_listings_are_due():
    names = [r["name"] for r in s.due_for_close(RECS, "20260831", "A")]
    assert names == ["미정산"]


def test_strategy_c_waits_five_trading_days():
    """상장 당일에 5일 종가를 쓸 수는 없다."""
    cal = ["20260820", "20260821", "20260824", "20260825", "20260826", "20260827"]
    assert s.due_for_close(RECS, "20260821", "C", cal) == []
    assert [r["name"] for r in s.due_for_close(RECS, "20260827", "C", cal)] == ["미정산"]


def test_a_broken_listing_date_is_skipped_not_crashed():
    bad = [{"name": "x", "listing_date": "2026-08-20", "return_pct": None}]
    assert s.due_for_close(bad, "20260831", "A") == []


# ─── 기준가 추출 ─────────────────────────────────────


PAYLOAD = {"date": ["20260820", "20260821", "20260824"],
           "close": [24_000, 25_000, 23_000],
           "high": [26_000, 25_500, 23_500]}


def test_strategy_a_uses_the_listing_day_close():
    r = s.exit_price_from_ohlcv(PAYLOAD, "20260820", "A")
    assert r["price"] == 24_000 and r["day"] == "20260820"


def test_strategy_b_uses_the_listing_day_high():
    assert s.exit_price_from_ohlcv(PAYLOAD, "20260820", "B")["price"] == 26_000


def test_strategy_c_uses_a_later_day():
    r = s.exit_price_from_ohlcv(PAYLOAD, "20260820", "C", wait=2)
    assert r["price"] == 23_000 and r["day"] == "20260824"


def test_a_listing_day_outside_the_cache_yields_nothing():
    assert s.exit_price_from_ohlcv(PAYLOAD, "20261231", "A")["price"] is None


def test_an_empty_cache_yields_nothing():
    assert s.exit_price_from_ohlcv({}, "20260820", "A")["price"] is None


def test_a_zero_price_is_not_used():
    payload = {"date": ["20260820"], "close": [0], "high": [0]}
    assert s.exit_price_from_ohlcv(payload, "20260820", "A")["price"] is None


def test_not_enough_days_for_strategy_c_yields_nothing():
    assert s.exit_price_from_ohlcv(PAYLOAD, "20260824", "C", wait=5)["price"] is None


# ─── 정산 무한 반복 차단 (2026-08-31 전수 점검에서 발견) ─


def test_a_priced_record_without_a_return_is_not_retried():
    """상장가는 있는데 수익률이 비면 재시도해도 결과가 같다.

    `ipo_close`가 확정 공모가를 못 찾으면 `return_pct`가 None으로 남는데,
    그 기록을 다시 정산 대상에 넣으면 **12시간마다 같은 정산·같은 알림을
    영원히 반복한다**(전수 점검에서 재현). 로그로 알리고 빼야 한다.
    """
    rec = {"name": "x", "listing_date": "20260820", "return_pct": None,
           "listing_price": 24_000.0}
    assert s.due_for_close([rec], "20260831", "A") == []


def test_an_unpriced_record_is_still_due():
    rec = {"name": "x", "listing_date": "20260820", "return_pct": None,
           "listing_price": None}
    assert [r["name"] for r in s.due_for_close([rec], "20260831", "A")] == ["x"]
