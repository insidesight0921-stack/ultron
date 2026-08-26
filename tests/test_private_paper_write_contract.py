from __future__ import annotations

from uuid import uuid4

import pytest

from private_paper_write_contract import (
    PaperTradeWriteIntent,
    normalize_paper_slot_id,
    paper_slot_scope_hash,
    validate_paper_trade_write_intent,
)
from private_write_contract import WriteContractError


def _intent(**overrides) -> PaperTradeWriteIntent:
    values = {
        "operation": "paper.buy",
        "request_id": str(uuid4()),
        "approval_id": str(uuid4()),
        "idempotency_key": f"paper-trade-{uuid4().hex}",
        "expected_version": 0,
        "payload": {
            "slot": 1,
            "ticker": "005930",
            "name": " 삼성전자 ",
            "quantity": 10,
            "price": 80_000,
            "fees": 1_200,
            "notes": " 테스트 매수 ",
        },
    }
    values.update(overrides)
    return validate_paper_trade_write_intent(**values)


def test_valid_paper_trade_is_normalized_and_scope_bound_without_plaintext_repr():
    intent = _intent()

    assert intent.slot_id == 1
    assert intent.intent.payload == {
        "slot": 1,
        "ticker": "005930",
        "name": "삼성전자",
        "quantity": 10,
        "price": 80_000.0,
        "fees": 1_200.0,
        "notes": "테스트 매수",
    }
    assert intent.scope_hash == paper_slot_scope_hash(1)
    assert len(intent.fingerprint) == 64
    assert "005930" not in repr(intent)
    assert "삼성전자" not in repr(intent)
    assert "테스트 매수" not in repr(intent)


def test_paper_trade_fingerprint_changes_with_slot_scope():
    request_id = str(uuid4())
    approval_id = str(uuid4())
    key = f"paper-trade-{uuid4().hex}"
    first = _intent(request_id=request_id, approval_id=approval_id, idempotency_key=key)
    second = _intent(
        request_id=request_id,
        approval_id=approval_id,
        idempotency_key=key,
        payload={
            "slot": 2,
            "ticker": "005930",
            "name": "삼성전자",
            "quantity": 10,
            "price": 80_000,
            "fees": 1_200,
            "notes": "테스트 매수",
        },
    )

    assert first.intent.fingerprint != second.intent.fingerprint
    assert first.fingerprint != second.fingerprint
    assert first.scope_hash != second.scope_hash


@pytest.mark.parametrize("slot", [True, 0, -1, "1", "콴텍"])
def test_paper_slot_requires_positive_integer(slot):
    with pytest.raises(WriteContractError) as exc:
        normalize_paper_slot_id(slot)
    assert exc.value.code == "invalid_paper_slot"


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"slot": 1, "ticker": "5930", "name": "삼성", "quantity": 1, "price": 1}, "invalid_paper_ticker"),
        ({"slot": 1, "ticker": "005930", "name": "", "quantity": 1, "price": 1}, "invalid_paper_payload"),
        ({"slot": 1, "ticker": "005930", "name": "삼성", "quantity": True, "price": 1}, "invalid_paper_quantity"),
        ({"slot": 1, "ticker": "005930", "name": "삼성", "quantity": 1, "price": 0}, "invalid_paper_money"),
        ({"slot": 1, "ticker": "005930", "name": "삼성", "quantity": 1, "price": 1, "fees": -1}, "invalid_paper_money"),
    ],
)
def test_invalid_paper_buy_domain_values_are_rejected(payload, code):
    with pytest.raises(WriteContractError) as exc:
        _intent(payload=payload)
    assert exc.value.code == code


def test_sell_forbids_name_and_normalizes_optional_fields():
    sell = _intent(
        operation="paper.sell",
        payload={
            "slot": 1,
            "ticker": "005930",
            "quantity": 3,
            "price": 90_000,
            "fees": 0,
            "notes": " 일부 매도 ",
        },
    )
    assert sell.intent.payload == {
        "slot": 1,
        "ticker": "005930",
        "quantity": 3,
        "price": 90_000.0,
        "fees": 0.0,
        "notes": "일부 매도",
    }

    with pytest.raises(WriteContractError) as exc:
        _intent(
            operation="paper.sell",
            payload={
                "slot": 1,
                "ticker": "005930",
                "name": "금지",
                "quantity": 3,
                "price": 90_000,
            },
        )
    assert exc.value.code == "unknown_field"


def test_non_paper_trade_operation_is_rejected():
    with pytest.raises(WriteContractError) as exc:
        _intent(operation="paper_ipo.subscribe")
    assert exc.value.code == "operation_not_allowed"
