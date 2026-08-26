from __future__ import annotations

import sqlite3
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from private_data_api import create_app
from private_data_api_client import PrivateAPIWriteConflict, PrivateDataClient
from private_watchlist_write_consumer import (
    WatchlistApprovalRequired,
    WatchlistConsumerDisabled,
    WatchlistTerminalState,
    WatchlistWriteExecutor,
)
from private_watchlist_write_store import WatchlistWriteStore
from telegram_write_identity import build_watchlist_write_identity
import watchlist_bot as wb
import watchlist_store


TOKEN = "private-consumer-token-32-characters-minimum"


class _BridgeResponse:
    def __init__(self, response):
        self.headers = response.headers
        self.content = response.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.content if limit < 0 else self.content[:limit]


class _Opener:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def __call__(self, request, timeout):
        parsed = urlsplit(request.full_url)
        self.calls.append((request.get_method(), parsed.path))
        response = self.client.request(
            request.get_method(),
            parsed.path,
            headers=dict(request.header_items()),
            content=request.data,
        )
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url,
                response.status_code,
                "Private API error",
                response.headers,
                BytesIO(response.content),
            )
        return _BridgeResponse(response)


@pytest.fixture
def consumer_stack(tmp_path, private_write_permit_factory):
    db = tmp_path / "consumer-isolated" / "assistant.db"
    writer = WatchlistWriteStore(db, writes_enabled=True)
    permit = private_write_permit_factory(db)

    def read_items():
        return [
            {"ticker": item.ticker, "name": item.name, "created_at": item.created_at}
            for item in watchlist_store.list_items(db_path=db)
        ]

    app = create_app(
        TOKEN,
        watchlist_reader=read_items,
        watchlist_writer=writer,
        enable_watchlist_writes=True,
        write_activation_permit=permit,
    )
    opener = _Opener(TestClient(app))
    client = PrivateDataClient(
        token=TOKEN,
        opener=opener,
        writes_enabled=True,
        write_activation_permit=permit,
    )
    executor = WatchlistWriteExecutor(
        client,
        enabled=True,
        write_activation_permit=permit,
    )
    return executor, client, writer, opener


def _identity(*, update_id=7001, action="add"):
    return build_watchlist_write_identity(
        update_id=update_id,
        chat_id=-100123456789,
        user_id=123456,
        message_id=3001 + update_id,
        action=action,
    )


def test_executor_is_disabled_by_default_before_client_call():
    class Client:
        writes_enabled = True

        def preflight_watchlist_write(self, **kwargs):
            raise AssertionError("must not call client")

    executor = WatchlistWriteExecutor(Client())
    with pytest.raises(WatchlistConsumerDisabled):
        executor.execute(
            action="add",
            ticker="005930",
            name="삼성전자",
            identity=_identity(),
            user_approved=True,
        )


def test_executor_requires_explicitly_enabled_client(private_write_permit_factory):
    client = PrivateDataClient(token=TOKEN)
    with pytest.raises(ValueError, match="activation permit"):
        WatchlistWriteExecutor(
            client,
            enabled=True,
            write_activation_permit=private_write_permit_factory(),
        )


def test_executor_requires_same_permit_as_client(private_write_permit_factory):
    client_permit = private_write_permit_factory()
    client = PrivateDataClient(
        token=TOKEN,
        writes_enabled=True,
        write_activation_permit=client_permit,
    )
    with pytest.raises(ValueError, match="do not match"):
        WatchlistWriteExecutor(
            client,
            enabled=True,
            write_activation_permit=private_write_permit_factory(),
        )


def test_executor_requires_explicit_user_approval_without_http(consumer_stack):
    executor, _, writer, opener = consumer_stack

    with pytest.raises(WatchlistApprovalRequired):
        executor.execute(
            action="add",
            ticker="005930",
            name="삼성전자",
            identity=_identity(),
        )

    assert opener.calls == []
    with sqlite3.connect(f"file:{writer.db_path}?mode=ro", uri=True) as con:
        assert con.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'private_write_%'"
        ).fetchone()[0] == 0


def test_executor_full_flow_and_applied_replay(consumer_stack):
    executor, _, writer, opener = consumer_stack
    identity = _identity()

    first = executor.execute(
        action="add",
        ticker="005930",
        name="삼성전자",
        identity=identity,
        user_approved=True,
    )
    replay = executor.execute(
        action="add",
        ticker="005930",
        name="삼성전자",
        identity=identity,
        user_approved=True,
    )

    assert first.state == "applied"
    assert first.replayed is False
    assert replay.state == "applied"
    assert replay.replayed is True
    assert replay.result == first.result
    assert writer.current_version() == 1
    assert len(watchlist_store.list_items(db_path=writer.db_path)) == 1
    assert [path for _, path in opener.calls].count(
        "/v1/private/watchlist/write/approvals/apply"
    ) == 1


