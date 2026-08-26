"""
invest_bot 단위 테스트.

pykrx HTTP 호출(_fetch_ohlcv_raw / _fetch_ticker_map_raw)은 monkeypatch로
가짜 DataFrame/dict 주입. 순수 계산 함수(_rsi/_ma/_crossed/_annualized_vol)
+ compute_indicators + detect_signals + resolve_ticker + run() 분기 검증.
"""
from __future__ import annotations

import math
import os
from types import SimpleNamespace

import pandas as pd
import pytest

import invest_bot as ib


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ib, "_DISK_CACHE_DIR", tmp_path / "ticker-cache")
    ib._TICKER_MAP_CACHE.clear()
    yield
    ib._TICKER_MAP_CACHE.clear()


# ─── 순수 계산 함수 ──────────────────────────────────


def test_ma_basic():
    closes = [10, 12, 14, 16, 18]
    assert ib._ma(closes, 5) == 14.0
    assert ib._ma(closes, 3) == pytest.approx((14 + 16 + 18) / 3)


def test_ma_short_data_returns_none():
    assert ib._ma([1, 2, 3], 5) is None


def test_rsi_constant_prices_high():
    # 상승만: RSI는 100 근처
    closes = list(range(1, 30))  # 1, 2, 3, ..., 29
    assert ib._rsi(closes, 14) == pytest.approx(100.0)


def test_rsi_constant_decline_low():
    # 하락만: RSI는 0
    closes = list(range(30, 1, -1))
    val = ib._rsi(closes, 14)
    assert val is not None
    assert val < 5


def test_rsi_short_data_none():
    assert ib._rsi([1, 2, 3], 14) is None


def test_crossed_golden():
    # MA5 어제 < MA20, 오늘 ≥
    assert ib._crossed(10, 12, 13, 12) == "golden"


def test_crossed_dead():
    assert ib._crossed(13, 12, 11, 12) == "dead"


def test_crossed_no_change():
    assert ib._crossed(13, 12, 14, 12) == "none"
    assert ib._crossed(11, 12, 11, 12) == "none"


def test_crossed_none_input():
    assert ib._crossed(None, 12, 13, 12) == "none"


def test_annualized_vol_consistent():
    # 일정 비율 상승 → log return 일정 → stddev 0 → vol 0
    closes = [100 * (1.01 ** i) for i in range(40)]
    assert ib._annualized_vol(closes, 21) == pytest.approx(0, abs=1e-9)


def test_annualized_vol_short_none():
    assert ib._annualized_vol([100, 101, 102], 21) is None


# ─── compute_indicators ──────────────────────────────


def _make_df(closes, vols=None):
    n = len(closes)
    if vols is None:
        vols = [1000] * n
    return pd.DataFrame({
        "시가": closes,
        "고가": closes,
        "저가": closes,
        "종가": closes,
        "거래량": vols,
    })


def test_compute_indicators_empty():
    df = pd.DataFrame()
    out = ib.compute_indicators(df)
    assert "error" in out


def test_compute_indicators_full():
    closes = list(range(50, 200))  # 150개 일봉, 단조 증가
    vols = [1000] * 130 + [5000] * 20  # 마지막 20일 거래량 폭증
    df = _make_df(closes, vols)
    out = ib.compute_indicators(df)
    assert "error" not in out
    assert out["last_close"] == 199
    assert out["MA5"] == pytest.approx(197)
    assert out["MA20"] == pytest.approx(189.5)
    assert out["RSI14"] == pytest.approx(100.0)
    assert out["samples"] == 150
    # 거래량 비율: 마지막은 5000, 20일 평균은 5000 → 1.0
    assert out["vol_ratio_20d"] == pytest.approx(1.0)


def test_compute_indicators_no_close_column():
    df = pd.DataFrame({"고가": [1, 2, 3]})
    out = ib.compute_indicators(df)
    assert "error" in out


def test_compute_indicators_golden_cross_detection():
    # MA5와 MA20이 오늘 교차하도록 설계
    # 앞 25일 — MA5 < MA20 / 마지막 봉만 급등
    closes = [100] * 24 + [98]   # 어제까지 평탄 + 어제 약하락
    closes += [200]                # 오늘 급등 → MA5 평균 끌어올림
    df = _make_df(closes)
    out = ib.compute_indicators(df)
    # MA5(오늘): (100+100+98+100+200)/5 = 119.6
    # MA20(오늘): 19개 100 + 200 → 105.0 ... 사실은 110정도
    # MA5(어제): (100+100+100+100+98)/5 = 99.6
    # MA20(어제): 100 → 거의 100
    # 어제 99.6 < 100 → 오늘 119.6 > 110 → golden
    assert out["cross_5_20"] == "golden"


# ─── detect_signals ─────────────────────────────────


