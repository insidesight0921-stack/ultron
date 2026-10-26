from __future__ import annotations

import vol_target_backtest as vt
import weight_rule_backtest as wb


def test_realized_vol_does_not_see_the_same_day():
    """t일 값은 t일 수익률을 몰라야 한다 — 그날 폭락을 그날 비중이 알면 미래 참조."""
    rets = [None] + [0.01, -0.01] * 15
    base = vt.realized_vol(rets, window=4)
    crashed = list(rets)
    crashed[20] = -0.30
    after = vt.realized_vol(crashed, window=4)
    assert base[20] == after[20]                 # 그날까지는 같다
    assert after[21] > base[21]                  # 다음 날부터 반영


def test_realized_vol_is_none_until_the_window_fills():
    rets = [None, 0.01, 0.02, 0.03]
    assert vt.realized_vol(rets, window=3)[:4] == [None, None, None, None]


def test_expanding_median_uses_only_the_past():
    out = vt.expanding_median([1.0, 3.0, 2.0, 100.0, 5.0], min_history=3)
    assert out == [None, None, None, 2.0, 2.5]   # 100은 그 다음 날부터 들어간다


def test_weights_fall_when_volatility_rises_and_respect_the_clamp():
    calm = [0.002, -0.002] * 200
    wild = [0.05, -0.05] * 20
    rets = [None] + calm + wild
    w = vt.vol_target_weights(rets, [0.7] * len(rets), window=20, min_history=100)
    calm_w = [x for x in w[150:400] if x is not None]
    wild_w = [x for x in w[-10:] if x is not None]
    assert max(wild_w) < min(calm_w)
    assert min(wild_w) >= vt.CLAMP[0] and max(calm_w) <= vt.CLAMP[1]


def test_unknown_base_stays_unknown():
    rets = [None] + [0.01, -0.01] * 200
    base = [None] * len(rets)
    assert all(w is None for w in vt.vol_target_weights(rets, base, min_history=10))


def test_costs_are_charged_on_weight_changes_only():
    rets = [0.0, 0.0, 0.0]
    assert vt.net_returns(rets, [0.5, 0.5, 0.5]) == [0.0, 0.0, 0.0]
    got = vt.net_returns(rets, [0.5, 0.7, 0.7], cost=0.001)
    assert got[0] == 0.0 and abs(got[1] + 0.0002) < 1e-12 and got[2] == 0.0
    assert abs(vt.turnover([0.5, None, 0.7, 0.4]) - 0.5) < 1e-12


def test_verdict_needs_both_mdd_and_cost_adjusted_sharpe():
    rule = {"net_sharpe": 1.0}
    assert vt.verdict(rule, {"sharpe": 0.9}, {"percentile": 97.0}).startswith("우위(")
    assert vt.verdict(rule, {"sharpe": 1.1}, {"percentile": 97.0}).startswith("우위 없음")
    assert vt.verdict(rule, {"sharpe": 0.9}, {"percentile": 80.0}).startswith("우위 없음")
    assert vt.verdict(rule, {"sharpe": 0.9}, {"percentile": None}) == "판정 불가"


def test_a_rule_that_cuts_exposure_in_turbulent_regimes_beats_rotation():
    """양성 대조: 거친 구간이 뭉쳐 오고 규칙이 그걸 따라가면 MDD 회전 백분위가 높다."""
    import random
    rng = random.Random(1)
    # 국면 길이를 무작위로 둔다 — 주기가 있으면 그 주기만큼 민 회전이 정렬을 되살려
    # 귀무가 오염된다(처음 쓴 300일 주기 시장에서 백분위 88.7로 실측).
    rets, wild = [None], False
    while len(rets) < 1500:
        span = rng.randint(30, 90) if wild else rng.randint(120, 400)
        for _ in range(span):
            rets.append(rng.gauss(-0.002 if wild else 0.0005, 0.03 if wild else 0.006))
        wild = not wild
    w = vt.vol_target_weights(rets, [0.7] * len(rets), min_history=250)
    idx = vt.common_days(rets, w)
    r, ww = [rets[i] for i in idx], [w[i] for i in idx]
    t = wb.rotation_test(r, ww, wb._mdd_metric, larger_is_better=False)
    assert t["percentile"] >= vt.MDD_BAR


def test_a_constant_market_gives_no_edge_over_fixed():
    """음성 대조: 변동성이 늘 같으면 비중도 거의 고정 — 같은 평균 고정을 못 이긴다."""
    import random
    rng = random.Random(2)
    rets = [None] + [rng.gauss(0.0003, 0.01) for _ in range(1200)]
    w = vt.vol_target_weights(rets, [0.7] * len(rets), min_history=250)
    idx = vt.common_days(rets, w)
    r, ww = [rets[i] for i in idx], [w[i] for i in idx]
    t = wb.rotation_test(r, ww, wb._mdd_metric, larger_is_better=False)
    assert t["percentile"] < vt.MDD_BAR
