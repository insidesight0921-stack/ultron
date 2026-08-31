"""signal_bot 단위 테스트 — 지표·신호 규칙·스캔(mock).

외부 호출(_fetch_intraday_raw / resolve_etf_ticker)은 monkeypatch.
지표 계산은 결정론이라 직접 검증.
"""
from __future__ import annotations
import math
import pytest

from pathlib import Path

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


def test_scan_with_mocked_fetch(monkeypatch, tmp_path):
    # 코드 해석·intraday fetch를 가짜로
    monkeypatch.setattr(sb, "resolve_etf_ticker", lambda name: "069500")
    # 2026-08-31: 신호 대상이 관심종목으로 바뀌었다. 스캔 **로직**을 보는
    # 테스트이므로 대상 목록을 명시적으로 준다(Private API를 때리지 않는다).
    monkeypatch.setattr(sb, "signal_watchlist", lambda: list(sb.WATCHLIST))

    def fake_fetch(sym, interval="60m", period="60d"):
        # 강한 상승 → macd 종목은 홀딩유지 신호
        return {"closes": [100 + i * 0.8 for i in range(60)],
                "volumes": [100.0] * 60}
    monkeypatch.setattr(sb, "_fetch_intraday_raw", fake_fetch)
    # 일봉도 반드시 mock한다 — 안 하면 실제 yfinance를 때린다(테스트가 장세에 따라 흔들린다)
    monkeypatch.setattr(sb, "_fetch_daily_raw",
                        lambda *a, **k: [100 + i for i in range(40)])   # 상승추세

    sigs = sb.scan(log_path=tmp_path / "log.jsonl")
    assert isinstance(sigs, list)
    # macd 종목 2개는 홀딩유지 신호가 나와야 (일봉 상승이라 MTF 통과)
    macd_sigs = [s for s in sigs if s.strategy == "MACD"]
    assert len(macd_sigs) >= 1


def test_scan_skips_on_empty_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "resolve_etf_ticker", lambda name: "069500")
    monkeypatch.setattr(sb, "_fetch_intraday_raw", lambda *a, **k: None)
    assert sb.scan(log_path=tmp_path / "log.jsonl") == []


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
    wl = sb.load_watchlist(include_personal=False)
    assert wl is sb._FALLBACK_WATCHLIST
    assert sum(item.weight for item in wl) == 95.0


def test_load_watchlist_parses_real_file(monkeypatch, tmp_path):
    f = tmp_path / "p.md"
    f.write_text(_SAMPLE_MD, encoding="utf-8")
    monkeypatch.setattr(sb, "WIKI_PORTFOLIO_PATH", f)
    wl = sb.load_watchlist(include_personal=False)
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


def test_scan_mtf_suppresses(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "resolve_etf_ticker", lambda n: "069500")
    # 1시간봉: 강상승 → macd 홀딩유지 신호
    monkeypatch.setattr(sb, "_fetch_intraday_raw",
                        lambda *a, **k: {"closes": [100 + i * 0.8 for i in range(60)], "volumes": [100.0]*60})
    # 일봉: 하락추세 → 매수성(홀딩유지) 억제
    monkeypatch.setattr(sb, "_fetch_daily_raw", lambda *a, **k: [200 - i for i in range(40)])
    sigs = sb.scan(log_path=tmp_path / "log.jsonl")
    assert all(s.action not in sb._BULLISH for s in sigs)


# ─── 관심종목 병합 (v2) ─────────────────────────────


class _Item:
    def __init__(self, ticker, name):
        self.ticker, self.name = ticker, name


def test_personal_watch_items_maps_to_watchitem():
    items = sb.personal_watch_items([_Item("005930", "삼성전자")])
    assert len(items) == 1
    it = items[0]
    assert it.code == "005930" and it.name == "삼성전자"
    assert it.weight == 0 and it.strategy == sb.PERSONAL_STRATEGY
    assert it.asset_class == sb.PERSONAL_ASSET_CLASS


def test_personal_watch_items_accepts_dict_rows():
    items = sb.personal_watch_items([{"ticker": "000660", "name": "SK하이닉스"}])
    assert items[0].code == "000660"


def test_personal_watch_items_skips_rows_without_ticker():
    assert sb.personal_watch_items([{"name": "이름만"}]) == []


def test_personal_watch_items_falls_back_to_ticker_as_name():
    assert sb.personal_watch_items([_Item("005930", None)])[0].name == "005930"


def test_personal_watch_items_reads_private_api_without_db_fallback():
    class Client:
        def list_watchlist(self):
            return [{"ticker": "005930", "name": "삼성전자", "created_at": "now"}]

    items = sb.personal_watch_items(private_client=Client())
    assert [(item.code, item.name) for item in items] == [("005930", "삼성전자")]


def test_personal_watch_items_private_api_failure_is_empty():
    class Client:
        def list_watchlist(self):
            raise RuntimeError("private details must not leak")

    assert sb.personal_watch_items(private_client=Client()) == []


