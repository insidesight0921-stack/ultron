from __future__ import annotations

import json

import pytest

from shareable_store import (
    ShareableNotFound,
    ShareableStore,
    ShareableStoreError,
    normalize_exchange_market,
    normalize_market,
    normalize_market_index,
    normalize_query,
    normalize_ticker,
)


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_normalize_ticker_accepts_only_six_digits():
    assert normalize_ticker("005930") == "005930"
    for value in ("5930", "005930.json", "../005930", "ABCDEF", "005930/../../"):
        with pytest.raises(ValueError):
            normalize_ticker(value)


def test_normalize_query():
    assert normalize_query("  삼성   전자 ") == "삼성 전자"
    with pytest.raises(ValueError):
        normalize_query("  ")
    with pytest.raises(ValueError):
        normalize_query("가" * 101)


def test_normalize_market_allowlist():
    assert normalize_market("kospi200") == "KOSPI200"
    assert normalize_market("KOSPI200+KOSDAQ150") == "KOSPI200+KOSDAQ150"
    for value in ("", "KOSPI", "../KOSPI200", "KOSPI200.json"):
        with pytest.raises(ValueError):
            normalize_market(value)


def test_normalize_market_index_allowlist():
    assert normalize_market_index("kospi") == "KOSPI"
    assert normalize_market_index("VKOSPI") == "VKOSPI"
    for value in ("", "KOSPI200", "../KOSPI", "VIX"):
        with pytest.raises(ValueError):
            normalize_market_index(value)


def test_normalize_exchange_market_allowlist():
    assert normalize_exchange_market("kospi") == "KOSPI"
    assert normalize_exchange_market("KOSDAQ") == "KOSDAQ"
    for value in ("", "KOSPI200", "../KOSPI"):
        with pytest.raises(ValueError):
            normalize_exchange_market(value)


def test_get_instrument_uses_latest_map_and_allowlisted_fields(tmp_path):
    root = tmp_path / "shareable"
    _write_json(root / "cache" / "ticker_map_20260821.json", {"005930": "옛이름"})
    _write_json(root / "cache" / "ticker_map_20260822.json", {"005930": "삼성전자"})
    _write_json(
        root / "cache" / "corp_codes.json",
        {
            "005930": {
                "corp_code": "00126380",
                "corp_name": "삼성전자",
                "chat_id": "must-not-leak",
            }
        },
    )

    result = ShareableStore(root).get_instrument("005930")

    assert result == {
        "ticker": "005930",
        "name": "삼성전자",
        "corp_code": "00126380",
        "as_of": "20260822",
    }
    assert "chat_id" not in result


def test_get_instrument_missing(tmp_path):
    root = tmp_path / "shareable"
    (root / "cache").mkdir(parents=True)
    with pytest.raises(ShareableNotFound):
        ShareableStore(root).get_instrument("005930")


def test_search_instruments_exact_then_shortest_partial(tmp_path):
    root = tmp_path / "shareable"
    _write_json(
        root / "cache" / "ticker_map_20260822.json",
        {
            "005935": "삼성전자우",
            "207940": "삼성바이오로직스",
            "005930": "삼성전자",
        },
    )
    store = ShareableStore(root)
    assert store.search_instruments("삼성전자", limit=2)[0]["ticker"] == "005930"
    assert [item["ticker"] for item in store.search_instruments("삼성", limit=2)] == [
        "005930",
        "005935",
    ]


def test_latest_ohlcv_selects_latest_and_filters_unknown_fields(tmp_path):
    root = tmp_path / "shareable"
    _write_json(root / "cache" / "ohlcv" / "005930_20260821.json", {"close": [1]})
    _write_json(
        root / "cache" / "ohlcv" / "005930_20260822.json",
        {"close": [2, 3], "volume": [4, 5], "portfolio": {"cash": 1}},
    )

    result = ShareableStore(root).latest_ohlcv("005930")

    assert result == {
        "ticker": "005930",
        "as_of": "20260822",
        "series": {"close": [2, 3], "volume": [4, 5]},
    }


def test_latest_ohlcv_rejects_symlink(tmp_path):
    root = tmp_path / "shareable"
    outside = tmp_path / "outside.json"
    _write_json(outside, {"close": [999]})
    ohlcv = root / "cache" / "ohlcv"
    ohlcv.mkdir(parents=True)
    (ohlcv / "005930_20260822.json").symlink_to(outside)

    with pytest.raises(ShareableNotFound):
        ShareableStore(root).latest_ohlcv("005930")


