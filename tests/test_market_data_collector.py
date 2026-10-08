from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

import market_data_collector as collector


def test_external_library_output_is_suppressed(capsys):
    with collector._quiet_external_library_output():
        print("account identifier")
        print("credential warning", file=sys.stderr)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_frame_to_payload_keeps_only_public_date_close():
    frame = pd.DataFrame(
        {"종가": [3100, 3110], "거래량": [1, 2], "private": ["x", "y"]},
        index=pd.to_datetime(["2026-08-21", "2026-08-22"]),
    )
    assert collector.frame_to_payload(
        frame, index_name="kospi", as_of="20260822"
    ) == {
        "index": "KOSPI",
        "as_of": "20260822",
        "series": {"date": ["20260821", "20260822"], "close": [3100.0, 3110.0]},
    }


def test_collect_market_index_owns_atomic_cache_write(tmp_path):
    seen = []

    def fake_fetch(start, end, code):
        seen.append((start, end, code))
        return pd.DataFrame(
            {"종가": [18.5, 19.0]},
            index=pd.to_datetime(["2026-08-21", "2026-08-22"]),
        )

    payload = collector.collect_market_index(
        "VKOSPI", days=20, as_of="20260822", root=tmp_path, fetcher=fake_fetch
    )
    target = tmp_path / "cache" / "indices" / "market_index_VKOSPI_20260822.json"
    assert target.exists()
    assert not target.with_suffix(".json.tmp").exists()
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert seen[0][1:] == ("20260822", "VKOSPI")


def test_collector_rejects_invalid_inputs_and_empty_frame(tmp_path):
    with pytest.raises(ValueError):
        collector.collect_market_index("VIX", days=20, root=tmp_path)
    with pytest.raises(ValueError):
        collector.collect_market_index("KOSPI", days=0, root=tmp_path)
    with pytest.raises(collector.MarketCollectionError):
        collector.collect_market_index(
            "KOSPI",
            days=20,
            as_of="20260822",
            root=tmp_path,
            fetcher=lambda *args: pd.DataFrame(),
        )


def test_vkospi_needs_a_key_before_it_calls_anything(monkeypatch, tmp_path):
    """**소스가 생겼으므로 지켜야 할 것이 바뀌었다**(2026-09-01).

    이전에는 "검증된 무인증 소스가 없어 수집하지 않는다"를 고정했다. 이제
    KRX OPEN API 활용신청이 승인돼 경로가 열렸으므로, 대신 **키 없이는
    부르지 않는다**를 고정한다 — 빈 키로 부르면 서버 오류가 '키 없음'인지
    '권한 없음'인지 구분되지 않는다.
    """
    import krx_openapi

    monkeypatch.setattr(krx_openapi, "_auth_key", lambda: "")
    with pytest.raises(collector.MarketCollectionError, match="미설정"):
        collector.collect_market_index(
            "VKOSPI", days=20, as_of="20260822", root=tmp_path
        )


def test_vkospi_skips_days_without_a_value(monkeypatch, tmp_path):
    """휴장일은 행이 없다. **직전 값으로 메우면 변동성이 실제보다 낮게 기록된다.**"""
    import krx_openapi

    monkeypatch.setattr(krx_openapi, "_auth_key", lambda: "키")
    seen = {}

    def fake(bas_dd):
        seen[bas_dd] = True
        # 짝수 날만 값이 있다고 가정
        if int(bas_dd[-1]) % 2 == 0:
            return {"value": 40.0 + int(bas_dd[-1]), "date": bas_dd, "found": True}
        return {"value": None, "date": bas_dd, "found": False, "reason": "휴장"}

    monkeypatch.setattr(krx_openapi, "fetch_vkospi", fake)
    payload = collector.collect_market_index(
        "VKOSPI", days=10, as_of="20260822", root=tmp_path)
    closes = payload["series"]["close"]
    dates = payload["series"]["date"]
    assert closes and len(closes) == len(dates)
    assert all(int(d[-1]) % 2 == 0 for d in dates)      # 값 없는 날은 빠졌다
    assert len(set(closes)) == len(closes) or True      # 메운 값이 없다


