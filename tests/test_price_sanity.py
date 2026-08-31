"""test_price_sanity.py — 진입가 스테일 탐지(순수) 검증 (hermetic)."""
from __future__ import annotations

from pathlib import Path

import price_sanity as ps

TODAY = "20260608"

# 실제 사고 데이터: 06-01 종가 == 06-08 기록된 진입가
CAL = ["20260601", "20260602", "20260603", "20260604", "20260605", "20260608"]
SERIES = {
    "010120": list(zip(CAL, [267000, 250000, 240000, 232000, 224000, 208000])),
    "006800": list(zip(CAL, [60900, 58000, 55000, 53000, 51000, 49750])),
    "009150": list(zip(CAL, [2005000, 1950000, 1900000, 1820000, 1750000, 1664000])),
}


def _item(ticker, price, name="종목"):
    return {"ticker": ticker, "name": name, "price": price}


# ─── 정확 일치 ───────────────────────────────────────


def test_exact_match_finds_the_stale_date():
    assert ps.exact_match_dates(267000, SERIES["010120"], today=TODAY) == ["20260601"]


def test_today_is_not_a_stale_match():
    """당일 종가와 같은 것은 정상이다."""
    assert ps.exact_match_dates(208000, SERIES["010120"], today=TODAY) == []


def test_no_match_for_an_ordinary_price():
    assert ps.exact_match_dates(211000, SERIES["010120"], today=TODAY) == []


def test_missing_price_matches_nothing():
    for bad in (0, None, -1):
        assert ps.exact_match_dates(bad, SERIES["010120"], today=TODAY) == []


# ─── 배치 판정 ───────────────────────────────────────


def test_the_actual_incident_is_caught():
    """2026-06-08 사고 재현 — 3종목이 모두 06-01 종가와 일치."""
    v = ps.inspect_batch([_item("010120", 267000), _item("006800", 60900),
                          _item("009150", 2005000)], SERIES, TODAY)
    assert v["stale"] and v["stale_date"] == "20260601" and len(v["hits"]) == 3


def test_the_corrected_rebuy_is_not_flagged():
    """13:01 정상 재매수는 걸리면 안 된다."""
    v = ps.inspect_batch([_item("010120", 211000), _item("006800", 51200),
                          _item("009150", 1710000)], SERIES, TODAY)
    assert not v["stale"]


def test_single_coincidence_does_not_block():
    """한 종목이 우연히 과거 종가와 같을 수는 있다 — 그걸로 배치를 막지 않는다."""
    v = ps.inspect_batch([_item("010120", 267000), _item("006800", 51200)],
                         SERIES, TODAY)
    assert not v["stale"]


def test_two_hits_on_the_same_date_is_enough():
    v = ps.inspect_batch([_item("010120", 267000), _item("006800", 60900)],
                         SERIES, TODAY)
    assert v["stale"] and len(v["hits"]) == 2


def test_hits_must_share_one_date():
    """서로 다른 과거 날짜에 하나씩 걸리는 것은 스테일 신호가 아니다."""
    v = ps.inspect_batch([_item("010120", 267000), _item("006800", 51000)],
                         SERIES, TODAY)
    assert not v["stale"]


def test_min_hits_is_configurable():
    v = ps.inspect_batch([_item("010120", 267000)], SERIES, TODAY, min_hits=1)
    assert v["stale"]


def test_unknown_tickers_are_not_judged():
    """비교할 일봉이 없으면 판정하지 않는다 — 모르는 것을 이상으로 몰지 않는다."""
    v = ps.inspect_batch([_item("999999", 1000), _item("888888", 2000)], {}, TODAY)
    assert not v["stale"] and v["checked"] == 0 and "판정하지 않음" in v["reason"]


def test_empty_batch_is_safe():
    v = ps.inspect_batch([], SERIES, TODAY)
    assert not v["stale"] and v["checked"] == 0


# ─── 괴리 경고 ───────────────────────────────────────


def test_deviation_warns_but_does_not_block():
    """상한가는 정상이다 — 경고만 남기고 막지 않는다."""
    v = ps.inspect_batch([_item("010120", 270400)], SERIES, TODAY)   # +30% 상한가
    assert not v["stale"] and len(v["warnings"]) == 1
    assert v["warnings"][0]["deviation"] == 30.0


def test_small_deviation_is_silent():
    v = ps.inspect_batch([_item("010120", 210000)], SERIES, TODAY)
    assert v["warnings"] == []


def test_ordinary_volatility_does_not_warn():
    """이 시장은 일간 표준편차가 ~7%다. 2σ에서 경고하면 곧 무시하게 된다."""
    v = ps.inspect_batch([_item("010120", 240000)], SERIES, TODAY)   # +15.4%
    assert v["warnings"] == []


def test_deviation_pct_handles_missing_values():
    assert ps.deviation_pct(100, None) is None
    assert ps.deviation_pct(0, 100) is None
    assert ps.deviation_pct(100, 0) is None


