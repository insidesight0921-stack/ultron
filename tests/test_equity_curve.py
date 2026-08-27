"""test_equity_curve.py — 일간 자산곡선(순수) 검증 (hermetic)."""
from __future__ import annotations

import math

import pytest

import equity_curve as ec

CAL = ["20260601", "20260602", "20260603", "20260604", "20260605"]
SEEDS = {"키움": 10_000_000.0, "콴텍": 10_000_000.0}


def _t(side, day, qty=10, price=100_000, ticker="005930", slot="키움", fees=0, tid=1):
    return {"id": tid, "slot_name": slot, "ticker": ticker, "side": side,
            "quantity": qty, "price": price, "fees": fees,
            "executed_at": f"{day[:4]}-{day[4:6]}-{day[6:]} 10:00:00"}


# ─── 재생 ────────────────────────────────────────────


def test_replay_starts_at_the_first_trade_day():
    """거래 전 구간은 수익률 0인 날만 늘려 변동성을 낮추고 샤프를 부풀린다."""
    frames = ec.replay([_t("buy", "20260603")], CAL, SEEDS)
    assert [f["date"] for f in frames] == ["20260603", "20260604", "20260605"]


def test_buy_moves_cash_into_holdings():
    f = ec.replay([_t("buy", "20260601", qty=10, price=100_000, fees=300)],
                  CAL, SEEDS)[0]
    assert f["slots"]["키움"]["cash"] == 10_000_000 - 1_000_300
    assert f["slots"]["키움"]["holdings"] == {"005930": 10}


def test_sell_returns_cash_and_clears_position():
    trades = [_t("buy", "20260601", tid=1), _t("sell", "20260602", price=110_000, tid=2)]
    frames = ec.replay(trades, CAL, SEEDS)
    last = frames[1]["slots"]["키움"]
    assert last["holdings"] == {} and last["cash"] == 10_000_000 - 1_000_000 + 1_100_000


def test_partial_sell_keeps_the_remainder():
    trades = [_t("buy", "20260601", qty=10, tid=1), _t("sell", "20260602", qty=4, tid=2)]
    assert ec.replay(trades, CAL, SEEDS)[1]["slots"]["키움"]["holdings"] == {"005930": 6}


def test_slots_are_independent():
    trades = [_t("buy", "20260601", slot="키움", tid=1),
              _t("buy", "20260601", slot="콴텍", ticker="000660", tid=2)]
    f = ec.replay(trades, CAL, SEEDS)[0]
    assert f["slots"]["키움"]["holdings"] == {"005930": 10}
    assert f["slots"]["콴텍"]["holdings"] == {"000660": 10}


def test_replay_empty_is_safe():
    assert ec.replay([], CAL, SEEDS) == [] and ec.replay([_t("buy", "20260601")], [], SEEDS) == []


# ─── 평가 ────────────────────────────────────────────


def test_mark_adds_cash_and_market_value():
    state = {"cash": 1_000_000, "holdings": {"005930": 10}}
    assert ec.mark(state, {"005930": 120_000}) == (2_200_000, [])


def test_mark_refuses_to_guess_a_missing_price():
    """직전 값으로 메우면 변동성이 줄어 샤프가 부풀고 MDD가 얕아진다."""
    state = {"cash": 0, "holdings": {"005930": 10, "000660": 5}}
    value, missing = ec.mark(state, {"005930": 100_000})
    assert value is None and missing == ["000660"]


def test_mark_ignores_zero_quantity_holdings():
    state = {"cash": 100, "holdings": {"005930": 0}}
    assert ec.mark(state, {}) == (100, [])


# ─── 곡선 ────────────────────────────────────────────


def _series(prices, ticker="005930"):
    return {ticker: dict(zip(CAL, prices))}


def test_build_marks_every_day_when_prices_exist():
    curve = ec.build([_t("buy", "20260601")], CAL, SEEDS,
                     _series([100_000, 110_000, 120_000, 130_000, 140_000]))
    assert curve["n_days"] == 5 and curve["coverage"] == 1.0
    first, last = curve["days"][0], curve["days"][-1]
    assert first["total"] == 20_000_000                      # 시드 그대로
    assert last["total"] == 20_000_000 + 10 * 40_000         # +40,000 × 10주


def test_build_skips_days_without_prices():
    prices = {"005930": {"20260601": 100_000, "20260603": 120_000}}
    curve = ec.build([_t("buy", "20260601")], CAL, SEEDS, prices)
    assert [d["date"] for d in curve["days"]] == ["20260601", "20260603"]
    assert curve["missing_days"] == ["20260602", "20260604", "20260605"]
    assert curve["coverage"] == 0.4


def test_build_reports_which_ticker_is_missing():
    curve = ec.build([_t("buy", "20260601", ticker="999999")], CAL, SEEDS, {})
    assert curve["missing_tickers"] == {"999999": 5} and curve["n_days"] == 0


def test_cash_only_days_need_no_prices():
    """보유가 없으면 가격을 몰라도 자산을 셀 수 있다."""
    trades = [_t("buy", "20260601", tid=1), _t("sell", "20260601", tid=2)]
    curve = ec.build(trades, CAL, SEEDS, {})
    assert curve["n_days"] == 5


# ─── 지표 ────────────────────────────────────────────


def test_daily_returns_skips_zero_denominator():
    assert ec.daily_returns([100, 110]) == [pytest.approx(0.1)]
    assert ec.daily_returns([0, 110]) == []
    assert ec.daily_returns([100]) == []


