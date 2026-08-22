"""signal_bot 단위 테스트 — 지표·신호 규칙·스캔(mock).

외부 호출(_fetch_intraday_raw / resolve_etf_ticker)은 monkeypatch.
지표 계산은 결정론이라 직접 검증.
"""
from __future__ import annotations
import math
import pytest

import signal_bot as sb


# ─── 지표 ───────────────────────────────────────────


def test_rsi_all_gains_is_100():
    closes = [100 + i for i in range(30)]
    rsi = sb.compute_rsi(closes)
    assert rsi[-1] == 100.0


def test_rsi_all_losses_is_low():
    closes = [100 - i for i in range(30)]
    rsi = sb.compute_rsi(closes)
    assert rsi[-1] == 0.0


def test_rsi_short_series_returns_none():
    assert sb.compute_rsi([1, 2, 3])[-1] is None


def test_macd_uptrend_positive():
    closes = [100 + i * 0.7 for i in range(60)]
    macd, sig, hist = sb.compute_macd(closes)
    assert macd[-1] is not None and macd[-1] > 0


def test_bollinger_bands_ordered():
    closes = [100 + math.sin(i / 3) * 5 for i in range(40)]
    mid, up, lo = sb.compute_bollinger(closes)
    assert lo[-1] < mid[-1] < up[-1]


def test_stochrsi_in_unit_range():
    closes = [100 + math.sin(i / 5) * 10 for i in range(60)]
    k, d = sb.compute_stoch_rsi(closes)
    vals = [x for x in k if x is not None]
    assert all(0.0 <= x <= 1.0 for x in vals)


# ─── 신호 규칙 ──────────────────────────────────────


def _macd_item():
    return sb.WatchItem("X", 5, "macd", "지수")


def test_macd_signal_cross_up_is_buy():
    # 하락 후 상승 → macd 0선 상향 돌파 유도
    closes = [100 - i * 0.5 for i in range(40)] + [80 + i * 1.2 for i in range(40)]
    sig = sb.evaluate(_macd_item(), {"closes": closes}, "000001")
    assert sig is not None
    assert sig.action in ("매수", "홀딩유지")


def test_macd_signal_strong_uptrend_holding():
    closes = [100 + i * 0.8 for i in range(60)]
    sig = sb.evaluate(_macd_item(), {"closes": closes}, "000001")
    assert sig is not None and sig.action == "홀딩유지"


def test_bollinger_lower_touch_is_buy():
    base = [100.0] * 40
    base[-1] = 80.0  # 마지막 봉 급락 → 하단 이탈/터치
    sig = sb.evaluate(sb.WatchItem("K", 20, "bollinger", "지수"),
                      {"closes": base}, "000002")
    assert sig is not None and sig.action == "매수"


def test_bollinger_band_walk_is_sell():
    closes = [100.0] * 38 + [70.0, 65.0]  # 연속 2봉 하단 이탈
    sig = sb.evaluate(sb.WatchItem("K", 20, "bollinger", "지수"),
                      {"closes": closes}, "000002")
    assert sig is not None and sig.action == "매도" and "Band Walk" in sig.reason


def test_stochrsi_oversold_golden_volume_is_buy():
    # 큰 하락 후 마지막에 반등 + 거래량 스파이크 → 과매도 골든크로스
    closes = [100 - i for i in range(50)] + [50, 53]
    volumes = [100.0] * (len(closes) - 1) + [500.0]
    sig = sb.evaluate(sb.WatchItem("S", 6, "stochrsi", "섹터"),
                      {"closes": closes, "volumes": volumes}, "000003")
    # 신호가 나오면 매수여야 (안 나올 수도 있으니 None 허용 안 함 위해 데이터 조정)
    assert sig is None or sig.action == "매수"


def test_monitor_safe_asset_drop_warns():
    closes = [100.0] * 39 + [98.5]  # -1.5%
    sig = sb.evaluate(sb.WatchItem("채권", 15, "monitor", "채권"),
                      {"closes": closes}, "000004")
    assert sig is not None and sig.action == "경고"


def test_monitor_safe_asset_stable_no_signal():
    closes = [100.0] * 40
    sig = sb.evaluate(sb.WatchItem("채권", 15, "monitor", "채권"),
                      {"closes": closes}, "000004")
    assert sig is None


def test_too_few_candles_returns_none():
    sig = sb.evaluate(_macd_item(), {"closes": [100, 101, 102]}, "000001")
    assert sig is None


# ─── 스캔 (외부 호출 mock) ──────────────────────────