# ─── 메시지 ──────────────────────────────────────────


def test_block_message_explains_the_stake_and_scope():
    v = ps.inspect_batch([_item("010120", 267000, "LS ELECTRIC"),
                          _item("006800", 60900, "미래에셋증권")], SERIES, TODAY)
    text = ps.format_block(v)
    assert "오염" in text and "매도는 막지 않습니다" in text
    assert "LS ELECTRIC" in text


def test_block_message_empty_when_clean():
    v = ps.inspect_batch([_item("010120", 211000)], SERIES, TODAY)
    assert ps.format_block(v) == ""


def test_warning_message_says_it_does_not_block():
    v = ps.inspect_batch([_item("010120", 270400)], SERIES, TODAY)
    assert "차단하지 않습니다" in ps.format_warnings(v)


def test_warning_message_empty_when_clean():
    assert ps.format_warnings({"warnings": []}) == ""


# ─── 캐시 정렬 경계 ──────────────────────────────────


def test_series_longer_than_calendar_is_refused(tmp_path, monkeypatch):
    """어긋난 정렬로 판정하느니 판정하지 않는 편이 낫다."""
    monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
    (tmp_path / "ohlcv").mkdir(parents=True)
    (tmp_path / "ohlcv" / "010120_20260608.json").write_text(
        '{"close": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}', encoding="utf-8")
    assert ps.load_series("010120", CAL) == []


def test_series_with_unknown_as_of_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
    (tmp_path / "ohlcv").mkdir(parents=True)
    (tmp_path / "ohlcv" / "010120_20991231.json").write_text(
        '{"close": [1, 2]}', encoding="utf-8")
    assert ps.load_series("010120", CAL) == []