def test_max_drawdown_sees_intramonth_decline():
    """보유 중 평가손실 — 거래 순서 곡선에서는 보이지 않던 값."""
    assert ec.max_drawdown([100, 120, 90, 130]) == 25.0
    assert ec.max_drawdown([100]) is None


def test_sharpe_annualizes_daily_returns():
    rets = [0.01, -0.005, 0.008, 0.002, -0.003]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    expected = round(mean / math.sqrt(var) * math.sqrt(252), 2)
    assert ec.sharpe(rets) == expected


def test_sharpe_subtracts_risk_free_when_given():
    rets = [0.01, -0.005, 0.008, 0.002, -0.003]
    assert ec.sharpe(rets, rf_annual=0.03) < ec.sharpe(rets)


def test_sharpe_none_for_flat_or_short_series():
    assert ec.sharpe([0.01, 0.01, 0.01]) is None      # 표준편차 0
    assert ec.sharpe([0.01]) is None


def test_total_return():
    assert ec.total_return([100, 150]) == 50.0
    assert ec.total_return([0, 150]) is None and ec.total_return([100]) is None


def test_beta_alpha_recovers_a_known_relationship():
    bench = [0.01 * (1 if i % 2 else -1) + 0.001 * i for i in range(40)]
    port = [2 * b for b in bench]                       # 베타 2, 알파 0
    beta, alpha = ec.beta_alpha(port, bench)
    assert beta == 2.0 and abs(alpha) < 1e-6


def test_beta_alpha_needs_enough_days():
    assert ec.beta_alpha([0.01] * 5, [0.01] * 5) == (None, None)


def test_align_matches_dates_not_positions():
    """날짜를 맞추지 않으면 휴장·결측 구간에서 서로 다른 날을 비교하게 된다."""
    days = [{"date": "20260601", "total": 100}, {"date": "20260603", "total": 120}]
    port, marks, dates = ec.align(days, {"20260601": 2000, "20260603": 2100})
    assert dates == ["20260601", "20260603"] and port == [100, 120] and marks == [2000, 2100]


def test_align_drops_days_the_benchmark_lacks():
    days = [{"date": "20260601", "total": 100}, {"date": "20260602", "total": 110}]
    port, marks, dates = ec.align(days, {"20260601": 2000})
    assert dates == ["20260601"] and len(port) == len(marks) == 1


# ─── 종합 판정 ───────────────────────────────────────


def _curve(n=40, growth=0.002):
    days = [{"date": f"d{i:03d}", "total": 100.0 * (1 + growth) ** i} for i in range(n)]
    return {"days": days, "n_days": n, "coverage": 1.0,
            "missing_days": [], "missing_tickers": {}}


def _bench(n=40, growth=0.001):
    return {f"d{i:03d}": 2000.0 * (1 + growth) ** i for i in range(n)}


def test_evaluate_produces_excess_return_and_beta():
    ev = ec.evaluate(_curve(), _bench())
    assert ev["usable"] and ev["n_days"] == 40
    assert ev["port_return"] > ev["bench_return"] > 0
    assert ev["excess_return"] == round(ev["port_return"] - ev["bench_return"], 2)


def test_evaluate_holds_when_too_few_days():
    ev = ec.evaluate(_curve(n=10), _bench(n=10))
    assert not ev["usable"] and "거래일" in ev["reasons"][0]


def test_evaluate_holds_when_coverage_is_thin():
    curve = _curve()
    curve["coverage"] = 0.4
    ev = ec.evaluate(curve, _bench())
    assert not ev["usable"] and any("커버리지" in r for r in ev["reasons"])


def test_evaluate_empty_is_safe():
    ev = ec.evaluate({"days": [], "n_days": 0, "coverage": None,
                      "missing_days": [], "missing_tickers": {}}, {})
    assert ev["n_days"] == 0 and ev["sharpe"] is None


def test_verdict_marks_unusable_as_undecided():
    """표본이 모자라면 '미충족'이 아니라 '판정 불가'다."""
    v = ec.verdict(ec.evaluate(_curve(n=10), _bench(n=10)))
    assert all(c["passed"] is None for c in v)


def test_verdict_checks_direction_per_metric():
    ev = ec.evaluate(_curve(), _bench())
    ev["sharpe"], ev["mdd"], ev["excess_return"] = 1.5, 10.0, 5.0
    assert all(c["passed"] for c in ec.verdict(ev))
    ev["mdd"] = 20.0                                    # MDD는 작아야 통과
    assert [c["passed"] for c in ec.verdict(ev)] == [True, False, True]


# ─── 표시 ────────────────────────────────────────────


def test_report_says_when_risk_free_was_not_subtracted():
    text = ec.format_report(ec.evaluate(_curve(), _bench()))
    assert "무위험수익률 미차감" in text


def test_report_names_skipped_days():
    curve = _curve()
    curve["missing_days"] = ["d100", "d101"]
    curve["missing_tickers"] = {"000660": 2}
    text = ec.format_report(ec.evaluate(curve, _bench()))
    assert "건너뛴 날 2일" in text and "직전 값으로 메우지 않았습니다" in text


def test_report_empty_state():
    ev = ec.evaluate({"days": [], "n_days": 0, "coverage": None,
                      "missing_days": [], "missing_tickers": {}}, {})
    assert "평가할 거래일이 없습니다" in ec.format_report(ev)