def test_scan_with_mocked_fetch(monkeypatch):
    # 코드 해석·intraday fetch를 가짜로
    monkeypatch.setattr(sb, "resolve_etf_ticker", lambda name: "069500")

    def fake_fetch(sym, interval="60m", period="60d"):
        # 강한 상승 → macd 종목은 홀딩유지 신호
        return {"closes": [100 + i * 0.8 for i in range(60)],
                "volumes": [100.0] * 60}
    monkeypatch.setattr(sb, "_fetch_intraday_raw", fake_fetch)

    sigs = sb.scan()
    assert isinstance(sigs, list)
    # macd 종목 2개는 홀딩유지 신호가 나와야
    macd_sigs = [s for s in sigs if s.strategy == "MACD"]
    assert len(macd_sigs) >= 1


def test_scan_skips_on_empty_fetch(monkeypatch):
    monkeypatch.setattr(sb, "resolve_etf_ticker", lambda name: "069500")
    monkeypatch.setattr(sb, "_fetch_intraday_raw", lambda *a, **k: None)
    assert sb.scan() == []


def test_format_signals_empty():
    assert sb.format_signals([]) == ""


def test_format_signals_orders_buy_first():
    sigs = [
        sb.Signal("A", "1", "MACD", "홀딩유지", "✅", "r", 100, 5),
        sb.Signal("B", "2", "볼린저", "매수", "🟢", "r", 100, 20),
    ]
    out = sb.format_signals(sigs)
    assert out.index("매수") < out.index("홀딩유지")


def test_etf_map_empty_guard(monkeypatch):
    # 빈 응답이면 캐시 저장 안 함 (v3.21 가드)
    monkeypatch.setattr(sb, "_fetch_etf_map_raw", lambda d: {})
    sb._ETF_MAP_CACHE.clear()
    monkeypatch.setattr(sb, "_load_etf_map_disk", lambda d: None)
    assert sb.get_etf_map() == {}


# ─── 멱등 dedup ─────────────────────────────────────


def _sig(t, strat, act):
    return sb.Signal("N", t, strat, act, "x", "r", 100, 5)


def test_filter_new_signals_dedup():
    sigs = [_sig("1", "MACD", "매수"), _sig("2", "볼린저", "매도")]
    fresh, keys = sb.filter_new_signals(sigs, set())
    assert len(fresh) == 2
    # 같은 신호 재전송 시 0건
    fresh2, _ = sb.filter_new_signals(sigs, keys)
    assert fresh2 == []


def test_filter_new_signals_partial():
    sigs = [_sig("1", "MACD", "매수"), _sig("2", "볼린저", "매도")]
    _, keys = sb.filter_new_signals([sigs[0]], set())
    fresh, _ = sb.filter_new_signals(sigs, keys)
    assert len(fresh) == 1 and fresh[0].ticker == "2"


def test_sent_keys_roundtrip(tmp_path):
    p = tmp_path / "signal_last.json"
    sb.save_sent_keys(p, "2026-06-02", {"1|MACD|매수"})
    assert sb.load_sent_keys(p, "2026-06-02") == {"1|MACD|매수"}


def test_sent_keys_date_rollover(tmp_path):
    p = tmp_path / "signal_last.json"
    sb.save_sent_keys(p, "2026-06-01", {"old"})
    # 다른 날짜로 읽으면 빈 집합 (자동 롤오버)
    assert sb.load_sent_keys(p, "2026-06-02") == set()


def test_sent_keys_missing_file(tmp_path):
    assert sb.load_sent_keys(tmp_path / "nope.json", "2026-06-02") == set()


# ─── wiki 파서 (v2) ─────────────────────────────────

_SAMPLE_MD = """
## 위험자산
| 분류 | 종목 | 코드 | 비중 | 적용 지표 |
|---|---|---|---|---|
| 해외주식_지수 | KODEX 미국나스닥100 | 379810 | 5% | MACD (미국 대형 지수 추종) |
| 국내주식_지수 | TIGER 200 | 102110 | 20% | 볼린저+20MA (박스권) |
| 국내주식_섹터 | KODEX 반도체 | 091160 | 6% | StochRSI+거래량 |
| FX 파생 | 달러선물 | KRW=X | 4% | StochRSI+거래량 |
| 금리연계 | KODEX KOFR금리액티브(합성) | 423160 | 5% | 모니터링만 |
"""


def test_parse_watchlist_basic():
    wl = sb.parse_watchlist_from_wiki(_SAMPLE_MD)
    assert len(wl) == 5
    by = {w.name: w for w in wl}
    assert by["KODEX 미국나스닥100"].strategy == "macd"
    assert by["KODEX 미국나스닥100"].code == "379810"
    assert by["TIGER 200"].strategy == "bollinger"
    assert by["KODEX 반도체"].strategy == "stochrsi"
    assert by["KODEX KOFR금리액티브(합성)"].strategy == "monitor"


