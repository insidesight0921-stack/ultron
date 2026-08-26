from __future__ import annotations

import json
from email.message import Message
from urllib.error import HTTPError

import pytest

from private_data_api_client import (
    PrivateAPIAuthError,
    PrivateAPIError,
    PrivateAPIUnavailable,
    PrivateDataClient,
    private_api_enabled,
    validate_private_base_url,
)


TOKEN = "private-client-test-token-32-characters-minimum"


class _Response:
    def __init__(self, payload, *, cache_control="no-store"):
        self.payload = payload
        self.headers = Message()
        self.headers["Cache-Control"] = cache_control

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def _payload():
    return {
        "items": [
            {
                "ticker": "005930",
                "name": "삼성전자",
                "created_at": "2026-08-23 12:00:00",
            }
        ],
        "count": 1,
    }


def _schedule_payload(scope="upcoming"):
    return {
        "events": [
            {
                "id": 7,
                "title": "회의",
                "when_at": "2099-01-01T10:00:00",
                "notes": "안건",
                "completed": False,
                "rrule_freq": None,
                "rrule_byday": None,
                "rrule_until": None,
                "pre_notify_minutes": 30,
                "pre_notify_minutes_list": [30, 5],
            }
        ],
        "count": 1,
        "scope": scope,
    }


def _paper_portfolios_payload():
    return {
        "portfolios": [{
            "id": 1,
            "name": "모의투자",
            "seed_capital": 100_000_000.0,
            "created_at": "2026-08-24 09:00:00",
        }],
        "count": 1,
    }


def _paper_positions_payload():
    return {
        "positions": [{
            "id": 3,
            "slot_id": 2,
            "ticker": "005930",
            "name": "삼성전자",
            "quantity": 5,
            "avg_price": 80_000.0,
            "opened_at": "2026-08-24 09:10:00",
            "updated_at": "2026-08-24 09:10:00",
            "slot_name": "콴텍",
        }],
        "count": 1,
    }


def _paper_slots_payload():
    return {
        "slots": [{
            "id": 2,
            "portfolio_id": 1,
            "name": "콴텍",
            "allocation_pct": 0.4,
            "current_capital": 39_000_000.0,
            "created_at": "2026-08-24 09:00:00",
            "n_positions": 1,
            "n_trades": 1,
        }],
        "count": 1,
    }


def _paper_trades_payload():
    return {
        "trades": [{
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
        }],
        "count": 1,
    }


def _paper_ipo_records_payload():
    return {
        "records": [{
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
        }],
        "count": 1,
    }


def _paper_ipo_stats_payload():
    return {
        "stats": [{
            "grade": "A", "n": 1, "avg_return": 20.0,
            "min_return": 20.0, "max_return": 20.0, "n_pos": 1,
        }],
        "count": 1,
    }


def _paper_performance_payload():
    return {
        "performance": [{
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
        }],
        "count": 1,
    }


def _paper_myquant_payload():
    return {
        "tags": {"정배열": {"n": 1, "pnl": 10_000, "win_rate": 100.0}},
        "text": "정배열 성과",
    }


def test_private_base_url_is_locked_to_loopback_8091():
    assert validate_private_base_url("http://127.0.0.1:8091") == "http://127.0.0.1:8091"
    assert validate_private_base_url("http://[::1]:8091") == "http://[::1]:8091"
    for value in (
        "https://127.0.0.1:8091",
        "http://192.168.0.10:8091",
        "http://127.0.0.1:8090",
        "http://user:pass@127.0.0.1:8091",
        "http://127.0.0.1:8091/private",
    ):
        with pytest.raises(ValueError):
            validate_private_base_url(value)


def test_private_api_enabled(monkeypatch):
    monkeypatch.delenv("AI_AGENT_PRIVATE_API_ENABLED", raising=False)
    assert private_api_enabled() is False
    monkeypatch.setenv("AI_AGENT_PRIVATE_API_ENABLED", "true")
    assert private_api_enabled() is True


def test_watchlist_request_uses_header_only_and_validates_response():
    seen = []

    def opener(request, timeout):
        seen.append((request.full_url, request.get_header("Authorization"), timeout))
        return _Response(_payload())

    client = PrivateDataClient(token=TOKEN, timeout=0.5, opener=opener)
    assert client.list_watchlist()[0]["name"] == "삼성전자"
    assert seen == [
        ("http://127.0.0.1:8091/v1/private/watchlist", f"Bearer {TOKEN}", 0.5)
    ]
    assert "token=" not in seen[0][0]