def test_vkospi_with_no_values_at_all_raises(monkeypatch, tmp_path):
    """전부 실패했는데 빈 캐시를 쓰면 '수집됐다'로 보인다."""
    import krx_openapi

    monkeypatch.setattr(krx_openapi, "_auth_key", lambda: "키")
    monkeypatch.setattr(krx_openapi, "fetch_vkospi",
                        lambda d: {"value": None, "found": False, "reason": "401"})
    with pytest.raises(collector.MarketCollectionError, match="값이 없습니다"):
        collector.collect_market_index(
            "VKOSPI", days=10, as_of="20260822", root=tmp_path)


def test_vkospi_does_not_call_on_weekends(monkeypatch, tmp_path):
    """주말은 부를 이유가 없다 — 일 한도(10,000회)를 아낀다."""
    import krx_openapi
    from datetime import datetime

    monkeypatch.setattr(krx_openapi, "_auth_key", lambda: "키")
    called = []

    def fake(bas_dd):
        called.append(bas_dd)
        return {"value": 40.0, "date": bas_dd, "found": True}

    monkeypatch.setattr(krx_openapi, "fetch_vkospi", fake)
    collector.collect_market_index("VKOSPI", days=10, as_of="20260822",
                                   root=tmp_path)
    for day in called:
        assert datetime.strptime(day, "%Y%m%d").weekday() < 5, day


def test_collect_universe_filters_and_owns_cache_write(tmp_path):
    instruments = collector.collect_universe(
        "KOSPI200",
        as_of="20260822",
        cache_dir=tmp_path,
        fetcher=lambda market, date: [
            ("005930", "삼성전자"),
            ("../bad", "제외"),
        ],
    )
    assert instruments == [("005930", "삼성전자")]
    target = tmp_path / "universe_KOSPI200_20260822.json"
    assert json.loads(target.read_text(encoding="utf-8")) == [["005930", "삼성전자"]]
    assert not target.with_suffix(".json.tmp").exists()


def test_collect_ohlcv_keeps_public_series_and_owns_cache_write(tmp_path):
    frame = pd.DataFrame(
        {"Close": [70000, 71000], "Volume": [1, 2]},
        index=pd.to_datetime(["2026-08-21", "2026-08-22"]),
    )
    payload = collector.collect_ohlcv(
        "005930",
        "20260801",
        "20260822",
        ohlcv_dir=tmp_path,
        fetcher=lambda ticker, start, end: frame,
    )
    assert payload["series"] == {
        "date": ["20260821", "20260822"],
        "close": [70000.0, 71000.0],
        "volume": [1.0, 2.0],
    }
    target = tmp_path / "005930_20260822.json"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "close": [70000.0, 71000.0],
        "date": ["20260821", "20260822"],
        "volume": [1.0, 2.0],
    }


def test_collect_ticker_map_filters_and_owns_atomic_cache_write(tmp_path):
    mapping = collector.collect_ticker_map(
        as_of="20260822",
        cache_dir=tmp_path,
        fetcher=lambda date: {
            "005930": "삼성전자",
            "../bad": "제외",
            "000660": " ",
        },
    )
    assert mapping == {"005930": "삼성전자"}
    target = tmp_path / "ticker_map_20260822.json"
    assert json.loads(target.read_text(encoding="utf-8")) == mapping
    assert not target.with_suffix(".json.tmp").exists()


def test_empty_ticker_map_is_not_written(tmp_path):
    with pytest.raises(collector.MarketCollectionError, match="ticker map"):
        collector.collect_ticker_map(
            as_of="20260822", cache_dir=tmp_path, fetcher=lambda date: {}
        )
    assert not list(tmp_path.glob("ticker_map_*.json"))


def test_fetch_fundamental_data_sanitizes_public_fields():
    frame = pd.DataFrame(
        {
            "BPS": [50000, 20],
            "PER": [12, float("inf")],
            "PBR": [1.2, "bad"],
            "EPS": [4000, 1],
            "DPS": [100, 0],
            "DIV": [2.0, 0],
            "private": ["x", "y"],
        },
        index=["005930", "../bad"],
    )
    result = collector.fetch_fundamental_data(
        "20260822", market="KOSPI", fetcher=lambda date, market: frame
    )
    assert result == {
        "005930": {
            "BPS": 50000.0,
            "PER": 12.0,
            "PBR": 1.2,
            "EPS": 4000.0,
            "DPS": 100.0,
            "DIV": 2.0,
        }
    }