def test_series_aligns_to_the_end_of_the_calendar(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
    (tmp_path / "ohlcv").mkdir(parents=True)
    (tmp_path / "ohlcv" / "010120_20260608.json").write_text(
        '{"close": [224000, 208000]}', encoding="utf-8")
    assert ps.load_series("010120", CAL) == [("20260605", 224000), ("20260608", 208000)]


def test_missing_cache_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
    assert ps.load_series("010120", CAL) == []
    assert ps.trading_calendar() == []


# ─── 배선 검증 ───────────────────────────────────────


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_both_auto_buy_paths_check_price_sanity():
    """스캔가 관문 2곳(키움·콴텍) + 체결가 관문 1곳(공용 헬퍼) = 3.

    v3.61에서 **체결가**를 따로 보는 관문을 더했다. 스캔가 관문은 "신호가 언제
    것인가"를 보고, 체결가 관문은 **실제로 기록될 가격**을 본다. 2026-06-08
    사고에서 손실을 만든 것은 스캔가가 아니라 체결가였다.
    """
    import ast

    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 속성 참조(`_ps.check_batch`)와 이름 참조(`_fill_price_stale_block`)를 센다.
    # 문자열로 세면 **독스트링에 걸린다** — 실제로 두 번 그렇게 틀렸다.
    attrs = [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]
    assert attrs.count("check_batch") == 3, attrs.count("check_batch")
    assert names.count("_fill_price_stale_block") == 2   # 호출 2(정의는 Name이 아니다)


def test_sell_paths_are_not_blocked():
    """보유를 정리할 길까지 막으면 더 위험하다."""
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    for marker in ("check_batch",):
        for i in range(src.count(marker)):
            pass
    # 매도 기록 지점 근처에 관문이 없어야 한다
    sell_idx = [i for i in range(len(src)) if src.startswith("record_sell", i)]
    for i in sell_idx:
        assert "check_batch" not in src[max(0, i - 600):i]


# ─── 일봉 캐시 로드 (2026-08-31) ─────────────────────
#
# 캐시 형식이 바뀌어 `date` 배열이 생겼는데 로더는 "종가만 있고 날짜가 없다"는
# 옛 전제로 파일명 기준일 + 거래일 달력을 역산하고 있었다. 달력의 출처(지수 캐시)가
# 08-28까지인데 OHLCV는 08-31까지 받아, **전 종목이 0건**이 되고 자산곡선
# 커버리지가 18%로 떨어졌다. 최신 파일 하나만 보고 실패하면 그대로 포기하는
# 구조라 파일이 19개 있어도 소용없었다.

import json as _json


def _write_cache(tmp_path, ticker, as_of, closes, dates=None):
    d = tmp_path / "ohlcv"
    d.mkdir(exist_ok=True)
    payload = {"close": closes}
    if dates is not None:
        payload["date"] = dates
    (d / f"{ticker}_{as_of}.json").write_text(
        _json.dumps(payload), encoding="utf-8")


class TestLoadSeriesDates:
    def test_dates_in_the_cache_are_used_directly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
        _write_cache(tmp_path, "005930", "20260831",
                     [100.0, 110.0], ["20260828", "20260831"])
        # 달력에 20260831이 없어도 읽혀야 한다
        assert ps.load_series("005930", ["20260827", "20260828"]) == [
            ("20260828", 100.0), ("20260831", 110.0)]

    def test_the_old_format_still_works(self, tmp_path, monkeypatch):
        """날짜 없는 옛 캐시는 달력 역산으로 계속 읽는다."""
        monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
        _write_cache(tmp_path, "005930", "20260828", [100.0, 110.0])
        assert ps.load_series("005930", ["20260827", "20260828"]) == [
            ("20260827", 100.0), ("20260828", 110.0)]

    def test_it_falls_back_to_an_earlier_file(self, tmp_path, monkeypatch):
        """최신 파일 하나가 안 읽힌다고 19개를 다 버리면 안 된다."""
        monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
        _write_cache(tmp_path, "005930", "20260828", [100.0, 110.0])   # 옛 형식·달력 안
        _write_cache(tmp_path, "005930", "20260831", [1.0, 2.0, 3.0])  # 달력 밖·날짜 없음
        got = ps.load_series("005930", ["20260827", "20260828"])
        assert got == [("20260827", 100.0), ("20260828", 110.0)]

    def test_a_broken_file_is_skipped_not_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
        (tmp_path / "ohlcv").mkdir()
        (tmp_path / "ohlcv" / "005930_20260831.json").write_text("{망가짐", encoding="utf-8")
        _write_cache(tmp_path, "005930", "20260828", [100.0], ["20260828"])
        assert ps.load_series("005930", ["20260828"]) == [("20260828", 100.0)]

    def test_a_length_mismatch_falls_back_to_the_calendar(self, tmp_path, monkeypatch):
        """date와 close 길이가 다르면 짝이 어긋난 것 — 그 배열을 믿지 않는다."""
        monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
        _write_cache(tmp_path, "005930", "20260828", [100.0, 110.0], ["20260828"])
        assert ps.load_series("005930", ["20260827", "20260828"]) == [
            ("20260827", 100.0), ("20260828", 110.0)]

    def test_no_cache_is_an_empty_series(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
        (tmp_path / "ohlcv").mkdir()
        assert ps.load_series("005930", ["20260828"]) == []


# ─── 직전 종가는 스테일이 아니다 (2026-08-31) ────────


def _series(*pairs):
    return list(pairs)


def test_the_previous_close_is_not_stale():
    """일봉 스캔은 직전 종가를 들고 온다 — 그게 정상이다.

    2026-08-31: 월요일 콴텍 리밸런싱에서 7종목이 금요일(20260828) 종가와
    일치한다고 배치 전체가 막혔다. 스캔이 일봉으로 도는 한 **항상** 그렇다.
    """
    s = _series(("20260826", 100.0), ("20260827", 110.0), ("20260828", 120.0))
    assert ps.exact_match_dates(120.0, s, today="20260831") == []


def test_an_older_close_is_still_stale():
    """2026-06-08 사고는 5거래일 전 종가였다 — 이건 계속 잡아야 한다."""
    s = _series(("20260601", 100.0), ("20260602", 110.0), ("20260605", 120.0))
    assert ps.exact_match_dates(100.0, s, today="20260608") == ["20260601"]


def test_two_days_back_is_still_stale():
    """2026-07-22 사고는 2거래일 전(20260720)이었다."""
    s = _series(("20260720", 100.0), ("20260721", 110.0))
    assert ps.exact_match_dates(100.0, s, today="20260722") == ["20260720"]


def test_today_is_still_excluded():
    s = _series(("20260828", 120.0), ("20260831", 130.0))
    assert ps.exact_match_dates(130.0, s, today="20260831") == []


def test_the_previous_trading_day_is_the_latest_one_before_today():
    s = _series(("20260826", 1.0), ("20260827", 2.0), ("20260828", 3.0))
    assert ps.previous_trading_day(s, "20260831") == "20260828"
    assert ps.previous_trading_day(s, "20260827") == "20260826"


def test_no_history_yields_no_previous_day():
    assert ps.previous_trading_day([], "20260831") is None
    assert ps.previous_trading_day([("20260831", 1.0)], "20260831") is None


def test_the_skip_can_be_turned_off_for_forensics():
    """소급 점검에서는 직전 종가까지 보고 싶을 수 있다."""
    s = _series(("20260828", 120.0))
    assert ps.exact_match_dates(120.0, s, today="20260831",
                                skip_previous_close=False) == ["20260828"]


def test_a_gap_over_a_weekend_still_skips_only_one_day():
    """금요일 종가는 직전 거래일이다 — 주말이 끼어도 하루만 건너뛴다."""
    s = _series(("20260827", 100.0), ("20260828", 120.0))
    assert ps.exact_match_dates(120.0, s, today="20260831") == []
    assert ps.exact_match_dates(100.0, s, today="20260831") == ["20260827"]