def test_auth_failure_is_distinct_and_does_not_echo_token():
    def unauthorized(request, timeout):
        raise HTTPError(request.full_url, 401, "Unauthorized", Message(), None)

    client = PrivateDataClient(token=TOKEN, opener=unauthorized)
    with pytest.raises(PrivateAPIAuthError) as exc_info:
        client.list_watchlist()
    assert TOKEN not in str(exc_info.value)


def test_unavailable_and_invalid_payload_fail_closed():
    client = PrivateDataClient(
        token=TOKEN,
        opener=lambda request, timeout: (_ for _ in ()).throw(OSError("down")),
    )
    with pytest.raises(PrivateAPIUnavailable):
        client.list_watchlist()

    client = PrivateDataClient(
        token=TOKEN,
        opener=lambda request, timeout: _Response({"items": [], "count": 1}),
    )
    with pytest.raises(PrivateAPIError):
        client.list_watchlist()


def test_missing_no_store_header_is_rejected():
    client = PrivateDataClient(
        token=TOKEN,
        opener=lambda request, timeout: _Response(_payload(), cache_control="public"),
    )
    with pytest.raises(PrivateAPIError, match="cache"):
        client.list_watchlist()


def test_schedule_request_keeps_chat_scope_in_header_not_url():
    seen = []

    def opener(request, timeout):
        seen.append(
            (
                request.full_url,
                request.get_header("X-ai-agent-chat-id"),
                request.get_header("Authorization"),
            )
        )
        return _Response(_schedule_payload())

    client = PrivateDataClient(token=TOKEN, opener=opener)
    events = client.list_schedule("-100123", upcoming_only=True, limit=20)
    assert events[0]["title"] == "회의"
    assert seen == [
        (
            "http://127.0.0.1:8091/v1/private/schedule/events/upcoming",
            "-100123",
            f"Bearer {TOKEN}",
        )
    ]
    assert "-100123" not in seen[0][0]


def test_schedule_client_validates_scope_limit_and_payload():
    client = PrivateDataClient(
        token=TOKEN,
        opener=lambda request, timeout: _Response(_schedule_payload(scope="all")),
    )
    with pytest.raises(PrivateAPIError):
        client.list_schedule("111", upcoming_only=True)
    with pytest.raises(ValueError):
        client.list_schedule("not-a-chat", upcoming_only=True)
    with pytest.raises(ValueError):
        client.list_schedule("111", upcoming_only=True, limit=101)


def test_schedule_client_rejects_extra_or_private_fields():
    payload = _schedule_payload()
    payload["events"][0]["chat_id"] = "111"
    client = PrivateDataClient(
        token=TOKEN,
        opener=lambda request, timeout: _Response(payload),
    )
    with pytest.raises(PrivateAPIError):
        client.list_schedule("111", upcoming_only=True)


def test_paper_requests_use_fixed_paths_and_filter_slot_locally():
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        payload = (
            _paper_portfolios_payload()
            if request.full_url.endswith("/portfolios")
            else _paper_positions_payload()
        )
        return _Response(payload)

    client = PrivateDataClient(token=TOKEN, opener=opener)
    assert client.list_paper_portfolios()[0]["id"] == 1
    assert client.list_paper_positions(slot="콴텍")[0]["slot_id"] == 2
    assert client.list_paper_positions(slot=2)[0]["ticker"] == "005930"
    assert client.list_paper_positions(slot="키움") == []
    assert all("?" not in url for url in seen)


@pytest.mark.parametrize("kind", ["portfolio", "position"])
def test_paper_client_rejects_extra_or_invalid_fields(kind):
    payload = (
        _paper_portfolios_payload()
        if kind == "portfolio"
        else _paper_positions_payload()
    )
    collection = "portfolios" if kind == "portfolio" else "positions"
    payload[collection][0]["secret"] = "not-allowed"
    client = PrivateDataClient(
        token=TOKEN,
        opener=lambda request, timeout: _Response(payload),
    )
    with pytest.raises(PrivateAPIError):
        if kind == "portfolio":
            client.list_paper_portfolios()
        else:
            client.list_paper_positions()