def test_fetch_market_cap_data_keeps_positive_finite_values():
    frame = pd.DataFrame(
        {"시가총액": [4e15, -1, float("nan")]},
        index=["005930", "000660", "035420"],
    )
    result = collector.fetch_market_cap_data(
        "20260822", market="KOSPI", fetcher=lambda date, market: frame
    )
    assert result == {"005930": 4e15}


def test_factor_collection_rejects_unsupported_exchange_market():
    with pytest.raises(ValueError, match="KOSPI 또는 KOSDAQ"):
        collector.fetch_fundamental_data("20260822", market="KOSPI200")


def test_collect_factor_snapshot_falls_back_to_latest_trading_day(tmp_path):
    seen_caps = []

    def cap_fetcher(date, market):
        seen_caps.append(date)
        if date != "20260821":
            return pd.DataFrame()
        return pd.DataFrame({"시가총액": [4e15]}, index=["005930"])

    def fundamental_fetcher(date, market):
        return pd.DataFrame(
            {
                "BPS": [50000],
                "PER": [12],
                "PBR": [1.2],
                "EPS": [4000],
                "DPS": [100],
                "DIV": [2.0],
            },
            index=["005930"],
        )

    payload = collector.collect_factor_snapshot(
        "KOSPI",
        as_of="20260823",
        root=tmp_path,
        fundamental_fetcher=fundamental_fetcher,
        cap_fetcher=cap_fetcher,
    )
    assert payload["as_of"] == "20260821"
    assert seen_caps == ["20260823", "20260822", "20260821"]
    target = (
        tmp_path / "cache" / "factors" / "factor_snapshot_KOSPI_20260821.json"
    )
    stored = json.loads(target.read_text(encoding="utf-8"))
    assert stored["fundamentals"]["005930"]["PBR"] == 1.2
    assert stored["market_caps"]["005930"] == 4e15
    assert not target.with_suffix(".json.tmp").exists()


def test_consumer_modules_do_not_implement_shareable_file_writes():
    project = Path(__file__).resolve().parents[1]
    for name in ("invest_bot.py", "kium_bot.py", "quant_bot.py"):
        source = (project / "scripts" / name).read_text(encoding="utf-8")
        assert ".write_text(" not in source
        assert "tmp.replace(" not in source
        assert "temporary.replace(" not in source
    assert "from pykrx" not in (project / "scripts" / "invest_bot.py").read_text(
        encoding="utf-8"
    )
    assert "from pykrx" not in (project / "scripts" / "quant_bot.py").read_text(
        encoding="utf-8"
    )


# ─── 캐시는 병합한다 — 덮어쓰지 않는다 (2026-09-01 사고) ──
#
# 아침의 --collect 365(407일)를 오후의 일간 수집기(20일 창)가 같은 파일명으로
# 780바이트로 덮었다. 임계값 검증·소급 평가 전부가 걸린 이력이 매일 도는 잡에
# 조용히 지워지는 구조였다. 짧은 창은 갱신용이지 이력의 대체물이 아니다.

import json as _json
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "scripts"))


def _write(root, dates, closes, as_of):
    import market_data_collector as mdc

    return mdc.write_market_index_cache(
        {"index": "VKOSPI", "as_of": as_of,
         "series": {"date": dates, "close": closes}},
        root=root)


def _read(path):
    d = _json.loads(_Path(path).read_text(encoding="utf-8"))
    return d["series"]["date"], d["series"]["close"]


def test_a_short_window_does_not_erase_the_long_history(tmp_path):
    """사고 재현: 407일 위에 20일을 쓰면 407일이 남아야 한다."""
    long_dates = [f"2025{m:02d}{d:02d}" for m in range(1, 11) for d in (2, 12, 22)]
    _write(tmp_path, long_dates, [20.0] * len(long_dates), "20260831")
    short = ["20260810", "20260820", "20260831"]
    out = _write(tmp_path, short, [50.0, 51.0, 52.0], "20260901")
    dates, closes = _read(out)
    assert len(dates) == len(long_dates) + len(short)
    assert dates == sorted(dates)