def test_detect_signals_overbought():
    sigs = ib.detect_signals({"RSI14": 75, "last_close": 100, "MA20": 90})
    assert any("과매수" in s for s in sigs)


def test_detect_signals_oversold():
    sigs = ib.detect_signals({"RSI14": 25})
    assert any("과매도" in s for s in sigs)


def test_detect_signals_volume_surge():
    sigs = ib.detect_signals({"vol_ratio_20d": 3.5})
    assert any("거래량 폭증" in s for s in sigs)


def test_detect_signals_volatility_surge():
    sigs = ib.detect_signals({
        "vol21_ann_pct": 50, "vol63_ann_pct": 30
    })
    assert any("단기 변동성" in s for s in sigs)


def test_detect_signals_empty_on_error():
    assert ib.detect_signals({"error": "x"}) == []


# ─── resolve_ticker ─────────────────────────────────


def test_ticker_map_adapters_delegate_to_collector(monkeypatch, tmp_path):
    seen = []
    fake = {"005930": "삼성전자"}

    monkeypatch.setattr(
        ib,
        "fetch_ticker_map_data",
        lambda date: seen.append(("fetch", date)) or dict(fake),
    )
    monkeypatch.setattr(
        ib,
        "write_ticker_map_cache",
        lambda date, mapping, cache_dir: seen.append(
            ("write", date, mapping, cache_dir)
        ),
    )
    monkeypatch.setattr(ib, "_DISK_CACHE_DIR", tmp_path)

    assert ib._fetch_ticker_map_raw("20260822") == fake
    ib._save_disk_cache("20260822", fake)
    assert seen == [
        ("fetch", "20260822"),
        ("write", "20260822", fake, tmp_path),
    ]


def test_resolve_ticker_direct_six_digits(monkeypatch):
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw",
                        lambda d: {"005930": "삼성전자", "035420": "NAVER"})
    res = ib.resolve_ticker("005930")
    assert res == ("005930", "삼성전자")


def test_resolve_ticker_exact_name(monkeypatch):
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw",
                        lambda d: {"005930": "삼성전자", "005935": "삼성전자우"})
    res = ib.resolve_ticker("삼성전자")
    assert res == ("005930", "삼성전자")


def test_resolve_ticker_partial_shortest(monkeypatch):
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw",
                        lambda d: {
                            "005930": "삼성전자",
                            "005935": "삼성전자우",
                            "207940": "삼성바이오로직스",
                        })
    res = ib.resolve_ticker("삼성")
    # 가장 짧은 이름 = '삼성전자'
    assert res == ("005930", "삼성전자")


def test_resolve_ticker_not_found(monkeypatch):
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw", lambda d: {"005930": "삼성전자"})
    assert ib.resolve_ticker("비트코인") is None


def test_resolve_ticker_empty():
    assert ib.resolve_ticker("") is None
    assert ib.resolve_ticker("   ") is None


def test_resolve_ticker_six_digit_unknown_passes(monkeypatch):
    """6자리 숫자라면 매핑에 없어도 그대로 (ticker, ticker) 반환."""
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw", lambda d: {})
    res = ib.resolve_ticker("999999")
    assert res == ("999999", "999999")


def test_resolve_ticker_uses_data_api_when_enabled(monkeypatch):
    class FakeClient:
        def get_instrument(self, ticker):
            return {"ticker": ticker, "name": "삼성전자"}

        def search_instruments(self, query, limit=20):
            return []

    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(ib, "_DATA_API_CLIENT", FakeClient())
    monkeypatch.setattr(
        ib,
        "get_ticker_map",
        lambda: (_ for _ in ()).throw(AssertionError("기존 캐시가 호출되면 안 됨")),
    )
    assert ib.resolve_ticker("005930") == ("005930", "삼성전자")


def test_resolve_name_uses_data_api_search_when_enabled(monkeypatch):
    class FakeClient:
        def get_instrument(self, ticker):
            return None

        def search_instruments(self, query, limit=20):
            return [
                {"ticker": "005935", "name": "삼성전자우"},
                {"ticker": "005930", "name": "삼성전자"},
            ]

    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "true")
    monkeypatch.setattr(ib, "_DATA_API_CLIENT", FakeClient())
    assert ib.resolve_ticker("삼성") == ("005930", "삼성전자")


def test_resolve_ticker_data_api_failure_falls_back(monkeypatch):
    class FailingClient:
        def get_instrument(self, ticker):
            raise ib.DataAPIError("down")

        def search_instruments(self, query, limit=20):
            raise ib.DataAPIError("down")

    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(ib, "_DATA_API_CLIENT", FailingClient())
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw", lambda d: {"005930": "삼성전자"})
    assert ib.resolve_ticker("005930") == ("005930", "삼성전자")