def test_latest_ohlcv_requires_allowlisted_series(tmp_path):
    root = tmp_path / "shareable"
    _write_json(
        root / "cache" / "ohlcv" / "005930_20260822.json",
        {"chat_id": "private", "portfolio": [1]},
    )
    with pytest.raises(ShareableStoreError):
        ShareableStore(root).latest_ohlcv("005930")


def test_latest_universe_selects_latest_and_filters_invalid_rows(tmp_path):
    root = tmp_path / "shareable"
    _write_json(
        root / "cache" / "universe_KOSPI200_20260821.json",
        [["000660", "옛이름"]],
    )
    _write_json(
        root / "cache" / "universe_KOSPI200_20260822.json",
        [
            ["005930", "삼성전자"],
            ["000660", "SK하이닉스"],
            ["../bad", "제외"],
            ["035420", {"chat_id": 1}],
        ],
    )

    assert ShareableStore(root).latest_universe("kospi200") == {
        "market": "KOSPI200",
        "as_of": "20260822",
        "instruments": [
            {"ticker": "005930", "name": "삼성전자"},
            {"ticker": "000660", "name": "SK하이닉스"},
        ],
    }


def test_latest_universe_rejects_symlink(tmp_path):
    root = tmp_path / "shareable"
    outside = tmp_path / "outside.json"
    _write_json(outside, [["005930", "삼성전자"]])
    cache = root / "cache"
    cache.mkdir(parents=True)
    (cache / "universe_KOSPI200_20260822.json").symlink_to(outside)
    with pytest.raises(ShareableNotFound):
        ShareableStore(root).latest_universe("KOSPI200")


def test_latest_market_index_filters_unknown_fields(tmp_path):
    root = tmp_path / "shareable"
    _write_json(
        root / "cache" / "indices" / "market_index_KOSPI_20260822.json",
        {
            "index": "KOSPI",
            "as_of": "ignored",
            "series": {"date": ["20260821", "20260822"], "close": [3100, "3110"]},
            "portfolio": {"cash": 1},
        },
    )
    assert ShareableStore(root).latest_market_index("kospi") == {
        "index": "KOSPI",
        "as_of": "20260822",
        "series": {"date": ["20260821", "20260822"], "close": [3100.0, 3110.0]},
    }


def test_latest_market_index_rejects_symlink(tmp_path):
    root = tmp_path / "shareable"
    outside = tmp_path / "outside.json"
    _write_json(
        outside,
        {"index": "VKOSPI", "series": {"date": ["20260822"], "close": [20]}},
    )
    index_dir = root / "cache" / "indices"
    index_dir.mkdir(parents=True)
    (index_dir / "market_index_VKOSPI_20260822.json").symlink_to(outside)
    with pytest.raises(ShareableNotFound):
        ShareableStore(root).latest_market_index("VKOSPI")


def test_latest_factors_filters_private_and_invalid_fields(tmp_path):
    root = tmp_path / "shareable"
    _write_json(
        root / "cache" / "factors" / "factor_snapshot_KOSPI_20260821.json",
        {
            "market": "KOSPI",
            "fundamentals": {
                "005930": {"BPS": 50000, "PBR": "1.2", "chat_id": 123},
                "../bad": {"BPS": 1},
            },
            "market_caps": {"005930": 4e15, "000660": -1},
            "portfolio": {"cash": 1},
        },
    )
    assert ShareableStore(root).latest_factors("kospi") == {
        "market": "KOSPI",
        "as_of": "20260821",
        "fundamentals": {"005930": {"BPS": 50000.0, "PBR": 1.2}},
        "market_caps": {"005930": 4e15},
    }


def test_latest_factors_rejects_symlink(tmp_path):
    root = tmp_path / "shareable"
    outside = tmp_path / "outside.json"
    _write_json(outside, {"market_caps": {"005930": 4e15}})
    factors = root / "cache" / "factors"
    factors.mkdir(parents=True)
    (factors / "factor_snapshot_KOSPI_20260821.json").symlink_to(outside)
    with pytest.raises(ShareableNotFound):
        ShareableStore(root).latest_factors("KOSPI")