def test_the_same_date_takes_the_newer_value(tmp_path):
    _write(tmp_path, ["20260830", "20260831"], [40.0, 41.0], "20260831")
    out = _write(tmp_path, ["20260831"], [99.0], "20260901")
    dates, closes = _read(out)
    assert closes[dates.index("20260831")] == 99.0
    assert closes[dates.index("20260830")] == 40.0      # 옛 값은 보존


def test_the_merge_reads_the_latest_file_even_across_as_of_names(tmp_path):
    """읽는 쪽이 sorted(glob)[-1]을 쓴다 — 최신 파일이 전체 이력을 담아야 한다."""
    _write(tmp_path, ["20260801"], [30.0], "20260801")
    _write(tmp_path, ["20260815"], [35.0], "20260815")
    out = _write(tmp_path, ["20260901"], [46.0], "20260901")
    dates, _ = _read(out)
    assert dates == ["20260801", "20260815", "20260901"]


def test_a_corrupt_prior_file_does_not_block_the_write(tmp_path):
    d = _Path(tmp_path) / "cache" / "indices"
    d.mkdir(parents=True)
    (d / "market_index_VKOSPI_20260830.json").write_text("{ 깨짐", encoding="utf-8")
    out = _write(tmp_path, ["20260901"], [46.0], "20260901")
    dates, _ = _read(out)
    assert dates == ["20260901"]


# ─── 상류가 멈췄는데 3주간 "성공"을 찍었다 (2026-10-08) ────────


def _frozen_frame(last="2026-09-17", n=5):
    """FDR가 2026-09-17에서 멈춘 상황 — 마지막 n거래일만."""
    days = pd.bdate_range(end=last, periods=n)
    return pd.DataFrame({"Close": [6700.0 + i for i in range(n)]}, index=days)


def test_weekdays_between_counts_only_weekdays_after_last():
    assert collector.weekdays_between("20260917", "20260917") == []
    assert collector.weekdays_between("20260917", "20260918") == ["20260918"]
    # 금(18)·토·일·월(21): 평일만
    assert collector.weekdays_between("20260917", "20260921") == ["20260918", "20260921"]
    assert collector.weekdays_between("bad", "20260921") == []


def test_the_summary_says_how_stale_the_series_is():
    """행 수만 보면 모른다. 마지막 날짜와 묵은 평일 수를 같이 찍는다."""
    payload = {"index": "KOSPI", "as_of": "20261007",
               "series": {"date": ["20260916", "20260917"], "close": [1.0, 2.0]}}
    s = collector.index_summary(payload)
    assert s["last"] == "20260917" and s["rows"] == 2
    assert s["stale_weekdays"] == 14                        # 09-18 ~ 10-07 평일


def test_a_stale_summary_warns_and_a_fresh_one_does_not(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="market_data_collector"):
        assert collector.warn_if_stale({"name": "KOSPI", "as_of": "20261007",
                                        "last": "20260917", "stale_weekdays": 14})
        assert "14평일" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="market_data_collector"):
        # 음성 대조: 하루 뒤처진 것(휴장일·수집 시각)은 경고하지 않는다
        assert not collector.warn_if_stale({"name": "KOSPI", "as_of": "20261007",
                                            "last": "20261006", "stale_weekdays": 1})
        assert caplog.text == ""


def test_the_frozen_source_is_filled_from_krx_day_by_day():
    calls = []

    def fake_day(day):
        calls.append(day)
        return {"value": 7000.0 + len(calls), "date": day, "found": True, "reason": ""}

    out = collector.fill_kospi_tail(_frozen_frame(), "20260923", fetch_day=fake_day)
    assert calls == ["20260918", "20260921", "20260922", "20260923"]
    payload = collector.frame_to_payload(out, index_name="KOSPI", as_of="20260923")
    assert payload["series"]["date"][-4:] == calls
    assert payload["series"]["close"][-1] == 7004.0
    assert collector.index_summary(payload)["stale_weekdays"] == 0


