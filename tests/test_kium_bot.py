"""
kium_bot 단위 테스트 (v3.16).

- compute_momentum_score: 순수 계산 (pd.Series + list 둘 다)
- fetch_universe / scan_universe: pykrx HTTP는 monkeypatch
- format_scan_result: 출력 포맷
- run() entrypoint: scan / 잘못된 action / market 검증 / top_n 클램핑
- 디스크 캐시 roundtrip / TTL / corrupted fallback
- 라우터 _validate_kium_args
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
import pytest

import kium_bot as kb
from data_api_client import DataAPIUnavailable


@pytest.fixture(autouse=True)
def _clear_cache():
    kb._UNIVERSE_CACHE.clear()
    yield
    kb._UNIVERSE_CACHE.clear()


# ─── compute_momentum_score (순수 계산) ─────────────


def test_momentum_basic_positive():
    """가격이 252일 동안 정확히 +20% 상승 → score ≈ 0.20 (skip 0)."""
    # 길이 253(252+1) — 0~252. P[0]=100, 등비로 +20% to P[252]=120
    n = 253
    base = 100.0
    end = 120.0
    ratio = (end / base) ** (1 / (n - 1))
    prices = [base * (ratio ** i) for i in range(n)]
    score = kb.compute_momentum_score(prices, lookback_days=252, skip_days=0)
    assert score is not None
    assert abs(score - 0.20) < 0.005


def test_momentum_skip_excludes_recent_month():
    """최근 1개월(skip=21)을 제외하므로 그 구간 변동은 영향 없어야."""
    # 12개월 전(P[0]) = 100, 1개월 전(P[-22]) = 110, 현재(P[-1]) = 1000(노이즈)
    n = 253
    prices = [100.0] * n
    # 1개월 전 시점은 인덱스 -22 (n-22 = 231)
    prices[-22] = 110.0  # P[t-21-1]
    prices[-1] = 1000.0  # 최근 한 달은 무시되어야 함
    score = kb.compute_momentum_score(prices, lookback_days=252, skip_days=21)
    assert score is not None
    # 110/100 - 1 = 0.10
    assert abs(score - 0.10) < 0.005


def test_momentum_insufficient_data():
    """lookback+1 미만이면 None."""
    short = [100.0] * 100
    assert kb.compute_momentum_score(short, lookback_days=252) is None


def test_momentum_zero_or_negative_old_price():
    """과거 가격이 0/음수면 graceful None."""
    n = 253
    prices = [100.0] * n
    prices[0] = 0.0  # 과거 가격 0
    assert kb.compute_momentum_score(prices, lookback_days=252, skip_days=0) is None


def test_momentum_invalid_skip_lookback():
    """skip >= lookback이면 None."""
    prices = [100.0] * 300
    assert kb.compute_momentum_score(prices, lookback_days=21, skip_days=21) is None
    assert kb.compute_momentum_score(prices, lookback_days=21, skip_days=30) is None


def test_momentum_with_pandas_series():
    """pd.Series 입력도 동작."""
    n = 253
    prices = pd.Series([100.0 + i * 0.05 for i in range(n)])  # 단조 증가
    score = kb.compute_momentum_score(prices, lookback_days=252, skip_days=0)
    assert score is not None
    assert score > 0


def test_momentum_non_iterable_returns_none():
    assert kb.compute_momentum_score(None) is None
    assert kb.compute_momentum_score(42) is None


# ─── fetch_universe / 디스크 캐시 ──────────────────


def test_fetch_universe_uses_disk_cache(monkeypatch, tmp_path):
    """디스크 캐시에 같은 날짜 파일 있으면 fetch 안 부름."""
    monkeypatch.setattr(kb, "_DISK_CACHE_DIR", tmp_path)
    today = kb.datetime.now().strftime("%Y%m%d")
    p = tmp_path / f"universe_KOSPI200_{today}.json"
    p.write_text(json.dumps([["005930", "삼성전자"], ["000660", "SK하이닉스"]]),
                 encoding="utf-8")

    called = {"n": 0}
    def fake_fetch(market, date):
        called["n"] += 1
        return [("FAKE", "fake")]
    monkeypatch.setattr(kb, "_fetch_universe_raw", fake_fetch)

    lst = kb.fetch_universe("KOSPI200")
    assert called["n"] == 0  # 디스크 hit
    assert ("005930", "삼성전자") in lst


def test_fetch_universe_disk_ttl_expired_triggers_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(kb, "_DISK_CACHE_DIR", tmp_path)
    today = kb.datetime.now().strftime("%Y%m%d")
    p = tmp_path / f"universe_KOSPI200_{today}.json"
    p.write_text(json.dumps([["005930", "삼성전자"]]), encoding="utf-8")
    # 25시간 전 mtime
    old = time.time() - 25 * 3600
    os.utime(p, (old, old))

    called = {"n": 0}
    def fake_fetch(market, date):
        called["n"] += 1
        return [("000660", "SK하이닉스")]
    monkeypatch.setattr(kb, "_fetch_universe_raw", fake_fetch)

    lst = kb.fetch_universe("KOSPI200")
    assert called["n"] == 1  # 만료라 fetch
    assert ("000660", "SK하이닉스") in lst


def test_fetch_universe_force_refresh_ignores_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(kb, "_DISK_CACHE_DIR", tmp_path)
    today = kb.datetime.now().strftime("%Y%m%d")
    p = tmp_path / f"universe_KOSPI200_{today}.json"
    p.write_text(json.dumps([["005930", "삼성전자"]]), encoding="utf-8")

    monkeypatch.setattr(kb, "_fetch_universe_raw", lambda m, d: [("999999", "테스트")])
    lst = kb.fetch_universe("KOSPI200", force_refresh=True)
    assert lst == [("999999", "테스트")]


def test_fetch_universe_in_memory_cache_hit(monkeypatch, tmp_path):
    monkeypatch.setattr(kb, "_DISK_CACHE_DIR", tmp_path)
    today = kb.datetime.now().strftime("%Y%m%d")
    kb._UNIVERSE_CACHE[f"KOSPI200_{today}"] = (
        time.time(), [("005930", "삼성전자")]
    )

    def fake_fetch(market, date):
        raise AssertionError("in-memory hit이면 fetch 호출 금지")
    monkeypatch.setattr(kb, "_fetch_universe_raw", fake_fetch)

    lst = kb.fetch_universe("KOSPI200")
    assert lst == [("005930", "삼성전자")]


def test_fetch_universe_corrupted_disk_fallsback_to_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(kb, "_DISK_CACHE_DIR", tmp_path)
    today = kb.datetime.now().strftime("%Y%m%d")
    p = tmp_path / f"universe_KOSPI200_{today}.json"
    p.write_text("not-json", encoding="utf-8")

    monkeypatch.setattr(kb, "_fetch_universe_raw", lambda m, d: [("005930", "삼성전자")])
    lst = kb.fetch_universe("KOSPI200")
    assert lst == [("005930", "삼성전자")]


def test_fetch_universe_empty_response_skips_cache(monkeypatch, tmp_path):
    """v3.21 — 휴장일 등으로 빈 list가 fetch되면 in-memory + 디스크 둘 다 캐시 skip.
    다음 호출에서 다시 fetch 트리거되어야 24h stale 빈 응답 함정 차단."""
    monkeypatch.setattr(kb, "_DISK_CACHE_DIR", tmp_path)
    monkeypatch.setattr(kb, "_UNIVERSE_CACHE", {})

    call_count = {"n": 0}
    def fake_fetch(m, d):
        call_count["n"] += 1
        return []  # 휴장일 빈 응답 시뮬레이션
    monkeypatch.setattr(kb, "_fetch_universe_raw", fake_fetch)

    # 1차 호출 — 빈 list 반환되지만 캐시 안 됨
    lst1 = kb.fetch_universe("KOSPI200")
    assert lst1 == []
    assert call_count["n"] == 1

    # 디스크 캐시 파일 생성되지 않아야 함
    today = kb.datetime.now().strftime("%Y%m%d")
    cache_file = tmp_path / f"universe_KOSPI200_{today}.json"
    assert not cache_file.exists(), "빈 응답이 디스크에 저장되면 안 됨"

    # in-memory 캐시도 비어있어야 함
    assert not kb._UNIVERSE_CACHE, "빈 응답이 in-memory에 저장되면 안 됨"

    # 2차 호출 — 다시 fetch 호출되어야 함 (캐시 hit 안 됨)
    lst2 = kb.fetch_universe("KOSPI200")
    assert lst2 == []
    assert call_count["n"] == 2, "빈 캐시 hit으로 fetch가 skip되면 안 됨"


def test_save_disk_cache_skips_empty_list(monkeypatch, tmp_path):
    """v3.21 — _save_disk_cache 직접 호출도 빈 list 가드 통과해야 함."""
    monkeypatch.setattr(kb, "_DISK_CACHE_DIR", tmp_path)
    today = kb.datetime.now().strftime("%Y%m%d")

    # 빈 list 저장 시도
    kb._save_disk_cache("KOSPI200", today, [])

    cache_file = tmp_path / f"universe_KOSPI200_{today}.json"
    assert not cache_file.exists()

    # 정상 list는 저장됨 (BC 검증)
    kb._save_disk_cache("KOSPI200", today, [("005930", "삼성전자")])
    assert cache_file.exists()


def test_fetch_universe_api_first_for_current_date(monkeypatch, tmp_path):
    today = kb.datetime.now().strftime("%Y%m%d")
    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(kb, "_DISK_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        kb._DATA_API_CLIENT,
        "latest_universe",
        lambda market: {
            "market": market,
            "as_of": today,
            "instruments": [{"ticker": "005930", "name": "삼성전자"}],
        },
    )
    monkeypatch.setattr(
        kb,
        "_load_disk_cache",
        lambda *args: (_ for _ in ()).throw(AssertionError("disk fallback used")),
    )
    assert kb.fetch_universe("KOSPI200") == [("005930", "삼성전자")]


def test_fetch_universe_api_stale_falls_back_to_disk(monkeypatch):
    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(
        kb._DATA_API_CLIENT,
        "latest_universe",
        lambda market: {
            "market": market,
            "as_of": "20000101",
            "instruments": [{"ticker": "005930", "name": "삼성전자"}],
        },
    )
    monkeypatch.setattr(
        kb, "_load_disk_cache", lambda market, date: [("000660", "SK하이닉스")]
    )
    assert kb.fetch_universe("KOSPI200") == [("000660", "SK하이닉스")]


def test_fetch_universe_api_failure_falls_back_to_disk(monkeypatch):
    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(
        kb._DATA_API_CLIENT,
        "latest_universe",
        lambda market: (_ for _ in ()).throw(DataAPIUnavailable("down")),
    )
    monkeypatch.setattr(
        kb, "_load_disk_cache", lambda market, date: [("000660", "SK하이닉스")]
    )
    assert kb.fetch_universe("KOSPI200") == [("000660", "SK하이닉스")]


def test_fetch_universe_refresh_failure_uses_last_api_snapshot(monkeypatch):
    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(
        kb._DATA_API_CLIENT,
        "latest_universe",
        lambda market: {
            "market": market,
            "as_of": "20260803",
            "instruments": [{"ticker": "005930", "name": "삼성전자"}],
        },
    )
    monkeypatch.setattr(kb, "_load_disk_cache", lambda market, date: None)
    monkeypatch.setattr(
        kb,
        "_fetch_universe_raw",
        lambda market, date: (_ for _ in ()).throw(RuntimeError("KRX auth required")),
    )
    assert kb.fetch_universe("KOSPI200") == [("005930", "삼성전자")]


# ─── scan_universe ──────────────────────────────────


def _make_price_df(growth_pct: float, n: int = 253):
    """상승률 growth_pct(%)인 등비 시계열 DataFrame."""
    end = 100.0 * (1 + growth_pct / 100)
    ratio = (end / 100.0) ** (1 / (n - 1))
    closes = [100.0 * (ratio ** i) for i in range(n)]
    return pd.DataFrame({"종가": closes})


def test_load_ohlcv_api_first_and_fallback(monkeypatch):
    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(
        kb._DATA_API_CLIENT,
        "latest_ohlcv",
        lambda ticker: {
            "ticker": ticker,
            "as_of": "20260822",
            "series": {"close": [100, 110]},
        },
    )
    monkeypatch.setattr(
        kb,
        "_fetch_ohlcv_raw",
        lambda *args: (_ for _ in ()).throw(AssertionError("raw fallback used")),
    )
    assert kb._load_ohlcv("005930", "20250101", "20260822")["종가"].tolist() == [
        100,
        110,
    ]

    monkeypatch.setattr(
        kb._DATA_API_CLIENT,
        "latest_ohlcv",
        lambda ticker: (_ for _ in ()).throw(DataAPIUnavailable("down")),
    )
    monkeypatch.setattr(kb, "_fetch_ohlcv_raw", lambda *args: _make_price_df(10))
    assert len(kb._load_ohlcv("005930", "20250101", "20260822")) == 253


def test_scan_universe_sorts_by_score_desc(monkeypatch):
    """3종목 모두 다른 모멘텀 → score 내림차순 정렬."""
    universe = [("AAA", "에이"), ("BBB", "비"), ("CCC", "씨")]

    fakes = {
        "AAA": _make_price_df(10),   # +10%
        "BBB": _make_price_df(40),   # +40% (1등)
        "CCC": _make_price_df(-5),   # -5%
    }
    def fake_ohlcv(ticker, start, end):
        return fakes[ticker]
    monkeypatch.setattr(kb, "_fetch_ohlcv_raw", fake_ohlcv)

    results = kb.scan_universe(top_n=10, skip_days=0, universe=universe)
    assert [r["ticker"] for r in results] == ["BBB", "AAA", "CCC"]
    assert results[0]["score"] > results[1]["score"] > results[2]["score"]


def test_scan_universe_top_n_truncates(monkeypatch):
    universe = [(f"T{i:03d}", f"종{i}") for i in range(20)]
    monkeypatch.setattr(kb, "_fetch_ohlcv_raw",
                        lambda t, s, e: _make_price_df(float(t[-3:])))
    results = kb.scan_universe(top_n=5, skip_days=0, universe=universe)
    assert len(results) == 5
    # 가장 높은 5개 (인덱스 19,18,17,16,15)
    assert results[0]["ticker"] == "T019"


def test_scan_universe_skips_short_history(monkeypatch):
    """가격 데이터가 lookback+1보다 짧으면 skip."""
    universe = [("OK", "정상"), ("SHORT", "짧음")]
    fakes = {
        "OK": _make_price_df(15),
        "SHORT": pd.DataFrame({"종가": [100.0] * 50}),  # 50일치만
    }
    monkeypatch.setattr(kb, "_fetch_ohlcv_raw", lambda t, s, e: fakes[t])
    results = kb.scan_universe(top_n=10, skip_days=0, universe=universe)
    assert len(results) == 1
    assert results[0]["ticker"] == "OK"


def test_scan_universe_skips_fetch_exception(monkeypatch):
    """한 종목 fetch 실패해도 나머지는 정상 처리."""
    universe = [("OK", "정상"), ("FAIL", "실패")]
    def fake_ohlcv(ticker, start, end):
        if ticker == "FAIL":
            raise RuntimeError("network down")
        return _make_price_df(10)
    monkeypatch.setattr(kb, "_fetch_ohlcv_raw", fake_ohlcv)
    results = kb.scan_universe(top_n=10, skip_days=0, universe=universe)
    assert [r["ticker"] for r in results] == ["OK"]


def test_scan_universe_includes_metadata(monkeypatch):
    """결과 dict에 ticker/name/score/current/return_1m/return_12m 포함."""
    universe = [("AAA", "에이")]
    monkeypatch.setattr(kb, "_fetch_ohlcv_raw",
                        lambda t, s, e: _make_price_df(20))
    results = kb.scan_universe(top_n=10, universe=universe)
    assert len(results) == 1
    r = results[0]
    for key in ("ticker", "name", "score", "current_price", "return_1m", "return_12m"):
        assert key in r


# ─── format_scan_result ─────────────────────────────


def test_format_scan_result_empty():
    out = kb.format_scan_result([])
    assert "결과 없음" in out


def test_format_scan_result_renders_top_n():
    rows = [
        {"ticker": "005930", "name": "삼성전자", "score": 0.25,
         "current_price": 80000, "return_1m": 0.05, "return_12m": 0.30},
        {"ticker": "000660", "name": "SK하이닉스", "score": 0.18,
         "current_price": 200000, "return_1m": -0.02, "return_12m": 0.20},
    ]
    out = kb.format_scan_result(rows)
    assert "삼성전자" in out
    assert "SK하이닉스" in out
    assert "Top 2" in out
    assert "+25.0%" in out
    assert "+18.0%" in out
    assert "생존편향" in out


# ─── run() entrypoint ───────────────────────────────


def test_run_unknown_action():
    msg, _ = kb.run("dance")
    assert "❌" in msg
    assert "scan" in msg


def test_run_invalid_market():
    msg, _ = kb.run("scan", market="NASDAQ")
    assert "❌" in msg
    assert "지원하지 않는" in msg or "NASDAQ" in msg


def test_run_top_n_clamping(monkeypatch):
    """top_n 100 → 50으로 클램핑."""
    monkeypatch.setattr(kb, "scan_universe",
                        lambda **kw: [{"ticker": "X", "name": "x", "score": 0.1,
                                       "current_price": 1000, "return_1m": 0.0, "return_12m": 0.1}] * kw["top_n"])
    msg, _ = kb.run("scan", top_n=100)
    # Top 50 (50으로 clamped)
    assert "Top 50" in msg


def test_run_top_n_zero_minimum(monkeypatch):
    """top_n=0 → 1로 클램핑."""
    monkeypatch.setattr(kb, "scan_universe",
                        lambda **kw: [{"ticker": "X", "name": "x", "score": 0.1,
                                       "current_price": 1000, "return_1m": 0.0, "return_12m": 0.1}])
    msg, _ = kb.run("scan", top_n=0)
    assert "Top 1" in msg


def test_run_scan_happy_path(monkeypatch):
    """전체 흐름 — universe + ohlcv 모두 patch → 결과 메시지 검증."""
    monkeypatch.setattr(kb, "fetch_universe",
                        lambda market="KOSPI200", force_refresh=False:
                        [("AAA", "에이"), ("BBB", "비")])
    monkeypatch.setattr(kb, "_fetch_ohlcv_raw",
                        lambda t, s, e: _make_price_df(15.0 if t == "AAA" else 5.0))
    msg, sources = kb.run("scan", top_n=2)
    assert "키움봇 모멘텀 스캔" in msg
    assert "에이" in msg
    assert "비" in msg
    assert sources == []


def test_run_scan_failure_returns_error(monkeypatch):
    def boom(**kw):
        raise RuntimeError("fetch broken")
    monkeypatch.setattr(kb, "scan_universe", boom)
    msg, _ = kb.run("scan")
    assert "❌" in msg
    assert "키움봇 스캔 실패" in msg


# ─── router validator ──────────────────────────────


def test_router_validate_kium_args_scan_minimal():
    import router
    out = router._validate_kium_args({"action": "scan"})
    assert out == {"action": "scan"}


def test_router_validate_kium_args_with_top_n_market():
    import router
    out = router._validate_kium_args({
        "action": "scan", "top_n": 20, "market": "kosdaq150"  # 대소문자 무관
    })
    assert out == {"action": "scan", "top_n": 20, "market": "KOSDAQ150"}


def test_router_validate_kium_args_clamp_top_n():
    import router
    # 라우터 validator는 단순 검증만 — 100은 범위 밖이라 omit
    out = router._validate_kium_args({"action": "scan", "top_n": 100})
    assert out is not None
    assert "top_n" not in out  # 1~50 범위 밖


def test_router_validate_kium_args_unknown_action():
    import router
    assert router._validate_kium_args({"action": "ranking"}) is None


def test_router_validate_kium_args_invalid_market_omits():
    import router
    out = router._validate_kium_args({"action": "scan", "market": "NASDAQ"})
    assert out == {"action": "scan"}  # 잘못된 market은 omit, action만 통과


def test_router_kium_in_known_tools():
    import router
    assert "kium_bot" in router.KNOWN_TOOLS
# 이 블록을 tests/test_kium_bot.py 끝에 append (별도 import 추가 안 함 — pd, pytest, kb 이미 있음)


# ─── 3중 크래시 감지 (v3.17) ────────────────────────


def _make_returns_series(daily_std: float, n: int = 100) -> pd.Series:
    """일별 수익률 std=daily_std인 등간격 가격 시리즈."""
    import numpy as np
    rng = np.random.default_rng(42)
    rets = rng.normal(0, daily_std, n)
    prices = [100.0]
    for r in rets:
        prices.append(prices[-1] * (1 + r))
    return pd.Series(prices)


def test_volatility_spike_no_data_returns_safe():
    out = kb.compute_volatility_spike([100.0] * 30)
    assert out["hit"] is False
    assert out["ratio"] is None  # 데이터 부족


def test_volatility_spike_normal_no_hit():
    """일정한 변동성 → ratio ≈ 1.0, hit=False."""
    prices = _make_returns_series(0.01, n=100)  # 1% 변동성 일정
    out = kb.compute_volatility_spike(prices, threshold=1.5)
    assert out["hit"] is False


def test_volatility_spike_recent_spike_hits():
    """최근 21일만 변동성 3배 → ratio > 1.5 → hit=True."""
    import numpy as np
    rng = np.random.default_rng(0)
    long_part = rng.normal(0, 0.005, 80)  # 0.5% std 80일
    spike_part = rng.normal(0, 0.03, 25)  # 3% std 25일 (최근)
    prices = [100.0]
    for r in list(long_part) + list(spike_part):
        prices.append(prices[-1] * (1 + r))
    out = kb.compute_volatility_spike(pd.Series(prices), threshold=1.5)
    assert out["hit"] is True
    assert out["ratio"] > 1.5


def test_volatility_spike_with_list_input():
    out = kb.compute_volatility_spike(list(_make_returns_series(0.01, n=100)))
    assert out["hit"] is False  # 정상 수준


def test_volatility_spike_none_input():
    out = kb.compute_volatility_spike(None)
    assert out["hit"] is False
    assert out["ratio"] is None


def test_market_panic_no_drop():
    """KOSPI 평탄 → hit=False."""
    prices = pd.Series([2500.0] * 30)
    out = kb.compute_market_panic(prices, window=20, threshold=-0.10)
    assert out["hit"] is False
    assert abs(out["return_window"]) < 0.001


def test_market_panic_severe_drop_hits():
    """KOSPI 20일 -15% → hit=True."""
    n = 30
    # 20일 전 = 2500, 현재 = 2125 (-15%)
    prices = [2500.0] * (n - 20) + [2500.0 * (1 - 0.15 * i / 20) for i in range(1, 21)]
    out = kb.compute_market_panic(pd.Series(prices), window=20, threshold=-0.10)
    assert out["hit"] is True
    assert out["return_window"] < -0.10


def test_market_panic_borderline_no_hit():
    """20일 -8% — 임계 -10% 안 도달 → hit=False."""
    prices = [2500.0] * 5 + [2500.0 * (1 - 0.08 * i / 20) for i in range(1, 21)]
    out = kb.compute_market_panic(pd.Series(prices), window=20, threshold=-0.10)
    assert out["hit"] is False


def test_market_panic_insufficient_data():
    out = kb.compute_market_panic([100.0] * 5, window=20)
    assert out["hit"] is False
    assert out["return_window"] is None


def test_momentum_reversal_empty():
    out = kb.compute_momentum_reversal([])
    assert out["hit"] is False
    assert out["n_evaluated"] == 0


def test_momentum_reversal_positive_avg_no_hit():
    rows = [{"return_1m": 0.05}, {"return_1m": 0.02}, {"return_1m": -0.01}]
    out = kb.compute_momentum_reversal(rows)
    assert out["hit"] is False  # 평균 +0.02
    assert out["n_evaluated"] == 3


def test_momentum_reversal_negative_avg_hits():
    rows = [{"return_1m": -0.05}, {"return_1m": -0.02}, {"return_1m": 0.01}]
    out = kb.compute_momentum_reversal(rows)
    assert out["hit"] is True  # 평균 -0.02
    assert out["avg_return_1m"] < 0


def test_momentum_reversal_skips_none():
    rows = [{"return_1m": -0.10}, {"return_1m": None}, {"return_1m": 0.05}]
    out = kb.compute_momentum_reversal(rows)
    assert out["n_evaluated"] == 2
    # 평균 -0.025 → hit=True
    assert out["hit"] is True


def test_detect_crash_signals_zero_hits():
    """평탄한 시장 + 양수 모멘텀 → 0 hits, 정상 운용."""
    flat_prices = pd.Series([2500.0] * 100)
    rows = [{"return_1m": 0.05}] * 5
    out = kb.detect_crash_signals(kospi_close=flat_prices, top_results=rows)
    assert out["hits"] == 0
    assert "정상" in out["recommendation"]


def test_detect_crash_signals_two_or_more_hits_warns():
    """20일 -15% + 모멘텀 평균 -3% → 최소 2 hits."""
    n = 100
    prices = [2500.0] * (n - 20) + [2500.0 * (1 - 0.15 * i / 20) for i in range(1, 21)]
    rows = [{"return_1m": -0.05}] * 10
    out = kb.detect_crash_signals(
        kospi_close=pd.Series(prices), top_results=rows
    )
    assert out["hits"] >= 2
    assert "보수적 운용 권고" in out["recommendation"]


def test_detect_crash_signals_one_hit_monitor():
    """모멘텀 역전만 → 1 hit, 모니터링 강화."""
    flat_prices = pd.Series([2500.0] * 100)
    rows = [{"return_1m": -0.05}] * 5
    out = kb.detect_crash_signals(
        kospi_close=flat_prices, top_results=rows
    )
    assert out["hits"] == 1
    assert "모니터링" in out["recommendation"]


def test_detect_crash_signals_no_kospi_data():
    """kospi_close=None이어도 momentum_reversal만 평가."""
    rows = [{"return_1m": -0.05}] * 5
    out = kb.detect_crash_signals(kospi_close=None, top_results=rows)
    # vol/panic은 None → hit=False, reversal만 hit
    assert out["hits"] == 1


# ─── VKOSPI 비중 룰 ───────────────────────────────


def test_weight_default_no_data():
    """입력 없으면 디폴트 70/30 + reason='데이터 부족'."""
    out = kb.compute_weight_recommendation()
    assert out["equity_weight"] == 0.70
    assert out["bond_weight"] == 0.30
    assert "데이터 부족" in out["reason"]


def test_weight_kospi_above_ma_uses_70():
    # 200일선 위로 이동 — 마지막이 평균보다 큼
    prices = pd.Series(list(range(200, 400)))  # 200~399 단조 증가, 200개
    out = kb.compute_weight_recommendation(kospi_close=prices, ma_window=200)
    assert out["kospi_above_ma"] is True
    assert out["equity_weight"] == 0.70


def test_weight_kospi_below_ma_uses_50():
    prices = pd.Series(list(range(400, 200, -1)))  # 단조 감소, 200개
    out = kb.compute_weight_recommendation(kospi_close=prices, ma_window=200)
    assert out["kospi_above_ma"] is False
    assert out["equity_weight"] == 0.50


def test_weight_high_vkospi_reduces_equity():
    """VKOSPI 35 → 채권 +10%p (70 → 60)."""
    prices = pd.Series(list(range(200, 400)))
    out = kb.compute_weight_recommendation(vkospi=35.0, kospi_close=prices)
    assert out["vkospi_band"] == "high"
    assert out["equity_weight"] == 0.60


def test_weight_low_vkospi_boosts_equity():
    prices = pd.Series(list(range(200, 400)))
    out = kb.compute_weight_recommendation(vkospi=12.0, kospi_close=prices)
    assert out["vkospi_band"] == "low"
    assert out["equity_weight"] == 0.80


def test_weight_mid_vkospi_neutral():
    prices = pd.Series(list(range(200, 400)))
    out = kb.compute_weight_recommendation(vkospi=20.0, kospi_close=prices)
    assert out["vkospi_band"] == "mid"
    assert out["equity_weight"] == 0.70  # 변화 없음


def test_weight_clamping():
    """KOSPI < MA + VKOSPI 35 → 50 - 10 = 40 (하한 30 안 닿음)."""
    prices = pd.Series(list(range(400, 200, -1)))
    out = kb.compute_weight_recommendation(vkospi=35.0, kospi_close=prices)
    assert out["equity_weight"] == 0.40
    assert 0.30 <= out["equity_weight"] <= 0.90


# ─── format_scan_result + crash + weight 통합 ──────


def test_format_includes_crash_signals_section():
    rows = [
        {"ticker": "005930", "name": "삼성전자", "score": 0.20,
         "current_price": 80000, "return_1m": 0.05, "return_12m": 0.30}
    ]
    crash = {
        "vol_spike": {"hit": True, "ratio": 1.8},
        "market_panic": {"hit": False, "return_window": -0.05},
        "momentum_reversal": {"hit": True, "avg_return_1m": -0.03, "n_evaluated": 5},
        "hits": 2, "recommendation": "⚠️ 보수적 운용 권고 — 2개 hit",
    }
    out = kb.format_scan_result(rows, crash_signals=crash)
    assert "보수적 운용" in out
    assert "🚨" in out  # vol/reversal hit
    assert "✓" in out   # panic 안 hit


def test_format_includes_weight_section():
    rows = [{"ticker": "005930", "name": "삼성전자", "score": 0.20,
             "current_price": 80000, "return_1m": 0.0, "return_12m": 0.3}]
    weight = {
        "equity_weight": 0.60, "bond_weight": 0.40,
        "kospi_above_ma": True, "vkospi_band": "high",
        "vkospi_value": 35.0,
        "reason": "KOSPI > 200일선 → 주식 우위(70%) · VKOSPI 35.0 > 30 → 채권 +10%p",
    }
    out = kb.format_scan_result(rows, weight=weight)
    assert "주식 60%" in out
    assert "채권 40%" in out
    assert "VKOSPI" in out


def test_format_without_signals_unchanged():
    """v3.16 BC — crash_signals/weight 없이 호출하면 원래 출력."""
    rows = [{"ticker": "005930", "name": "삼성전자", "score": 0.20,
             "current_price": 80000, "return_1m": 0.05, "return_12m": 0.3}]
    out = kb.format_scan_result(rows)
    assert "보수적" not in out
    assert "주식" not in out  # 비중 섹션 없음


# ─── run("scan", with_crash_signals=True) 통합 ──────


def test_run_scan_with_crash_signals_invokes_detection(monkeypatch):
    """with_crash_signals=True → fetch_kospi_close + detect_crash_signals + weight."""
    # universe + ohlcv mock
    monkeypatch.setattr(kb, "fetch_universe",
                        lambda market="KOSPI200", force_refresh=False:
                        [("AAA", "에이"), ("BBB", "비")])

    def fake_ohlcv(ticker, start, end):
        # 충분히 긴 시계열, 양수 모멘텀
        n = 270
        ratio = 1.0008  # ~ +20%/year
        closes = [100.0 * (ratio ** i) for i in range(n)]
        return pd.DataFrame({"종가": closes})

    monkeypatch.setattr(kb, "_fetch_ohlcv_raw", fake_ohlcv)

    # KOSPI mock — 평탄한 시장 (panic 안 함)
    monkeypatch.setattr(
        kb, "fetch_kospi_close",
        lambda days=280: pd.Series([2500.0] * 250),
    )
    monkeypatch.setattr(kb, "fetch_vkospi_latest", lambda: 18.0)  # mid

    msg, _ = kb.run("scan", top_n=2, with_crash_signals=True)
    assert "키움봇 모멘텀 스캔" in msg
    # crash recommendation 섹션 포함
    assert "정상" in msg or "모니터링" in msg or "보수적" in msg
    # weight 섹션
    assert "주식" in msg and "채권" in msg


def test_run_scan_without_crash_signals_default(monkeypatch):
    """with_crash_signals 안 주면 (BC) 기존 출력 그대로."""
    monkeypatch.setattr(kb, "fetch_universe",
                        lambda market="KOSPI200", force_refresh=False:
                        [("AAA", "에이")])
    monkeypatch.setattr(
        kb, "_fetch_ohlcv_raw",
        lambda t, s, e: pd.DataFrame({"종가": [100.0 * 1.001 ** i for i in range(270)]})
    )
    msg, _ = kb.run("scan", top_n=1)
    assert "키움봇 모멘텀 스캔" in msg
    assert "보수적" not in msg
    assert "주식 " not in msg  # 비중 섹션 없음


def test_run_scan_crash_signals_failure_falls_back(monkeypatch):
    """KOSPI fetch 실패해도 결과는 나옴 (단지 crash 섹션만 빠짐)."""
    monkeypatch.setattr(kb, "fetch_universe",
                        lambda market="KOSPI200", force_refresh=False:
                        [("AAA", "에이")])
    monkeypatch.setattr(
        kb, "_fetch_ohlcv_raw",
        lambda t, s, e: pd.DataFrame({"종가": [100.0 * 1.001 ** i for i in range(270)]})
    )

    def boom(*args, **kwargs):
        raise RuntimeError("KRX down")
    monkeypatch.setattr(kb, "fetch_kospi_close", boom)

    msg, _ = kb.run("scan", top_n=1, with_crash_signals=True)
    assert "키움봇 모멘텀 스캔" in msg
    assert "에이" in msg


# ─── KOSPI / VKOSPI API + collector 경계 ───────────


def _index_payload(index_name, closes, *, last_date=None):
    last = last_date or kb.datetime.now().strftime("%Y%m%d")
    dates = [last] * len(closes)
    return {
        "index": index_name,
        "as_of": last,
        "series": {"date": dates, "close": closes},
    }


def test_fetch_kospi_close_returns_close_series(monkeypatch):
    monkeypatch.setattr(
        kb, "_collect_market_index",
        lambda name, days: _index_payload(name, [2500.0, 2510.0, 2520.0]),
    )
    out = kb.fetch_kospi_close()
    assert out is not None
    assert list(out) == [2500.0, 2510.0, 2520.0]


def test_fetch_kospi_close_handles_exception(monkeypatch):
    def boom(name, days):
        raise RuntimeError("KRX down")
    monkeypatch.setattr(kb, "_collect_market_index", boom)
    assert kb.fetch_kospi_close() is None


def test_fetch_vkospi_latest_returns_float(monkeypatch):
    monkeypatch.setattr(
        kb, "_collect_market_index",
        lambda name, days: _index_payload(name, [18.5, 19.0, 17.5]),
    )
    out = kb.fetch_vkospi_latest()
    assert out == 17.5


def test_fetch_vkospi_latest_handles_empty(monkeypatch):
    monkeypatch.setattr(
        kb, "_collect_market_index", lambda name, days: _index_payload(name, [])
    )
    assert kb.fetch_vkospi_latest() is None


def test_fetch_kospi_close_api_first(monkeypatch):
    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    payload = _index_payload("KOSPI", list(range(220)))
    monkeypatch.setattr(kb._DATA_API_CLIENT, "latest_market_index", lambda name: payload)
    monkeypatch.setattr(
        kb,
        "_collect_market_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("collector used")),
    )
    assert len(kb.fetch_kospi_close(days=200)) == 220


def test_fetch_market_index_stale_api_uses_collector(monkeypatch):
    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    stale = _index_payload("VKOSPI", [99.0], last_date="20000101")
    monkeypatch.setattr(kb._DATA_API_CLIENT, "latest_market_index", lambda name: stale)
    monkeypatch.setattr(
        kb,
        "_collect_market_index",
        lambda name, days: _index_payload(name, [18.5]),
    )
    assert kb.fetch_vkospi_latest() == 18.5


# ─── router validator: with_crash_signals ──────────


def test_router_validate_with_crash_signals_true():
    import router
    out = router._validate_kium_args({"action": "scan", "with_crash_signals": True})
    assert out["with_crash_signals"] is True


def test_router_validate_with_crash_signals_false():
    import router
    out = router._validate_kium_args({"action": "scan", "with_crash_signals": False})
    assert out["with_crash_signals"] is False


def test_router_validate_with_crash_signals_string_truthy():
    import router
    out = router._validate_kium_args({"action": "scan", "with_crash_signals": "true"})
    assert out["with_crash_signals"] is True


def test_router_validate_with_crash_signals_int():
    import router
    out = router._validate_kium_args({"action": "scan", "with_crash_signals": 1})
    assert out["with_crash_signals"] is True


def test_router_validate_with_crash_signals_omitted():
    import router
    out = router._validate_kium_args({"action": "scan"})
    assert "with_crash_signals" not in out
