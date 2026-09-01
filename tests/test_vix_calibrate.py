"""test_vix_calibrate.py — VIX 임계값도 교과서 상수다. 재기 전에 재는 도구를 잰다.

VKOSPI `<15`가 407일 중 0일 걸린 것과 **같은 출처의 값**이 여기 남아 있다
(≤18 / ≥28). 이 파일은 두 가지를 고정한다.

1. 재는 쪽이 **도는 쪽과 같은 부등호**를 쓰는가(≤ / ≥). 다르면 경계에서 갈린다.
2. "대용이었나"를 상관계수가 아니라 **판정 일치율**로 묻는가. 봇이 실제로
   쓰는 것은 세 글자다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import vix_calibrate as vx  # noqa: E402


def test_the_measurement_uses_the_same_operators_as_the_running_code():
    """도는 쪽은 `<=`인데 재는 쪽이 `<`면 경계 값에서 둘이 갈린다."""
    import proxy_indicators as pi

    calm, stress = pi.VIX_CALM, pi.VIX_STRESS
    # 경계값 자체가 판정에 포함되어야 한다.
    assert pi.vix_state(calm) == "risk_on"
    assert pi.vix_state(stress) == "risk_off"
    cov = vx.coverage([calm, stress], calm=calm, stress=stress)
    assert cov["risk_on"] == 1 and cov["risk_off"] == 1
    assert vx.state_of(calm, calm=calm, stress=stress) == "risk_on"
    assert vx.state_of(stress, calm=calm, stress=stress) == "risk_off"


def test_a_dead_branch_is_named():
    """이 프로젝트가 겪은 실패 그대로 — 한쪽이 0일이면 그 항은 없는 것과 같다."""
    cov = vx.coverage([30.0, 35.0, 40.0], calm=18.0, stress=28.0)
    assert cov["dead_on"] is True
    assert cov["constant_off"] is True
    assert cov["dead_off"] is False


def test_coverage_counts_the_neutral_band():
    cov = vx.coverage([10.0, 20.0, 30.0], calm=18.0, stress=28.0)
    assert (cov["risk_on"], cov["neutral"], cov["risk_off"]) == (1, 1, 1)


def test_an_empty_sample_gets_no_verdict():
    assert vx.coverage([], calm=18.0, stress=28.0) == {"n": 0}


# ─── 대용 검증 ───────────────────────────────────────


def test_opposite_verdicts_are_counted_separately_from_disagreement():
    """'하나는 중립'과 '정반대'는 다르다. 대용 지표라면 정반대가 0에 가까워야 한다."""
    vix = {"d1": 12.0, "d2": 20.0, "d3": 40.0}      # on / neutral / off
    vk = {"d1": 80.0, "d2": 40.0, "d3": 80.0}       # off / neutral / off
    agr = vx.proxy_agreement(vix, vk, vix_calm=18.0, vix_stress=28.0,
                             vk_low=20.7, vk_high=60.6)
    assert agr["n"] == 3
    assert agr["agree"] == 2                        # d2, d3
    assert agr["opposite"] == 1                     # d1만 정반대
    assert agr["opposite_pct"] == 33.3


def test_days_that_do_not_overlap_are_dropped_not_guessed():
    """겹치지 않는 날을 채워 넣으면 일치율이 조용히 부풀려진다."""
    agr = vx.proxy_agreement({"d1": 12.0}, {"d2": 80.0}, vix_calm=18.0,
                             vix_stress=28.0, vk_low=20.7, vk_high=60.6)
    assert agr == {"n": 0}


def test_correlation_needs_a_real_sample():
    assert vx.correlation([1.0, 2.0], [1.0, 2.0]) is None
    assert vx.correlation([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)
    assert vx.correlation([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_a_flat_series_has_no_correlation_rather_than_a_fake_one():
    """분모가 0이면 상관은 정의되지 않는다 — 0.0으로 내놓으면 거짓말이다."""
    assert vx.correlation([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) is None


def test_changes_are_available_for_the_level_trap():
    """둘 다 추세적으로 오르면 수준 상관은 높게 나온다 — 변화로도 봐야 한다."""
    assert vx.changes([1.0, 3.0, 6.0]) == [2.0, 3.0]
    up_a = [float(i) for i in range(20)]
    up_b = [float(i) * 2 for i in range(20)]
    assert vx.correlation(up_a, up_b) == pytest.approx(1.0)   # 수준은 완벽 상관
    # 변화까지 완벽 상관인 것은 이 인공 예시라서다. 실제 계열에서 둘이
    # 갈리는지를 보라는 것이 이 함수의 존재 이유다.
    assert len(vx.changes(up_a)) == 19


# ─── 보고 문구 ───────────────────────────────────────


def test_the_report_names_a_dead_branch():
    msg = vx.format_report([30.0, 35.0, 40.0] * 50, calm=18.0, stress=28.0)
    assert "죽은 가지" in msg


def test_the_proxy_report_leads_with_the_verdict_not_the_correlation():
    agr = vx.proxy_agreement({"d1": 12.0}, {"d1": 80.0}, vix_calm=18.0,
                             vix_stress=28.0, vk_low=20.7, vk_high=60.6)
    msg = vx.format_proxy(agr, level_corr=0.9, change_corr=0.1)
    assert msg.index("정반대 판정") < msg.index("수준 상관")


def test_an_empty_overlap_says_so_instead_of_printing_zeros():
    msg = vx.format_proxy({"n": 0}, level_corr=None, change_corr=None)
    assert "잴 수 없습니다" in msg


# ─── FRED 호출 형태 ──────────────────────────────────


def test_the_daily_series_asks_for_rows_not_months():
    """월간용 limit(months+3)을 일간 시리즈에 그대로 쓰면 24개월치가 27행으로
    조용히 잘린다 — 값은 멀쩡해 보이고 표본만 사라진다."""
    import inspect

    import quant_bot as qb

    sig = inspect.signature(qb._fetch_fred_series_raw)
    assert "limit" in sig.parameters
    src = inspect.getsource(vx._cli)
    assert "limit=args.days" in src
