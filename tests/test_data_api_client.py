from __future__ import annotations

import json

import pytest

from data_api_client import (
    DataAPIError,
    DataAPIUnavailable,
    ShareableDataClient,
    data_api_enabled,
    validate_base_url,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def test_validate_base_url_allows_only_loopback():
    assert validate_base_url("http://127.0.0.1:8090") == "http://127.0.0.1:8090"
    assert validate_base_url("http://[::1]:8090") == "http://[::1]:8090"
    for value in (
        "https://127.0.0.1:8090",
        "http://192.168.0.10:8090",
        "http://user:pass@127.0.0.1:8090",
        "http://127.0.0.1:8090/private",
    ):
        with pytest.raises(ValueError):
            validate_base_url(value)


def test_data_api_enabled(monkeypatch):
    monkeypatch.delenv("AI_AGENT_DATA_API_ENABLED", raising=False)
    assert data_api_enabled() is False
    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "true")
    assert data_api_enabled() is True


def test_get_instrument_validates_response_and_url():
    seen = []

    def opener(request, timeout):
        seen.append((request.full_url, timeout))
        return _Response({"ticker": "005930", "name": "삼성전자"})

    client = ShareableDataClient(opener=opener, timeout=0.5)
    assert client.get_instrument("005930")["name"] == "삼성전자"
    assert seen == [("http://127.0.0.1:8090/v1/shareable/instruments/005930", 0.5)]


def test_search_instruments_encodes_query():
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        return _Response([{"ticker": "005930", "name": "삼성전자"}])

    client = ShareableDataClient(opener=opener)
    assert client.search_instruments("삼성 전자", limit=3)[0]["ticker"] == "005930"
    assert "query=%EC%82%BC%EC%84%B1+%EC%A0%84%EC%9E%90" in seen[0]
    assert "limit=3" in seen[0]


def test_invalid_payload_and_unavailable_are_distinct():
    client = ShareableDataClient(opener=lambda request, timeout: _Response([]))
    with pytest.raises(DataAPIError):
        client.get_instrument("005930")

    def unavailable(request, timeout):
        raise OSError("down")

    client = ShareableDataClient(opener=unavailable)
    with pytest.raises(DataAPIUnavailable):
        client.get_instrument("005930")


def test_failure_cooldown_skips_repeated_network_calls():
    calls = {"n": 0}

    def unavailable(request, timeout):
        calls["n"] += 1
        raise OSError("down")

    client = ShareableDataClient(opener=unavailable, failure_cooldown=30)
    with pytest.raises(DataAPIUnavailable):
        client.get_instrument("005930")
    with pytest.raises(DataAPIUnavailable):
        client.get_instrument("005930")
    assert calls["n"] == 1


def test_latest_ohlcv_validates_and_normalizes_close():
    client = ShareableDataClient(
        opener=lambda request, timeout: _Response(
            {
                "ticker": "005930",
                "as_of": "20260822",
                "series": {"close": ["70000", 71000]},
            }
        )
    )
    payload = client.latest_ohlcv("005930")
    assert payload["series"]["close"] == [70000.0, 71000.0]


def test_latest_universe_validates_payload_and_encodes_market():
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        return _Response(
            {
                "market": "KOSPI200+KOSDAQ150",
                "as_of": "20260822",
                "instruments": [{"ticker": "005930", "name": "삼성전자"}],
            }
        )

    client = ShareableDataClient(opener=opener)
    payload = client.latest_universe("kospi200+kosdaq150")
    assert payload["instruments"] == [{"ticker": "005930", "name": "삼성전자"}]
    assert "/KOSPI200%2BKOSDAQ150/latest" in seen[0]


def test_latest_market_index_validates_payload():
    client = ShareableDataClient(
        opener=lambda request, timeout: _Response(
            {
                "index": "KOSPI",
                "as_of": "20260822",
                "series": {"date": ["20260821", "20260822"], "close": ["3100", 3110]},
            }
        )
    )
    payload = client.latest_market_index("kospi")
    assert payload["series"]["close"] == [3100.0, 3110.0]


def test_latest_factors_validates_and_normalizes_payload():
    client = ShareableDataClient(
        opener=lambda request, timeout: _Response(
            {
                "market": "KOSPI",
                "as_of": "20260821",
                "fundamentals": {"005930": {"BPS": "50000", "PBR": 1.2}},
                "market_caps": {"005930": "4000000000000000"},
            }
        )
    )
    payload = client.latest_factors("kospi")
    assert payload["fundamentals"]["005930"] == {"BPS": 50000.0, "PBR": 1.2}
    assert payload["market_caps"]["005930"] == 4e15
