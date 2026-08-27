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
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    assert src.count("check_batch") == 2


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
