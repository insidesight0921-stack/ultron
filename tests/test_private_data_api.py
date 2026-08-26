from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from private_data_api import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    TOKEN_ENV,
    create_app,
    resolve_private_token,
    validate_bind_host,
    validate_private_port,
    validate_private_token,
)


TOKEN = "private-api-test-token-32-characters-minimum"


@pytest.fixture
def client():
    return TestClient(
        create_app(
            TOKEN,
            watchlist_reader=lambda: [
                {
                    "ticker": "005930",
                    "name": "삼성전자",
                    "created_at": "2026-08-23 12:00:00",
                }
            ],
            schedule_reader=lambda chat_id, upcoming_only: [
                {
                    "id": 7,
                    "title": "회의",
                    "when_at": "2099-01-01T10:00:00",
                    "notes": None,
                    "completed": False,
                    "rrule_freq": None,
                    "rrule_byday": None,
                    "rrule_until": None,
                    "pre_notify_minutes": 0,
                    "pre_notify_minutes_list": [],
                }
            ],
            paper_portfolio_reader=lambda: [
                {
                    "id": 1,
                    "name": "모의투자",
                    "seed_capital": 100_000_000.0,
                    "created_at": "2026-08-24 09:00:00",
                }
            ],
            paper_position_reader=lambda: [
                {
                    "id": 3,
                    "slot_id": 2,
                    "ticker": "005930",
                    "name": "삼성전자",
                    "quantity": 5,
                    "avg_price": 80_000.0,
                    "opened_at": "2026-08-24 09:10:00",
                    "updated_at": "2026-08-24 09:10:00",
                    "slot_name": "콴텍",
                }
            ],
            paper_slot_reader=lambda: [
                {
                    "id": 2,
                    "portfolio_id": 1,
                    "name": "콴텍",
                    "allocation_pct": 0.4,
                    "current_capital": 39_000_000.0,
                    "created_at": "2026-08-24 09:00:00",
                    "n_positions": 1,
                    "n_trades": 1,
                }
            ],
            paper_trade_reader=lambda: [
                {
                    "id": 4,
                    "slot_id": 2,
                    "ticker": "005930",
                    "name": "삼성전자",
                    "side": "buy",
                    "quantity": 5,
                    "price": 80_000.0,
                    "fees": 600.0,
                    "notes": "테스트",
                    "executed_at": "2026-08-24 09:10:00",
                    "slot_name": "콴텍",
                }
            ],
            paper_ipo_record_reader=lambda: [
                {
                    "id": 5,
                    "name": "공모주",
                    "sub_start": "20260801",
                    "sub_end": "20260802",
                    "listing_date": "20260810",
                    "grade": "A",
                    "score": 80.0,
                    "factors": '{"offer_price": 10000}',
                    "subscribed": 1,
                    "alloc_amount": 1_000_000.0,
                    "listing_price": 12_000.0,
                    "return_pct": 20.0,
                    "notes": None,
                    "created_at": "2026-08-01 09:00:00",
                    "updated_at": "2026-08-10 09:00:00",
                }
            ],
            paper_ipo_stat_reader=lambda: [
                {
                    "grade": "A", "n": 1, "avg_return": 20.0,
                    "min_return": 20.0, "max_return": 20.0, "n_pos": 1,
                }
            ],
            paper_performance_reader=lambda: [
                {
                    "slot_id": 2,
                    "slot_name": "콴텍",
                    "n_closed": 1,
                    "win_rate": 100.0,
                    "total_pnl": 10_000,
                    "total_return_pct": 0.03,
                    "max_drawdown_pct": 0.0,
                    "sharpe": None,
                    "n_open_positions": 1,
                    "open_cost": 80_000,
                }
            ],
            paper_myquant_tag_reader=lambda: {
                "tags": {"정배열": {"n": 1, "pnl": 10_000, "win_rate": 100.0}},
                "text": "정배열 성과",
            },
        )
    )


def test_health_discloses_no_private_state_and_disables_caching(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "private_data_api",
        "version": "v1",
        "boundary": "private-auth-required",
    }
    assert response.headers["cache-control"] == "no-store"


