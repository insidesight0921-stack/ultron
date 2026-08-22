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
    (ohlcv / "005930_20260822.json").write_text(
        json.dumps({"close": [70000, 71000], "token": "must-not-leak"}),
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


def test_ohlcv_endpoint_filters_unexpected_fields(client):
    response = client.get("/v1/shareable/ohlcv/005930/latest")
    assert response.status_code == 200
    assert response.json()["series"] == {"close": [70000, 71000]}
    assert "token" not in response.text


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