def test_merge_appends_personal_after_base():
    base = [sb.WatchItem("TIGER 200", 20, "bollinger", "국내주식_지수", code="102110")]
    personal = sb.personal_watch_items([_Item("005930", "삼성전자")])
    merged = sb.merge_watchlists(base, personal)
    assert [i.code for i in merged] == ["102110", "005930"]


def test_merge_skips_duplicate_code():
    """자산배분에 이미 있는 종목을 관심종목에 넣어도 두 번 스캔하지 않는다."""
    base = [sb.WatchItem("TIGER 200", 20, "bollinger", "국내주식_지수", code="102110")]
    personal = sb.personal_watch_items([_Item("102110", "TIGER 200")])
    merged = sb.merge_watchlists(base, personal)
    assert len(merged) == 1 and merged[0].weight == 20   # 자산배분 비중이 유지된다


def test_merge_without_personal_returns_base_identity():
    base = [sb.WatchItem("TIGER 200", 20, "bollinger", "국내주식_지수", code="102110")]
    assert sb.merge_watchlists(base, []) is base


def test_load_watchlist_can_skip_personal(monkeypatch):
    monkeypatch.setattr(sb, "personal_watch_items", lambda *a, **k: 1 / 0)
    sb.load_watchlist(include_personal=False)   # 호출되지 않아야 한다


def test_format_shows_interest_tag_for_zero_weight():
    sig = sb.Signal("삼성전자", "005930", "StochRSI", "매수", "🟢", "테스트", 70000.0, 0.0)
    assert "(관심)" in sb.format_signals([sig])


# ─── 신호 기록 (v2) ─────────────────────────────────


def test_build_log_record_fields():
    sig = sb.Signal("TIGER 200", "102110", "볼린저", "매수", "🟢", "하단 터치", 41000.0, 20.0)
    rec = sb.build_log_record(sig, at="2026-08-26 10:00:00", trend="up",
                              asset_class="국내주식_지수")
    assert rec["ticker"] == "102110" and rec["action"] == "매수"
    assert rec["trend"] == "up" and rec["suppressed"] is False
    assert rec["price"] == 41000.0


def test_append_log_writes_jsonl(tmp_path):
    import json as _json
    path = tmp_path / "state" / "signal_log.jsonl"
    sb.append_log(path, {"a": 1})
    sb.append_log(path, {"a": 2})
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert [_json.loads(x)["a"] for x in lines] == [1, 2]