@pytest.mark.parametrize("partial_state", ["pending", "approved"])
def test_executor_resumes_partial_request_with_original_version(consumer_stack, partial_state):
    executor, client, writer, _ = consumer_stack
    identity = _identity()
    preflight = client.preflight_watchlist_write(
        operation=identity.operation,
        request_id=identity.request_id,
        approval_id=identity.approval_id,
        idempotency_key=identity.idempotency_key,
    )
    client.submit_watchlist_write(
        operation=identity.operation,
        request_id=identity.request_id,
        approval_id=identity.approval_id,
        idempotency_key=identity.idempotency_key,
        expected_version=preflight["expected_version"],
        payload={"ticker": "005930", "name": "삼성전자"},
    )
    if partial_state == "approved":
        client.approve_watchlist_write(
            request_id=identity.request_id,
            approval_id=identity.approval_id,
        )

    result = executor.execute(
        action="add",
        ticker="005930",
        name="삼성전자",
        identity=identity,
        user_approved=True,
    )

    assert result.state == "applied"
    assert result.replayed is True
    assert writer.current_version() == 1


def test_executor_refuses_terminal_rejected_intent(consumer_stack):
    executor, client, writer, _ = consumer_stack
    identity = _identity()
    preflight = client.preflight_watchlist_write(
        operation=identity.operation,
        request_id=identity.request_id,
        approval_id=identity.approval_id,
        idempotency_key=identity.idempotency_key,
    )
    client.submit_watchlist_write(
        operation=identity.operation,
        request_id=identity.request_id,
        approval_id=identity.approval_id,
        idempotency_key=identity.idempotency_key,
        expected_version=preflight["expected_version"],
        payload={"ticker": "005930", "name": "삼성전자"},
    )
    client.reject_watchlist_write(
        request_id=identity.request_id,
        approval_id=identity.approval_id,
    )

    with pytest.raises(WatchlistTerminalState) as exc:
        executor.execute(
            action="add",
            ticker="005930",
            name="삼성전자",
            identity=identity,
            user_approved=True,
        )

    assert exc.value.state == "rejected"
    assert writer.current_version() == 0
    assert watchlist_store.list_items(db_path=writer.db_path) == []


def test_executor_detects_payload_drift_on_partial_retry(consumer_stack):
    executor, client, writer, _ = consumer_stack
    identity = _identity()
    preflight = client.preflight_watchlist_write(
        operation=identity.operation,
        request_id=identity.request_id,
        approval_id=identity.approval_id,
        idempotency_key=identity.idempotency_key,
    )
    client.submit_watchlist_write(
        operation=identity.operation,
        request_id=identity.request_id,
        approval_id=identity.approval_id,
        idempotency_key=identity.idempotency_key,
        expected_version=preflight["expected_version"],
        payload={"ticker": "005930", "name": "삼성전자"},
    )

    with pytest.raises(PrivateAPIWriteConflict) as exc:
        executor.execute(
            action="add",
            ticker="000660",
            name="SK하이닉스",
            identity=identity,
            user_approved=True,
        )

    assert exc.value.code == "idempotency_conflict"
    assert writer.current_version() == 0
    assert watchlist_store.list_items(db_path=writer.db_path) == []


def test_watchlist_bot_injected_executor_bypasses_direct_store(monkeypatch):
    identity = _identity(action="remove")

    class Client:
        def list_watchlist(self):
            return [{"ticker": "005930", "name": "삼성전자", "created_at": "now"}]

    class Executor:
        client = Client()

        def require_ready(self, *, user_approved):
            assert user_approved is True

        def execute(self, **kwargs):
            assert kwargs["user_approved"] is True
            return type(
                "Outcome",
                (),
                {"result": {"ticker": "005930", "removed": True}},
            )()

    monkeypatch.setattr(
        wb.store,
        "remove",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("direct remove")),
    )
    answer, _ = wb.run(
        "remove",
        "삼성전자",
        write_identity=identity,
        write_executor=Executor(),
        write_user_approved=True,
    )

    assert answer == "✅ 관심종목에서 삭제: 삼성전자 (005930)"


def test_watchlist_bot_executor_requires_approval_and_forbids_db_path(
    consumer_stack,
    monkeypatch,
    tmp_path,
):
    executor, _, _, _ = consumer_stack
    identity = _identity()
    monkeypatch.setattr(
        wb,
        "resolve_ticker",
        lambda query: (_ for _ in ()).throw(AssertionError("resolve before approval")),
    )

    with pytest.raises(WatchlistApprovalRequired):
        wb.run(
            "add",
            "005930",
            write_identity=identity,
            write_executor=executor,
        )
    with pytest.raises(ValueError, match="cannot be combined"):
        wb.run(
            "add",
            "005930",
            db_path=tmp_path / "assistant.db",
            write_identity=identity,
            write_executor=executor,
            write_user_approved=True,
        )
