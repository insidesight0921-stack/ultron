from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from paper_trade_identity import (
    PAPER_DIRECT_WRITER_INVENTORY,
    PAPER_IDENTITY_CALLERS,
    build_paper_trade_write_identity,
    validate_paper_trade_write_identity,
)
from telegram_write_identity import build_watchlist_write_identity


ROOT = Path(__file__).resolve().parents[1]


def _identity(**overrides):
    values = {
        "caller": "telegram-kium",
        "operation": "paper.buy",
        "slot_id": 2,
        "actor_id": "telegram-user-123456",
        "source_event_id": "callback-update-987654",
        "item_key": "batch-item-0",
    }
    values.update(overrides)
    return build_paper_trade_write_identity(**values)


def _direct_calls(module_name):
    source = (ROOT / "scripts" / module_name).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        operations = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            function = child.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id if isinstance(function, ast.Name) else ""
            )
            if name == "record_buy":
                operations.add("paper.buy")
            elif name == "record_sell":
                operations.add("paper.sell")
        if operations:
            found.add((node.name, frozenset(operations)))
    return found


def test_direct_paper_writer_inventory_matches_every_production_callsite():
    expected = {}
    for policy in PAPER_DIRECT_WRITER_INVENTORY:
        expected.setdefault(policy.module, set()).add(
            (policy.function, policy.operations)
        )

    actual = {}
    for path in (ROOT / "scripts").glob("*.py"):
        calls = _direct_calls(path.name)
        if calls:
            actual[path.name] = calls

    assert actual == expected


def test_inventory_assigns_single_future_db_writer_and_retires_cli_direct_path():
    operational = [
        policy
        for policy in PAPER_DIRECT_WRITER_INVENTORY
        if policy.target_mode == "private-client"
    ]
    assert len(operational) == 5
    assert {policy.target_owner for policy in operational} == {"private-data-api"}
    cli = next(policy for policy in PAPER_DIRECT_WRITER_INVENTORY if policy.key == "operator-cli")
    assert cli.target_owner == "none"
    assert cli.target_mode == "retire-direct-rollback-only"
    intraday = next(
        policy for policy in PAPER_DIRECT_WRITER_INVENTORY if policy.key == "telegram-intraday"
    )
    assert intraday.operations == {"paper.sell"}
    assert intraday.approval_mode == "policy-auto-sell-only"


def test_same_source_coordinates_produce_same_retry_identity():
    first = _identity()
    second = _identity()

    assert first == second
    assert first.operation == "paper.buy"
    assert first.slot_id == 2
    assert first.idempotency_key.startswith("paper-telegram-kium-")


@pytest.mark.parametrize(
    "field,value",
    [
        ("caller", "telegram-quant"),
        ("operation", "paper.sell"),
        ("slot_id", 1),
        ("actor_id", "telegram-user-123457"),
        ("source_event_id", "callback-update-987655"),
        ("item_key", "batch-item-1"),
    ],
)
def test_each_identity_coordinate_change_creates_a_distinct_identity(field, value):
    original = _identity()
    changed = _identity(**{field: value})

    assert changed.request_id != original.request_id
    assert changed.approval_id != original.approval_id
    assert changed.idempotency_key != original.idempotency_key


def test_identity_retains_no_actor_event_item_or_trade_payload_plaintext():
    identity = _identity()
    rendered = repr(identity)

    for private_value in (
        "123456",
        "987654",
        "batch-item-0",
        "005930",
        "삼성전자",
        "80000",
    ):
        assert private_value not in rendered


def test_payload_values_are_not_identity_inputs_and_drift_keeps_same_identity():
    first = _identity()
    retry_after_payload_edit = _identity()

    assert first == retry_after_payload_edit


def test_paper_identity_is_domain_separated_from_existing_telegram_identity():
    paper = _identity()
    watchlist = build_watchlist_write_identity(
        update_id=987654,
        chat_id=-100123456789,
        user_id=123456,
        message_id=1,
        action="add",
    )

    assert paper.request_id != watchlist.request_id
    assert paper.approval_id != watchlist.approval_id
    assert paper.idempotency_key != watchlist.idempotency_key


@pytest.mark.parametrize(
    "overrides",
    [
        {"caller": "operator-cli"},
        {"caller": "unknown"},
        {"operation": "paper.subscribe"},
        {"slot_id": 0},
        {"actor_id": ""},
        {"source_event_id": None},
        {"item_key": "bad\nitem"},
        {"item_key": "x" * 201},
        {"caller": "telegram-intraday", "operation": "paper.buy"},
    ],
)
def test_invalid_or_policy_forbidden_identity_input_is_rejected(overrides):
    with pytest.raises(ValueError):
        _identity(**overrides)


def test_identity_validation_rejects_scope_mismatch_and_tampering():
    identity = _identity()
    assert validate_paper_trade_write_identity(
        identity,
        caller="telegram-kium",
        operation="paper.buy",
        slot_id=2,
    ) is identity
    with pytest.raises(ValueError, match="scope does not match"):
        validate_paper_trade_write_identity(
            identity,
            caller="telegram-quant",
            operation="paper.buy",
            slot_id=2,
        )
    with pytest.raises(ValueError, match="UUID"):
        validate_paper_trade_write_identity(
            replace(identity, request_id="not-a-uuid"),
            caller="telegram-kium",
            operation="paper.buy",
            slot_id=2,
        )


def test_identity_caller_allowlist_matches_future_runtime_policies():
    target_callers = {
        policy.current_owner
        for policy in PAPER_DIRECT_WRITER_INVENTORY
        if policy.target_mode == "private-client"
    }
    normalized = {
        "paper-ui" if owner == "paper-ui" else
        "telegram-intraday" if owner == "telegram-intraday-monitor" else
        "telegram-kium" if owner == "telegram-kium-callback" else
        "telegram-quant"
        for owner in target_callers
    }
    assert normalized == PAPER_IDENTITY_CALLERS