def test_private_status_requires_exact_bearer_token(client):
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        response = client.get("/v1/private/status", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    response = client.get(
        "/v1/private/status", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 200
    assert response.json()["boundary"] == "private-only"
    assert response.json()["capabilities"] == [
        "watchlist:read",
        "schedule:read",
        "paper:portfolio:read",
        "paper:positions:read",
        "paper:slots:read",
        "paper:trades:read",
        "paper:ipo:read",
        "paper:performance:read",
        "paper:myquant-tags:read",
    ]
    assert response.json()["writes_enabled"] is False


def test_token_in_query_is_not_accepted(client):
    response = client.get(f"/v1/private/status?token={TOKEN}")
    assert response.status_code == 401
    assert TOKEN not in response.text

    response = client.get(
        f"/v1/private/status?token={TOKEN}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 400
    assert TOKEN not in response.text


def test_authenticated_watchlist_returns_only_allowlisted_fields(client):
    response = client.get(
        "/v1/private/watchlist",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "ticker": "005930",
                "name": "삼성전자",
                "created_at": "2026-08-23 12:00:00",
            }
        ],
        "count": 1,
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path,scope",
    [
        ("/v1/private/schedule/events", "all"),
        ("/v1/private/schedule/events/upcoming", "upcoming"),
    ],
)
def test_authenticated_schedule_is_chat_scoped_and_allowlisted(client, path, scope):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 400
    response = client.get(
        path,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-AI-Agent-Chat-ID": "-100123",
        },
    )
    assert response.status_code == 200
    assert response.json()["scope"] == scope
    assert response.json()["count"] == 1
    event = response.json()["events"][0]
    assert set(event) == {
        "id", "title", "when_at", "notes", "completed", "rrule_freq",
        "rrule_byday", "rrule_until", "pre_notify_minutes",
        "pre_notify_minutes_list",
    }
    assert "chat_id" not in event
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path,collection,fields",
    [
        (
            "/v1/private/paper/portfolios",
            "portfolios",
            {"id", "name", "seed_capital", "created_at"},
        ),
        (
            "/v1/private/paper/positions",
            "positions",
            {
                "id", "slot_id", "ticker", "name", "quantity", "avg_price",
                "opened_at", "updated_at", "slot_name",
            },
        ),
        (
            "/v1/private/paper/slots",
            "slots",
            {
                "id", "portfolio_id", "name", "allocation_pct", "current_capital",
                "created_at", "n_positions", "n_trades",
            },
        ),
        (
            "/v1/private/paper/trades",
            "trades",
            {
                "id", "slot_id", "ticker", "name", "side", "quantity", "price",
                "fees", "notes", "executed_at", "slot_name",
            },
        ),
        (
            "/v1/private/paper/ipo/records",
            "records",
            {
                "id", "name", "sub_start", "sub_end", "listing_date", "grade", "score",
                "factors", "subscribed", "alloc_amount", "listing_price", "return_pct",
                "notes", "created_at", "updated_at",
            },
        ),
        (
            "/v1/private/paper/ipo/stats",
            "stats",
            {"grade", "n", "avg_return", "min_return", "max_return", "n_pos"},
        ),
        (
            "/v1/private/paper/performance",
            "performance",
            {
                "slot_id", "slot_name", "n_closed", "win_rate", "total_pnl",
                "total_return_pct", "max_drawdown_pct", "sharpe",
                "n_open_positions", "open_cost",
            },
        ),
    ],
)
def test_authenticated_paper_reads_are_allowlisted(client, path, collection, fields):
    assert client.get(path).status_code == 401
    assert client.get(
        f"{path}?slot=콴텍", headers={"Authorization": f"Bearer {TOKEN}"}
    ).status_code == 400
    response = client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert set(response.json()[collection][0]) == fields
    assert response.headers["cache-control"] == "no-store"


def test_authenticated_myquant_tags_returns_aggregate_only(client):
    path = "/v1/private/paper/myquant-tags"
    assert client.get(path).status_code == 401
    response = client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {
        "tags": {"정배열": {"n": 1, "pnl": 10_000, "win_rate": 100.0}},
        "text": "정배열 성과",
    }
    assert "trades" not in response.json()


def test_private_token_validation_and_separation():
    assert validate_private_token(TOKEN) == TOKEN
    for value in ("", "short", "x" * 31, "x" * 31 + " ", "change-me"):
        with pytest.raises(ValueError):
            validate_private_token(value)
    assert resolve_private_token({TOKEN_ENV: TOKEN}) == TOKEN
    with pytest.raises(ValueError, match="Telegram"):
        resolve_private_token({TOKEN_ENV: TOKEN, "TELEGRAM_BOT_TOKEN": TOKEN})


def test_private_api_is_loopback_only_on_distinct_port():
    assert DEFAULT_HOST == "127.0.0.1"
    assert DEFAULT_PORT == 8091
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("::1") == "::1"
    assert validate_bind_host("localhost") == "localhost"
    for host in ("0.0.0.0", "192.168.0.10", "example.com"):
        with pytest.raises(ValueError):
            validate_bind_host(host)
    assert validate_private_port(8091) == 8091
    for port in (0, 65536, 8080, 8082, 8090, 11434):
        with pytest.raises(ValueError):
            validate_private_port(port)


def test_private_skeleton_has_no_data_or_generic_routes(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    for path in (
        "/v1/private/paper",
        "/v1/private/files",
        "/v1/private/sql",
        "/v1/shareable/instruments",
    ):
        assert client.get(path).status_code == 404
    paths = {route.path for route in client.app.routes}
    assert paths == {
        "/health",
        "/v1/private/status",
        "/v1/private/watchlist",
        "/v1/private/schedule/events",
        "/v1/private/schedule/events/upcoming",
        "/v1/private/paper/portfolios",
        "/v1/private/paper/positions",
        "/v1/private/paper/slots",
        "/v1/private/paper/trades",
        "/v1/private/paper/ipo/records",
        "/v1/private/paper/ipo/stats",
        "/v1/private/paper/performance",
        "/v1/private/paper/myquant-tags",
    }


def test_private_api_module_does_not_import_private_stores_directly():
    project = Path(__file__).resolve().parents[1]
    source = (project / "scripts" / "private_data_api.py").read_text(encoding="utf-8")
    for forbidden in ("sqlite3", "watchlist_store", "paper_db", "schedule_bot"):
        assert forbidden not in source


def test_runtime_disables_access_log_to_protect_query_strings():
    project = Path(__file__).resolve().parents[1]
    source = (project / "scripts" / "private_data_api.py").read_text(encoding="utf-8")
    assert "access_log=False" in source