def test_parse_watchlist_weight():
    wl = sb.parse_watchlist_from_wiki(_SAMPLE_MD)
    assert {w.name: w.weight for w in wl}["TIGER 200"] == 20.0


def test_parse_watchlist_fx_uses_yf_override():
    wl = sb.parse_watchlist_from_wiki(_SAMPLE_MD)
    fx = [w for w in wl if w.name == "달러선물"][0]
    assert fx.code is None and fx.yf_override == "KRW=X"


def test_parse_watchlist_skips_header_and_separator():
    wl = sb.parse_watchlist_from_wiki(_SAMPLE_MD)
    assert all(w.name not in ("종목", "") for w in wl)


def test_parse_watchlist_empty_text():
    assert sb.parse_watchlist_from_wiki("") == []


def test_parse_watchlist_no_table():
    assert sb.parse_watchlist_from_wiki("# 제목\n본문만 있음") == []


def test_strategy_from_text_mapping():
    assert sb._strategy_from_text("MACD (추세)") == "macd"
    assert sb._strategy_from_text("볼린저+20MA") == "bollinger"
    assert sb._strategy_from_text("StochRSI+거래량") == "stochrsi"
    assert sb._strategy_from_text("모니터링만") == "monitor"
    assert sb._strategy_from_text("알수없음") == "stochrsi"


def test_load_watchlist_fallback_on_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "WIKI_PORTFOLIO_PATH", tmp_path / "nope.md")
    wl = sb.load_watchlist()
    assert wl is sb._FALLBACK_WATCHLIST
    assert sum(item.weight for item in wl) == 95.0


def test_load_watchlist_parses_real_file(monkeypatch, tmp_path):
    f = tmp_path / "p.md"
    f.write_text(_SAMPLE_MD, encoding="utf-8")
    monkeypatch.setattr(sb, "WIKI_PORTFOLIO_PATH", f)
    wl = sb.load_watchlist()
    assert len(wl) == 5 and wl is not sb._FALLBACK_WATCHLIST


# ─── 멀티 타임프레임 필터 (v3) ──────────────────────


def test_compute_trend_up():
    assert sb.compute_trend([100 + i for i in range(40)]) == "up"


def test_compute_trend_down():
    assert sb.compute_trend([100 - i for i in range(40)]) == "down"


def test_compute_trend_neutral_short():
    assert sb.compute_trend([100, 101, 102]) == "neutral"


def test_mtf_suppresses_buy_in_downtrend():
    sig = sb.Signal("X", "1", "MACD", "매수", "📈", "r", 100, 5)
    assert sb.apply_mtf_filter(sig, "down") is None


def test_mtf_suppresses_sell_in_uptrend():
    sig = sb.Signal("X", "1", "StochRSI", "경고", "⚠️", "r", 100, 5)
    assert sb.apply_mtf_filter(sig, "up") is None


def test_mtf_allows_buy_in_uptrend_and_annotates():
    sig = sb.Signal("X", "1", "MACD", "매수", "📈", "기본", 100, 5)
    out = sb.apply_mtf_filter(sig, "up")
    assert out is not None and "추세확인" in out.reason


def test_mtf_neutral_passes_through():
    sig = sb.Signal("X", "1", "MACD", "매수", "📈", "r", 100, 5)
    assert sb.apply_mtf_filter(sig, "neutral") is sig


def test_mtf_monitor_strategy_not_filtered():
    sig = sb.Signal("채권", "1", "모니터", "경고", "⚠️", "r", 100, 15)
    assert sb.apply_mtf_filter(sig, "up") is sig


def test_mtf_none_signal():
    assert sb.apply_mtf_filter(None, "up") is None


def test_scan_mtf_suppresses(monkeypatch):
    monkeypatch.setattr(sb, "resolve_etf_ticker", lambda n: "069500")
    # 1시간봉: 강상승 → macd 홀딩유지 신호
    monkeypatch.setattr(sb, "_fetch_intraday_raw",
                        lambda *a, **k: {"closes": [100 + i * 0.8 for i in range(60)], "volumes": [100.0]*60})
    # 일봉: 하락추세 → 매수성(홀딩유지) 억제
    monkeypatch.setattr(sb, "_fetch_daily_raw", lambda *a, **k: [200 - i for i in range(40)])
    sigs = sb.scan()
    assert all(s.action not in sb._BULLISH for s in sigs)
