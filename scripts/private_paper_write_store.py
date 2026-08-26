#!/usr/bin/env python3
"""Default-disabled, slot-scoped Paper buy/sell store for isolated tests."""
from __future__ import annotations

import sqlite3

import paper_db
from private_paper_write_contract import (
    PAPER_TRADE_OPERATIONS,
    PaperTradeWriteIntent,
    normalize_paper_slot_id,
    paper_slot_scope_hash,
    validate_paper_trade_write_intent,
)
from private_scoped_write_store import ScopedApprovalWriteStore, ScopedWriteIntent
from private_write_contract import WriteContractError, WriteIntent


class PaperTradeWriteStore(ScopedApprovalWriteStore):
    resource_name = "paper-trade"
    allowed_operations = PAPER_TRADE_OPERATIONS

    def _ensure_domain_schema(self, con: sqlite3.Connection) -> None:
        paper_db._ensure_schema(con)

    def _normalize_bound_intent(self, intent: object) -> ScopedWriteIntent:
        if not isinstance(intent, PaperTradeWriteIntent):
            raise WriteContractError(
                "invalid_paper_intent", "validated slot-scoped Paper intent is required"
            )
        normalized = validate_paper_trade_write_intent(
            operation=intent.intent.operation,
            request_id=intent.intent.request_id,
            idempotency_key=intent.intent.idempotency_key,
            approval_id=intent.intent.approval_id,
            expected_version=intent.intent.expected_version,
            payload=intent.intent.payload,
        )
        return ScopedWriteIntent(
            intent=normalized.intent,
            scope_hash=normalized.scope_hash,
            scope_value=str(normalized.slot_id),
            fingerprint=normalized.fingerprint,
        )

    def _normalize_scope(self, scope_value: object) -> tuple[str, str]:
        slot_id = normalize_paper_slot_id(scope_value)
        return str(slot_id), paper_slot_scope_hash(slot_id)

    @staticmethod
    def _fee(payload: dict[str, object]) -> float:
        if "fees" in payload:
            return float(payload["fees"])
        return paper_db._compute_fee(int(payload["quantity"]), float(payload["price"]))

    @staticmethod
    def _slot(con: sqlite3.Connection, slot_id: int) -> sqlite3.Row:
        row = con.execute(
            "SELECT id,current_capital FROM slots WHERE id=?", (slot_id,)
        ).fetchone()
        if row is None:
            raise WriteContractError("paper_slot_not_found", "Paper slot was not found")
        return row

    def _apply_domain(
        self,
        con: sqlite3.Connection,
        intent: WriteIntent,
        scope_value: str,
    ) -> tuple[dict[str, object], bool]:
        payload = intent.payload
        slot_id = int(scope_value)
        if int(payload["slot"]) != slot_id:
            raise WriteContractError("scope_conflict", "Paper payload slot differs from scope")
        slot = self._slot(con, slot_id)
        ticker = str(payload["ticker"])
        quantity = int(payload["quantity"])
        price = float(payload["price"])
        fees = self._fee(payload)

        if intent.operation == "paper.buy":
            total_cost = quantity * price + fees
            if total_cost > float(slot["current_capital"]):
                raise WriteContractError(
                    "paper_insufficient_capital", "Paper slot capital is insufficient"
                )
            trade = con.execute(
                """INSERT INTO trades(
                       slot_id,ticker,name,side,quantity,price,fees,notes
                   ) VALUES (?,?,?,'buy',?,?,?,?)""",
                (
                    slot_id,
                    ticker,
                    payload["name"],
                    quantity,
                    price,
                    fees,
                    payload.get("notes"),
                ),
            )
            position = con.execute(
                "SELECT quantity,avg_price FROM positions WHERE slot_id=? AND ticker=?",
                (slot_id, ticker),
            ).fetchone()
            if position is None:
                con.execute(
                    """INSERT INTO positions(slot_id,ticker,name,quantity,avg_price)
                       VALUES (?,?,?,?,?)""",
                    (
                        slot_id,
                        ticker,
                        payload["name"],
                        quantity,
                        total_cost / quantity,
                    ),
                )
            else:
                old_quantity = int(position["quantity"])
                new_quantity = old_quantity + quantity
                new_average = (
                    old_quantity * float(position["avg_price"])
                    + quantity * price
                    + fees
                ) / new_quantity
                con.execute(
                    """UPDATE positions
                       SET name=?,quantity=?,avg_price=?,
                           updated_at=datetime('now','localtime')
                       WHERE slot_id=? AND ticker=?""",
                    (
                        payload["name"],
                        new_quantity,
                        new_average,
                        slot_id,
                        ticker,
                    ),
                )
            con.execute(
                "UPDATE slots SET current_capital=current_capital-? WHERE id=?",
                (total_cost, slot_id),
            )
            return (
                {
                    "side": "buy",
                    "trade_id": int(trade.lastrowid),
                    "slot_id": slot_id,
                    "ticker": ticker,
                    "quantity": quantity,
                    "price": price,
                    "fees": fees,
                    "total_cost": total_cost,
                },
                True,
            )

        position = con.execute(
            "SELECT quantity FROM positions WHERE slot_id=? AND ticker=?",
            (slot_id, ticker),
        ).fetchone()
        if position is None:
            raise WriteContractError(
                "paper_position_not_found", "Paper position was not found"
            )
        held = int(position["quantity"])
        if quantity > held:
            raise WriteContractError(
                "paper_quantity_exceeds_position", "Paper sell quantity exceeds position"
            )
        proceeds = quantity * price - fees
        if proceeds < 0:
            raise WriteContractError(
                "paper_negative_proceeds", "Paper sell proceeds must not be negative"
            )
        trade = con.execute(
            """INSERT INTO trades(slot_id,ticker,name,side,quantity,price,fees,notes)
               VALUES (?,? ,NULL,'sell',?,?,?,?)""",
            (slot_id, ticker, quantity, price, fees, payload.get("notes")),
        )
        remaining = held - quantity
        if remaining == 0:
            con.execute(
                "DELETE FROM positions WHERE slot_id=? AND ticker=?", (slot_id, ticker)
            )
        else:
            con.execute(
                """UPDATE positions SET quantity=?,updated_at=datetime('now','localtime')
                   WHERE slot_id=? AND ticker=?""",
                (remaining, slot_id, ticker),
            )
        con.execute(
            "UPDATE slots SET current_capital=current_capital+? WHERE id=?",
            (proceeds, slot_id),
        )
        return (
            {
                "side": "sell",
                "trade_id": int(trade.lastrowid),
                "slot_id": slot_id,
                "ticker": ticker,
                "quantity": quantity,
                "price": price,
                "fees": fees,
                "proceeds": proceeds,
            },
            True,
        )
