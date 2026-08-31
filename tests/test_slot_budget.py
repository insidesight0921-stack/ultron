"""test_slot_budget.py — 매수 예산 배분 (hermetic, 순수 함수만).

2026-08-31 실측에서 나왔다. 기존 배분식 `현금 ÷ 전체 스캔 종목수`에는
**목표 비중이라는 개념이 없다.** 보유/현금 구성에 따라 배치 후 주식 비중이
71%~100%를 떠다닌다. 이번 배치가 70% 근처(66.7%)에 떨어진 것은 우연이다.

`compute_weight_recommendation`의 주식 70% 권고는 매수 경로 어디에서도 읽지
않았다 — 출력 문구에만 쓰였다.
"""
from __future__ import annotations

import slot_budget as sb

# 2026-08-31 09:23 키움 배치 실측값
CASH = 25_036_937
HELD = 9_849_600
PRICES = [("000660", 1_618_000), ("402340", 1_004_000), ("034730", 534_000),
          ("005930", 251_000), ("047040", 18_650)]


# ─── 목표 비중 ───────────────────────────────────────


def test_the_real_batch_lands_on_the_target():
    """이 케이스가 70%에 안 맞으면 규칙을 연결한 의미가 없다."""
    r = sb.allocate(PRICES, CASH, HELD, equity_weight=0.70)
    after = (HELD + r["spent"]) / sb.slot_total(CASH, HELD)
    assert abs(after - 0.70) < 0.005, after


def test_the_old_formula_had_no_target_at_all():
    """보유가 없으면 기존식은 현금을 100% 털어 넣는다 — 70%와 무관하다."""
    old_per = 35_000_000 / 8
    assert old_per * 8 == 35_000_000          # 신규 8종목이면 현금 전액
    r = sb.allocate([(f"t{i}", 1_000) for i in range(8)], 35_000_000, 0,
                    equity_weight=0.70)
    assert abs(r["spent"] / 35_000_000 - 0.70) < 0.02


def test_holdings_are_subtracted_from_the_target():
    """이미 주식이 된 몫을 또 사면 목표를 넘는다."""
    plan = sb.budget_for_new(cash=30_000_000, holdings_value=20_000_000,
                             n_new=2, equity_weight=0.70)
    assert plan["target"] == 35_000_000.0
    assert plan["spendable"] == 15_000_000.0     # 35,000,000 − 20,000,000
    assert plan["per_name"] == 7_500_000.0


def test_holdings_over_target_means_no_new_buying():
    """이미 목표를 넘겼으면 더 사지 않는다(마이너스 예산이 나오면 안 된다)."""
    plan = sb.budget_for_new(cash=1_000_000, holdings_value=40_000_000,
                             n_new=3, equity_weight=0.70)
    assert plan["spendable"] == 0.0 and plan["per_name"] == 0.0


def test_a_target_below_one_hundred_can_never_run_out_of_cash():
    """불변식: 주식 목표가 100% 미만이면 현금이 모자랄 수 없다.

    남은 목표 = (현금+보유)×w − 보유 = 현금×w − 보유×(1−w) ≤ 현금×w.
    그래서 w < 1 이면 항상 현금 안에서 해결된다. 이걸 모르고 '현금 부족'
    분기를 만들면 **절대 실행되지 않는 코드**를 두게 된다(처음에 그랬다).
    """
    for cash, held in ((1_000_000, 0), (5_000_000, 20_000_000),
                       (100, 50_000_000), (25_036_937, 9_849_600)):
        plan = sb.budget_for_new(cash=cash, holdings_value=held,
                                 n_new=3, equity_weight=0.70)
        assert plan["capped_by"] != "cash"
        assert plan["spendable"] <= cash


def test_a_full_equity_target_does_hit_the_cash_ceiling():
    """w=1.0(현금 100% 소진)일 때만 현금이 상한이 된다. 빚내지 않는다."""
    plan = sb.budget_for_new(cash=1_000_000, holdings_value=0,
                             n_new=1, equity_weight=1.0)
    assert plan["capped_by"] == "cash"
    assert plan["spendable"] < 1_000_000        # 버퍼만큼 남긴다


def test_a_target_capped_plan_says_so():
    plan = sb.budget_for_new(cash=30_000_000, holdings_value=0,
                             n_new=3, equity_weight=0.70)
    assert plan["capped_by"] == "target"


def test_a_nonsense_weight_falls_back_to_the_default():
    """이상한 비중으로 매수 금액이 정해지면 원인을 찾기 어렵다."""
    for bad in (None, -0.5, 1.5, "70%", True):
        assert sb.equity_target(10_000_000, 0, bad) == 10_000_000 * sb.DEFAULT_EQUITY_WEIGHT


# ─── 2차 배분 ────────────────────────────────────────


def test_the_second_pass_recovers_the_rounding_loss():
    """SK하이닉스는 1주가 예산의 51.7% — 나머지가 그냥 남는다."""
    with_pass = sb.allocate(PRICES, CASH, HELD, equity_weight=0.70)
    without = sb.allocate(PRICES, CASH, HELD, equity_weight=0.70, second_pass=False)
    assert with_pass["spent"] > without["spent"]
    assert without["leftover"] > 2_000_000
    assert with_pass["leftover"] < without["leftover"]