def test_fetch_ohlcv_uses_data_api_before_collector(monkeypatch):
    class FakeClient:
        def latest_ohlcv(self, ticker):
            return {
                "ticker": ticker,
                "as_of": "20260821",
                "series": {
                    "date": ["20260820", "20260821"],
                    "close": [70000, 71000],
                    "volume": [10, 20],
                },
            }

    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(ib, "_DATA_API_CLIENT", FakeClient())
    monkeypatch.setattr(
        ib,
        "_fetch_ohlcv_raw",
        lambda *args: (_ for _ in ()).throw(AssertionError("collector called")),
    )
    frame = ib.fetch_ohlcv("005930", days=2)
    assert frame["종가"].tolist() == [70000, 71000]
    assert frame["거래량"].tolist() == [10, 20]


def test_ohlcv_compatibility_wrapper_delegates_to_collector(monkeypatch):
    seen = []

    def fake_collect(ticker, start, end, ohlcv_dir):
        seen.append((ticker, start, end, ohlcv_dir))
        return {
            "ticker": ticker,
            "as_of": end,
            "series": {"date": [end], "close": [71000], "volume": [20]},
        }

    monkeypatch.setattr(ib, "collect_ohlcv", fake_collect)
    frame = ib._fetch_ohlcv_raw("005930", "20260801", "20260821")
    assert frame["종가"].tolist() == [71000]
    assert seen[0][3] == ib.PATHS.shareable_cache_dir / "ohlcv"


# ─── analyze ────────────────────────────────────────


def test_analyze_unknown_ticker(monkeypatch):
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw", lambda d: {})
    out = ib.analyze("비트코인")
    assert "error" in out


def test_analyze_full_flow(monkeypatch):
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw",
                        lambda d: {"005930": "삼성전자"})
    closes = list(range(50, 200))
    df = _make_df(closes)
    monkeypatch.setattr(ib, "_fetch_ohlcv_raw",
                        lambda ticker, start, end: df)
    out = ib.analyze("삼성전자")
    assert "error" not in out
    assert out["ticker"] == "005930"
    assert out["name"] == "삼성전자"
    assert out["indicators"]["last_close"] == 199
    assert any("과매수" in s for s in out["signals"])  # RSI 100


def test_analyze_fetch_failure(monkeypatch):
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw",
                        lambda d: {"005930": "삼성전자"})
    def boom(*a, **kw):
        raise RuntimeError("KRX 다운")
    monkeypatch.setattr(ib, "_fetch_ohlcv_raw", boom)
    out = ib.analyze("삼성전자")
    assert "error" in out
    assert "시세 조회 실패" in out["error"]


# ─── format_analysis ────────────────────────────────


def test_format_analysis_error():
    txt = ib.format_analysis({"error": "x"})
    assert "❌" in txt


def test_format_analysis_full():
    result = {
        "ticker": "005930",
        "name": "삼성전자",
        "indicators": {
            "last_close": 80000.0, "prev_close": 79000.0, "pct_change": 1.27,
            "MA5": 79500, "MA20": 78000, "MA60": 75000, "MA120": 72000,
            "RSI14": 65.0,
            "vol21_ann_pct": 25.0, "vol63_ann_pct": 22.0,
            "cross_5_20": "none", "cross_20_60": "none",
            "vol_ratio_20d": 1.2,
            "samples": 150,
        },
        "signals": ["MA20 위 (+2.6%)"],
    }
    txt = ib.format_analysis(result)
    assert "삼성전자" in txt
    assert "005930" in txt
    assert "RSI" in txt
    assert "MA20 위" in txt


# ─── run() entrypoint ────────────────────────────────


def test_run_analyze_missing_ticker():
    msg, _ = ib.run("analyze")
    assert "❌" in msg


def test_run_unknown_action():
    msg, _ = ib.run("dance", ticker_or_name="삼성전자")
    assert "❌" in msg


def test_run_compare_fast_falls_back_to_analyze(monkeypatch):
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw",
                        lambda d: {"005930": "삼성전자"})
    monkeypatch.setattr(ib, "_fetch_ohlcv_raw",
                        lambda t, s, e: _make_df(list(range(50, 200))))
    msg, sources = ib.run("compare_with_rules",
                          ticker_or_name="삼성전자", mode="fast")
    assert "fast 모드" in msg
    assert sources == []
    # LLM 호출 없으니 빠르게 끝남


def test_run_analyze_success(monkeypatch):
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw",
                        lambda d: {"005930": "삼성전자"})
    monkeypatch.setattr(ib, "_fetch_ohlcv_raw",
                        lambda t, s, e: _make_df(list(range(50, 200))))
    msg, _ = ib.run("analyze", ticker_or_name="삼성전자")
    assert "삼성전자" in msg
    assert "RSI" in msg