def test_paper_slot_and_trade_requests_are_fixed_and_filtered_locally():
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        payload = _paper_slots_payload() if request.full_url.endswith("/slots") else _paper_trades_payload()
        return _Response(payload)

    client = PrivateDataClient(token=TOKEN, opener=opener)
    assert client.list_paper_slots()[0]["n_trades"] == 1
    assert client.list_paper_trades(slot="콴텍", limit=1)[0]["id"] == 4
    assert client.list_paper_trades(slot=99) == []
    assert all("?" not in url for url in seen)
    with pytest.raises(ValueError):
        client.list_paper_trades(limit=1001)


@pytest.mark.parametrize("kind", ["slot", "trade"])
def test_paper_slot_and_trade_clients_reject_extra_fields(kind):
    payload = _paper_slots_payload() if kind == "slot" else _paper_trades_payload()
    collection = "slots" if kind == "slot" else "trades"
    payload[collection][0]["private"] = True
    client = PrivateDataClient(token=TOKEN, opener=lambda request, timeout: _Response(payload))
    with pytest.raises(PrivateAPIError):
        if kind == "slot":
            client.list_paper_slots()
        else:
            client.list_paper_trades()


def test_paper_ipo_requests_use_fixed_paths_and_validate_payloads():
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        payload = (
            _paper_ipo_records_payload()
            if request.full_url.endswith("/records")
            else _paper_ipo_stats_payload()
        )
        return _Response(payload)

    client = PrivateDataClient(token=TOKEN, opener=opener)
    assert client.list_paper_ipo_records()[0]["name"] == "공모주"
    assert client.list_paper_ipo_stats()[0]["avg_return"] == 20.0
    assert all("?" not in url for url in seen)


@pytest.mark.parametrize("kind", ["records", "stats"])
def test_paper_ipo_client_rejects_extra_fields(kind):
    payload = _paper_ipo_records_payload() if kind == "records" else _paper_ipo_stats_payload()
    payload[kind][0]["private"] = True
    client = PrivateDataClient(token=TOKEN, opener=lambda request, timeout: _Response(payload))
    with pytest.raises(PrivateAPIError):
        if kind == "records":
            client.list_paper_ipo_records()
        else:
            client.list_paper_ipo_stats()


def test_paper_ipo_client_accepts_negative_returns_but_rejects_invalid_factors():
    stats = _paper_ipo_stats_payload()
    stats["stats"][0].update(avg_return=-10.0, min_return=-20.0, max_return=-1.0, n_pos=0)
    client = PrivateDataClient(token=TOKEN, opener=lambda request, timeout: _Response(stats))
    assert client.list_paper_ipo_stats()[0]["avg_return"] == -10.0

    records = _paper_ipo_records_payload()
    records["records"][0]["factors"] = "[]"
    client = PrivateDataClient(token=TOKEN, opener=lambda request, timeout: _Response(records))
    with pytest.raises(PrivateAPIError):
        client.list_paper_ipo_records()


def test_paper_calculated_results_use_fixed_paths_and_validate_payloads():
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        payload = (
            _paper_performance_payload()
            if request.full_url.endswith("/performance")
            else _paper_myquant_payload()
        )
        return _Response(payload)

    client = PrivateDataClient(token=TOKEN, opener=opener)
    assert client.list_paper_performance()[0]["total_pnl"] == 10_000
    assert client.get_paper_myquant_tags()["tags"]["정배열"]["n"] == 1
    assert all("?" not in url for url in seen)


def test_paper_calculated_results_reject_extra_or_invalid_fields():
    performance = _paper_performance_payload()
    performance["performance"][0]["raw_trades"] = []
    client = PrivateDataClient(token=TOKEN, opener=lambda request, timeout: _Response(performance))
    with pytest.raises(PrivateAPIError):
        client.list_paper_performance()

    tags = _paper_myquant_payload()
    tags["tags"]["정배열"]["ticker"] = "005930"
    client = PrivateDataClient(token=TOKEN, opener=lambda request, timeout: _Response(tags))
    with pytest.raises(PrivateAPIError):
        client.get_paper_myquant_tags()
