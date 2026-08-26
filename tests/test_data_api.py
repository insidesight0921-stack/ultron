from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from data_api import DEFAULT_HOST, DEFAULT_PORT, create_app, validate_bind_host
from shareable_store import ShareableStore


@pytest.fixture
def client(tmp_path):
    root = tmp_path / "shareable"
    cache = root / "cache"
    ohlcv = cache / "ohlcv"
    ohlcv.mkdir(parents=True)
    (cache / "ticker_map_20260822.json").write_text(
        json.dumps({"005930": "삼성전자"}, ensure_ascii=False), encoding="utf-8"
    )
    (cache / "universe_KOSPI200_20260822.json").write_text(
        json.dumps([["005930", "삼성전자"]], ensure_ascii=False), encoding="utf-8"
    )
    indices = cache / "indices"
    indices.mkdir()
    (indices / "market_index_KOSPI_20260822.json").write_text(
        json.dumps(
            {
                "index": "KOSPI",
                "series": {"date": ["20260821", "20260822"], "close": [3100, 3110]},
                "token": "must-not-leak",
            }
        ),
        encoding="utf-8",
    )
    (ohlcv / "005930_20260822.json").write_text(
        json.dumps({"close": [70000, 71000], "token": "must-not-leak"}),
        encoding="utf-8",
    )
    factors = cache / "factors"
    factors.mkdir()
    (factors / "factor_snapshot_KOSPI_20260821.json").write_text(
        json.dumps(
            {
                "market": "KOSPI",
                "fundamentals": {
                    "005930": {"BPS": 50000, "PBR": 1.2, "chat_id": "must-not-leak"}
                },
                "market_caps": {"005930": 4e15},
                "portfolio": {"cash": 1},
            }
        ),
        encoding="utf-8",
    )
    return TestClient(create_app(ShareableStore(root)))


def test_health_declares_shareable_only_boundary(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["boundary"] == "shareable-only"


def test_instrument_endpoint(client):
    response = client.get("/v1/shareable/instruments/005930")
    assert response.status_code == 200
    assert response.json()["name"] == "삼성전자"


def test_instrument_search_endpoint(client):
    response = client.get("/v1/shareable/instruments?query=삼성&limit=5")
    assert response.status_code == 200
    assert response.json() == [
        {"ticker": "005930", "name": "삼성전자", "as_of": "20260822"}
    ]


def test_ohlcv_endpoint_filters_unexpected_fields(client):
    response = client.get("/v1/shareable/ohlcv/005930/latest")
    assert response.status_code == 200
    assert response.json()["series"] == {"close": [70000, 71000]}
    assert "token" not in response.text


def test_universe_endpoint_filters_and_validates_market(client):
    response = client.get("/v1/shareable/universes/KOSPI200/latest")
    assert response.status_code == 200
    assert response.json() == {
        "market": "KOSPI200",
        "as_of": "20260822",
        "instruments": [{"ticker": "005930", "name": "삼성전자"}],
    }
    assert client.get("/v1/shareable/universes/../private/latest").status_code in {404, 422}


def test_market_index_endpoint_is_allowlisted(client):
    response = client.get("/v1/shareable/market-indices/KOSPI/latest")
    assert response.status_code == 200
    assert response.json() == {
        "index": "KOSPI",
        "as_of": "20260822",
        "series": {"date": ["20260821", "20260822"], "close": [3100.0, 3110.0]},
    }
    assert "token" not in response.text
    assert client.get("/v1/shareable/market-indices/VIX/latest").status_code == 422


def test_factor_endpoint_filters_unexpected_fields(client):
    response = client.get("/v1/shareable/factors/KOSPI/latest")
    assert response.status_code == 200
    assert response.json() == {
        "market": "KOSPI",
        "as_of": "20260821",
        "fundamentals": {"005930": {"BPS": 50000.0, "PBR": 1.2}},
        "market_caps": {"005930": 4e15},
    }
    assert "chat_id" not in response.text
    assert "portfolio" not in response.text
    assert client.get("/v1/shareable/factors/KOSPI200/latest").status_code == 422


def test_invalid_ticker_rejected(client):
    response = client.get("/v1/shareable/instruments/not-a-ticker")
    assert response.status_code == 422


def test_private_and_generic_file_routes_do_not_exist(client):
    assert client.get("/v1/private/watchlist").status_code == 404
    assert client.get("/v1/files").status_code == 404
    paths = {route.path for route in client.app.routes}
    assert not any(path.startswith("/v1/private") for path in paths)
    assert not any("file" in path or "sql" in path for path in paths)


def test_default_bind_is_loopback_and_port_avoids_existing_services():
    assert DEFAULT_HOST == "127.0.0.1"
    assert DEFAULT_PORT == 8090
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("::1") == "::1"
    assert validate_bind_host("localhost") == "localhost"
    for host in ("0.0.0.0", "192.168.0.10", "example.com"):
        with pytest.raises(ValueError):
            validate_bind_host(host)