def test_run_compare_no_rules_fallback(monkeypatch):
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw",
                        lambda d: {"005930": "삼성전자"})
    monkeypatch.setattr(ib, "_fetch_ohlcv_raw",
                        lambda t, s, e: _make_df(list(range(50, 200))))
    monkeypatch.setattr(ib, "_gather_rules", lambda k_each=2: [])
    msg, chunks = ib.run("compare_with_rules",
                         ticker_or_name="삼성전자", mode="accurate")
    assert "삼성전자" in msg
    assert chunks == []
    assert "매매 규칙 노트를 찾지 못" in msg


# ─── 디스크 캐시 영속화 (B-1, v3.13) ───────────────────


def test_disk_cache_roundtrip(monkeypatch, tmp_path):
    """1) fresh fetch → 디스크 저장. 2) in-memory 비워도 디스크에서 로드 (fetch 미호출)."""
    monkeypatch.setattr(ib, "_DISK_CACHE_DIR", tmp_path)
    ib._TICKER_MAP_CACHE.clear()

    fake = {"005930": "삼성전자", "000660": "SK하이닉스"}
    calls = {"n": 0}

    def fake_fetch(date):
        calls["n"] += 1
        return dict(fake)

    monkeypatch.setattr(ib, "_fetch_ticker_map_raw", fake_fetch)

    m1 = ib.get_ticker_map()
    assert m1 == fake
    assert calls["n"] == 1
    # 디스크 파일 존재
    files = list(tmp_path.glob("ticker_map_*.json"))
    assert len(files) == 1

    # in-memory 비우고 다시
    ib._TICKER_MAP_CACHE.clear()
    m2 = ib.get_ticker_map()
    assert m2 == fake
    assert calls["n"] == 1, "디스크 캐시 hit인데 fetch가 또 호출됨"


def test_disk_cache_ttl_expired_triggers_fetch(monkeypatch, tmp_path):
    """디스크 파일 mtime이 24h 넘으면 무효 → fresh fetch."""
    import time
    monkeypatch.setattr(ib, "_DISK_CACHE_DIR", tmp_path)
    ib._TICKER_MAP_CACHE.clear()

    today = ib.datetime.now().strftime("%Y%m%d")
    p = tmp_path / f"ticker_map_{today}.json"
    p.write_text('{"005930": "삼성전자"}', encoding="utf-8")
    # 25시간 전 mtime
    old = time.time() - 25 * 3600
    os.utime(p, (old, old))

    fake = {"005930": "삼성전자", "NEW": "갱신됨"}

    def fake_fetch(date):
        return dict(fake)

    monkeypatch.setattr(ib, "_fetch_ticker_map_raw", fake_fetch)

    m = ib.get_ticker_map()
    assert m == fake, "TTL 지난 디스크 캐시를 무시 못 함"


def test_disk_cache_corrupted_falls_back_to_fetch(monkeypatch, tmp_path):
    """JSON 깨진 디스크 파일 → 경고 로그 + fresh fetch."""
    monkeypatch.setattr(ib, "_DISK_CACHE_DIR", tmp_path)
    ib._TICKER_MAP_CACHE.clear()

    today = ib.datetime.now().strftime("%Y%m%d")
    (tmp_path / f"ticker_map_{today}.json").write_text("{not valid json", encoding="utf-8")

    fake = {"005930": "삼성전자"}
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw", lambda d: dict(fake))

    m = ib.get_ticker_map()
    assert m == fake


def test_disk_cache_save_failure_does_not_break(monkeypatch, tmp_path):
    """디스크 쓰기 실패해도 본 동작에 영향 없어야 (best-effort 저장)."""
    monkeypatch.setattr(ib, "_DISK_CACHE_DIR", tmp_path / "readonly")  # 디렉토리 없음 → mkdir 시도
    # mkdir 막아버리기
    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(ib.Path, "mkdir", boom)
    ib._TICKER_MAP_CACHE.clear()

    fake = {"005930": "삼성전자"}
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw", lambda d: dict(fake))

    # 예외 없이 정상 반환되어야
    m = ib.get_ticker_map()
    assert m == fake


def test_disk_cache_force_refresh_ignores_disk(monkeypatch, tmp_path):
    """force_refresh=True 면 디스크 캐시도 우회하고 새로 fetch."""
    monkeypatch.setattr(ib, "_DISK_CACHE_DIR", tmp_path)
    ib._TICKER_MAP_CACHE.clear()

    today = ib.datetime.now().strftime("%Y%m%d")
    (tmp_path / f"ticker_map_{today}.json").write_text(
        '{"005930": "OLD_NAME"}', encoding="utf-8"
    )

    fresh = {"005930": "FRESH_NAME"}
    monkeypatch.setattr(ib, "_fetch_ticker_map_raw", lambda d: dict(fresh))

    m = ib.get_ticker_map(force_refresh=True)
    assert m == fresh
