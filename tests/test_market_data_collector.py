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


def test_default_vkospi_collector_fails_closed_without_verified_source(tmp_path):
    with pytest.raises(collector.MarketCollectionError, match="검증된 무인증"):
        collector.collect_market_index(
            "VKOSPI", days=20, as_of="20260822", root=tmp_path
        )


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