def test_the_second_pass_fills_from_the_cheapest():
    """비싼 종목부터 담으면 한 종목이 남은 돈을 다 먹어 비중이 틀어진다."""
    r = sb.allocate([("비쌈", 900_000), ("쌈", 10_000)], 2_000_000, 0,
                    equity_weight=1.0, cash_buffer=0.0)
    # 1차: 각 100만 예산 → 비쌈 1주(90만), 쌈 100주(100만). 잔액 10만 → 쌈 10주 추가
    assert r["quantities"]["쌈"] > 100
    assert r["quantities"]["비쌈"] == 1


def test_the_second_pass_never_exceeds_the_budget():
    r = sb.allocate(PRICES, CASH, HELD, equity_weight=0.70)
    assert r["spent"] <= r["spendable"] + 1e-6
    assert r["leftover"] >= -1e-6


def test_leftover_smaller_than_the_cheapest_share_is_expected():
    r = sb.allocate([("a", 7_000), ("b", 11_000)], 1_000_000, 0,
                    equity_weight=1.0, cash_buffer=0.0)
    assert r["leftover"] < 7_000


# ─── 값이 없을 때 ────────────────────────────────────


def test_a_name_without_a_price_is_skipped_not_guessed():
    """시세를 모르는 종목에 예산을 배정하면 수량이 나오지 않는다."""
    r = sb.allocate([("a", 10_000), ("b", None), ("c", 0)], 1_000_000, 0)
    assert r["skipped_no_price"] == ["b", "c"]
    assert set(r["quantities"]) == {"a"}


def test_no_names_yields_no_budget():
    r = sb.allocate([], 10_000_000, 0)
    assert r["quantities"] == {} and r["spent"] == 0


def test_no_cash_yields_no_quantities():
    r = sb.allocate(PRICES, 0, HELD)
    assert all(q == 0 for q in r["quantities"].values())


def test_a_cash_buffer_is_left_behind():
    """딱 맞춰 쓰면 수수료·슬리피지에서 체결이 실패한다."""
    r = sb.allocate([("a", 1)], 1_000_000, 0, equity_weight=1.0)
    assert r["spent"] < 1_000_000


# ─── 표시 ────────────────────────────────────────────


def test_the_summary_states_the_execution_rate():
    text = sb.format_plan(sb.allocate(PRICES, CASH, HELD, equity_weight=0.70),
                          equity_weight=0.70)
    assert "집행" in text and "70%" in text


def test_the_summary_admits_a_cash_shortfall():
    r = sb.allocate([("a", 100)], 1_000, 50_000_000, equity_weight=0.70)
    assert "현금 부족" in sb.format_plan(r) or r["spendable"] == 0


# ─── 2차 배분의 쏠림 (2026-08-31) ────────────────────


def test_the_second_pass_does_not_dump_everything_into_one_name():
    """싼 종목에 몰아 담으면 그 종목만 예산의 배가 된다.

    첫 구현이 그랬다 — 잔액 2,592,350원을 최저가 종목(18,650원)에 다 넣어
    139주가 추가됐고 그 종목만 예산의 **188.8%**가 됐다. 정수 내림을 고치려다
    비중을 더 크게 망가뜨린 것이다.
    """
    r = sb.allocate(PRICES, CASH, HELD, equity_weight=0.70)
    per = r["per_name"]
    ratios = {t: r["quantities"][t] * p / per for t, p in PRICES}
    assert max(ratios.values()) < 1.5, ratios


def test_the_round_robin_spreads_the_leftover():
    """한 바퀴에 한 주씩 — 여러 종목이 조금씩 늘어야 한다."""
    base = sb.allocate(PRICES, CASH, HELD, equity_weight=0.70, second_pass=False)
    full = sb.allocate(PRICES, CASH, HELD, equity_weight=0.70)
    grew = [t for t, _ in PRICES
            if full["quantities"][t] > base["quantities"][t]]
    assert len(grew) >= 3, grew


def test_topup_is_pure_and_does_not_mutate_the_input():
    qty = {"a": 1}
    sb.second_pass_topup(qty, {"a": 100}, 1000)
    assert qty == {"a": 1}


def test_topup_stops_when_nothing_is_affordable():
    r = sb.second_pass_topup({"a": 1}, {"a": 1000}, 999)
    assert r["added"] == {} and r["leftover"] == 999


def test_topup_ignores_names_without_a_price():
    r = sb.second_pass_topup({"a": 1, "b": 0}, {"a": 100, "b": None}, 350)
    assert r["quantities"]["b"] == 0 and r["added"] == {"a": 3}


# ─── 보유 평가 ───────────────────────────────────────


def test_unpriced_holdings_fall_back_to_cost_not_zero():
    """0으로 치면 슬롯 총액이 작아져 목표가 줄고 결국 덜 사게 된다."""
    hv = sb.holdings_value([{"ticker": "a", "quantity": 2, "avg_price": 1000}], {})
    assert hv["value"] == 2000 and hv["unpriced"] == 1
    assert "취득원가" in hv["basis"]


def test_market_prices_win_when_available():
    hv = sb.holdings_value([{"ticker": "a", "quantity": 2, "avg_price": 1000}],
                           {"a": 1500})
    assert hv["value"] == 3000 and hv["priced"] == 1 and hv["unpriced"] == 0


def test_closed_positions_do_not_count():
    hv = sb.holdings_value([{"ticker": "a", "quantity": 0, "avg_price": 1000}], {})
    assert hv["value"] == 0