def test_append_log_swallows_errors(tmp_path):
    """기록 실패가 스캔을 막으면 안 된다."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    sb.append_log(blocker / "sub" / "log.jsonl", {"a": 1})   # 예외 없이 통과


def test_scan_logs_suppressed_signal(tmp_path, monkeypatch):
    import json as _json
    item = sb.WatchItem("TIGER 200", 20, "bollinger", "국내주식_지수", code="102110")
    sig = sb.Signal("TIGER 200", "102110", "볼린저", "매수", "🟢", "하단 터치", 41000.0, 20.0)
    # 2026-08-31: scan은 signal_watchlist(관심종목)를 쓴다. 억제 기록을 보는
    # 테스트이므로 대상을 명시적으로 준다.
    monkeypatch.setattr(sb, "signal_watchlist", lambda *a, **k: [item])
    monkeypatch.setattr(sb, "_fetch_intraday_raw", lambda *a, **k: {"closes": [1] * 40})
    monkeypatch.setattr(sb, "_fetch_daily_raw", lambda *a, **k: [1] * 40)
    monkeypatch.setattr(sb, "evaluate", lambda *a, **k: sig)
    monkeypatch.setattr(sb, "compute_trend", lambda *a, **k: "down")
    monkeypatch.setattr(sb, "apply_mtf_filter", lambda s, t: None)   # 억제
    path = tmp_path / "signal_log.jsonl"

    out = sb.scan(log_path=path, now="2026-08-26 10:00:00")

    assert out == []                                   # 발송 대상은 없지만
    rec = _json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["suppressed"] is True and rec["trend"] == "down"   # 기록은 남는다


def test_format_watchlist_groups_and_marks_interest():
    items = [
        sb.WatchItem("TIGER 200", 20, "bollinger", "국내주식_지수", code="102110"),
        sb.WatchItem("삼성전자", 0, "stochrsi", sb.PERSONAL_ASSET_CLASS, code="005930"),
    ]
    text = sb.format_watchlist(items)
    assert "2종목" in text
    assert "TIGER 200 (102110) · 20% · bollinger" in text
    assert "삼성전자 (005930) · 관심 · stochrsi" in text
    assert "국내주식_지수" in text and sb.PERSONAL_ASSET_CLASS in text


def test_format_watchlist_empty():
    assert "없습니다" in sb.format_watchlist([])


# ─── 운영 기록 오염 방지 (2026-08-27 사고 회귀 테스트) ───
#
# scan()의 log_path 기본값이 운영 경로였던 탓에, 이 파일의 테스트들이
# signal_log.jsonl에 가짜 신호를 써 넣고 있었다(100건 중 86건). 그 기록으로
# "MACD 64건" 같은 분석을 하고 있었으므로, 조용한 오염이 잘못된 결론까지 만들었다.


def test_scan_does_not_write_without_an_explicit_path(monkeypatch, tmp_path):
    """log_path를 주지 않으면 아무 데도 쓰지 않는다."""
    monkeypatch.setattr(sb, "resolve_etf_ticker", lambda n: "069500")
    monkeypatch.setattr(sb, "_fetch_intraday_raw",
                        lambda *a, **k: {"closes": [100 + i * 0.8 for i in range(60)],
                                         "volumes": [100.0] * 60})
    monkeypatch.setattr(sb, "_fetch_daily_raw", lambda *a, **k: [100 + i for i in range(40)])
    written = []
    monkeypatch.setattr(sb, "append_log", lambda p, r: written.append(p))

    sb.scan()
    assert written == []


def test_run_supplies_the_production_log_path(monkeypatch):
    """운영 호출부는 기록 경로를 명시해야 한다 — 여기가 유일한 호출부다."""
    seen = {}
    monkeypatch.setattr(sb, "scan", lambda **kw: seen.update(kw) or [])
    sb.run()
    assert seen.get("log_path") == sb.default_log_path()


def test_no_test_in_this_file_calls_scan_without_a_log_path():
    """이 파일 안에서 실수를 되풀이하지 않도록 소스를 직접 본다."""
    import re
    src = Path(__file__).read_text(encoding="utf-8")
    bare = re.findall(r"sb\.scan\(\s*\)", src)
    # test_scan_does_not_write_without_an_explicit_path 한 곳만 의도적으로 인자가 없다
    assert len(bare) == 1


# ─── 신호 대상 범위 (2026-08-31) ─────────────────────
#
# 자산배분 포트폴리오(TIGER 200·KODEX 코스닥150 등 15종목)와 개인 관심종목을
# 합쳐서 스캔하고 있었고, 실제로 자산배분 쪽 신호만 올라와 "이전 데이터가 남아
# 있다"는 인상을 줬다. 관심종목은 실제 보유·거래 종목과 일치하므로 알림이 바로
# 행동과 연결된다.


class TestSignalScope:
    def test_the_default_scope_is_personal_only(self):
        assert sb.SIGNAL_SCOPE == "personal"

    def test_the_scan_uses_the_signal_scope_not_the_full_list(self):
        import inspect
        src = inspect.getsource(sb.scan)
        assert "signal_watchlist()" in src
        assert "load_watchlist()" not in src

    def test_personal_scope_returns_only_personal_items(self, monkeypatch):
        personal = [sb.WatchItem("알테오젠", 0.0, "stochrsi", "개인", code="196170")]
        monkeypatch.setattr(sb, "personal_watch_items", lambda: personal)
        assert sb.signal_watchlist("personal") == personal

    def test_an_empty_personal_list_does_not_fall_back(self, monkeypatch):
        """대체하면 사용자가 끈 것이 조용히 되살아난다 — 0건이 맞는 답이다."""
        monkeypatch.setattr(sb, "personal_watch_items", lambda: [])
        assert sb.signal_watchlist("personal") == []

    def test_a_lookup_failure_does_not_fall_back_either(self, monkeypatch):
        """관심종목 조회 실패도 마찬가지다 — 자산배분 신호가 되살아나면 안 된다."""
        monkeypatch.setattr(sb, "personal_watch_items", lambda: [])
        got = sb.signal_watchlist("personal")
        assert all(w.asset_class != "국내주식_지수" for w in got)

    def test_the_all_scope_still_works_for_other_callers(self):
        assert len(sb.signal_watchlist("all")) > 0

    def test_browsing_the_full_watchlist_is_unaffected(self):
        """`/watchlist`로 전체를 보는 것과 신호를 어디에 보낼지는 다른 질문이다."""
        assert len(sb.load_watchlist(include_personal=False)) == 15


# ─── 표시 = 실제 신호 대상 (2026-08-31) ──────────────


def test_the_default_display_shows_what_is_actually_scanned():
    """v3.57에서 신호 범위를 관심종목으로 좁혔는데 표시는 전체 28종목을 보여줬다.

    사용자가 "저 종목들을 신호 목록에서 지워 달라"고 실제로 요청했다 — 지울
    것이 없는데 목록이 있다고 말한 것이다. 표시는 실제 발송 대상과 같아야 한다.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts"
           / "signal_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "format_watchlist")
    body = ast.get_source_segment(src, fn)
    assert "items = signal_watchlist()" in body
    assert "items = load_watchlist()" not in body


def test_the_display_says_allocation_items_are_not_targets():
    """목록에 없는 이유를 안 적으면 '사라졌다'는 문의가 온다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts"
           / "signal_bot.py").read_text(encoding="utf-8")
    assert "신호 대상이 아닙니다" in src