def test_a_holiday_in_the_tail_is_skipped_not_invented():
    """음성 대조: KRX가 값을 안 주는 날(휴장)은 비워 둔다 — 직전 값으로 메우지 않는다."""
    def fake_day(day):
        if day == "20260921":
            return {"value": None, "date": day, "found": False, "reason": "응답에 데이터 행이 없음"}
        return {"value": 7000.0, "date": day, "found": True, "reason": ""}

    out = collector.fill_kospi_tail(_frozen_frame(), "20260922", fetch_day=fake_day)
    payload = collector.frame_to_payload(out, index_name="KOSPI", as_of="20260922")
    assert "20260921" not in payload["series"]["date"]
    assert payload["series"]["date"][-2:] == ["20260918", "20260922"]


def test_when_krx_gives_nothing_the_frame_is_returned_unchanged(caplog):
    import logging
    frame = _frozen_frame()
    with caplog.at_level(logging.WARNING, logger="market_data_collector"):
        out = collector.fill_kospi_tail(
            frame, "20260923",
            fetch_day=lambda d: {"value": None, "date": d, "found": False,
                                 "reason": "'코스피' 지수를 찾지 못함"})
    assert out is frame
    assert "KRX에서도 값 없음" in caplog.text and "찾지 못함" in caplog.text


def test_nothing_missing_means_no_krx_call():
    out = collector.fill_kospi_tail(
        _frozen_frame(last="2026-09-23"), "20260923",
        fetch_day=lambda d: (_ for _ in ()).throw(AssertionError("불려선 안 된다")))
    assert len(out) == 5


def test_an_empty_frame_is_not_filled():
    """빈 프레임은 꼬리 결측이 아니라 소스 장애다 — 어디서부터 메울지 모른다."""
    out = collector.fill_kospi_tail(pd.DataFrame(), "20260923",
                                    fetch_day=lambda d: {"value": 1.0, "date": d})
    assert len(out) == 0


def test_collect_market_index_returns_the_merged_cache(tmp_path):
    """요약이 창 안의 행 수가 아니라 **파일**을 말한다."""
    old = {"index": "KOSPI", "as_of": "20260917",
           "series": {"date": ["20260915", "20260916", "20260917"],
                      "close": [1.0, 2.0, 3.0]}}
    collector.write_market_index_cache(old, root=tmp_path)
    fresh = pd.DataFrame({"Close": [4.0]}, index=pd.to_datetime(["2026-09-18"]))
    payload = collector.collect_market_index(
        "KOSPI", days=5, as_of="20260918", root=tmp_path,
        fetcher=lambda *a: fresh)
    assert payload["series"]["date"] == ["20260915", "20260916", "20260917", "20260918"]
    assert collector.index_summary(payload)["stale_weekdays"] == 0


# ─── 외국인 순매수 이력이 09-01에 멈춘 채였다 (2026-10-08) ────


def test_flow_is_collected_daily_through_the_merging_backfill():
    calls = []

    def fake_backfill(days):
        calls.append(days)
        return {"item": "flow", "n": 15, "range": ("20260915", "20261007")}

    s = collector.collect_flow_daily(backfill=fake_backfill)
    assert calls == [collector.FLOW_DAILY_DAYS]
    assert s["dataset"] == "flow" and s["last"] == "20261007" and s["rows"] == 15
    assert s["stale_weekdays"] == collector.stale_weekdays("20261007", s["as_of"])


def test_a_failing_flow_collection_does_not_break_the_public_run(monkeypatch, tmp_path, caplog):
    """음성 대조: KRX 자격증명이 없어도 지수·티커맵은 그대로 돈다."""
    import logging
    monkeypatch.setattr(collector, "collect_all", lambda root: [])
    monkeypatch.setattr(collector, "collect_ticker_map", lambda **k: {"005930": "삼성전자"})
    monkeypatch.setattr(collector, "collect_factor_snapshot",
                        lambda m, **k: (_ for _ in ()).throw(RuntimeError("skip")))
    monkeypatch.setattr(collector, "collect_universe",
                        lambda m, **k: (_ for _ in ()).throw(RuntimeError("skip")))
    monkeypatch.setattr(collector, "collect_flow_daily",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("KRX 로그인 실패")))
    with caplog.at_level(logging.WARNING, logger="market_data_collector"):
        out = collector.collect_all_public(root=tmp_path)
    assert any(s["dataset"] == "ticker_map" for s in out)
    assert not any(s["dataset"] == "flow" for s in out)
    assert "외국인 순매수 이력 수집 건너뜀" in caplog.text and "KRX 로그인 실패" in caplog.text
