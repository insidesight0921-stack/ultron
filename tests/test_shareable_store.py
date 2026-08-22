from __future__ import annotations

import json

import pytest

from shareable_store import (
    ShareableNotFound,
    ShareableStore,
    ShareableStoreError,
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
