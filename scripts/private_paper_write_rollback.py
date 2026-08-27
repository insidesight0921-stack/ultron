#!/usr/bin/env python3
"""Isolated Paper writer rollback rehearsal on a verified backup copy."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import paper_db
from private_data_security import backup_database, verify_database
from private_paper_write_contract import validate_paper_trade_write_intent
from private_paper_write_store import PaperTradeWriteStore
from private_write_rollback import (
    DEFAULT_MAX_BACKUP_AGE,
    PrivateWriteRollbackError,
    _load_verified_backup,
    _require_empty_private_workspace,
    _sha256,
)


class PaperWriteRollbackError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Paper write rollback rehearsal failed: {code}")
        self.code = code


@dataclass(frozen=True)
class PaperWriteRollbackReport:
    api_buy_applied: bool
    api_sell_applied: bool
    api_round_trip_balanced: bool
    legacy_buy_resumed: bool
    legacy_sell_resumed: bool
    service_rollback_preserved_api_writes: bool
    emergency_restore_verified: bool
    backup_unchanged: bool
    artifacts_preserved: bool = True

    @property
    def verified(self) -> bool:
        return all(
            (
                self.api_buy_applied,
                self.api_sell_applied,
                self.api_round_trip_balanced,
                self.legacy_buy_resumed,
                self.legacy_sell_resumed,
                self.service_rollback_preserved_api_writes,
                self.emergency_restore_verified,
                self.backup_unchanged,
                self.artifacts_preserved,
            )
        )


def _rehearsal_scope(db_path: Path) -> tuple[int, str, str, float]:
    try:
        with sqlite3.connect(db_path) as connection:
            slot = connection.execute(
                "SELECT id,current_capital FROM slots "
                "WHERE current_capital>=1000 ORDER BY id LIMIT 1"
            ).fetchone()
            if slot is None:
                raise PaperWriteRollbackError("rehearsal_slot_unavailable")
            used = {
                str(row[0])
                for row in connection.execute(
                    "SELECT ticker FROM trades UNION SELECT ticker FROM positions"
                )
            }
    except sqlite3.Error as exc:
        raise PaperWriteRollbackError("rehearsal_slot_unavailable") from exc
    candidates = (
        f"{value:06d}"
        for value in range(999_999, 899_999, -1)
        if f"{value:06d}" not in used
    )
    try:
        return int(slot[0]), next(candidates), next(candidates), float(slot[1])
    except StopIteration as exc:
        raise PaperWriteRollbackError("rehearsal_ticker_unavailable") from exc


def _apply_api_trade(
    writer: PaperTradeWriteStore,
    *,
    operation: str,
    slot_id: int,
    ticker: str,
    name: str | None = None,
) -> object:
    request_id = str(uuid.uuid4())
    approval_id = str(uuid.uuid4())
    payload: dict[str, object] = {
        "slot": slot_id,
        "ticker": ticker,
        "quantity": 1,
        "price": 1_000,
        "fees": 0,
        "notes": "Paper rollback rehearsal",
    }
    if operation == "paper.buy":
        payload["name"] = name
    intent = validate_paper_trade_write_intent(
        operation=operation,
        request_id=request_id,
        approval_id=approval_id,
        idempotency_key=f"paper-rollback-{uuid.uuid4().hex}",
        expected_version=writer.current_version(slot_id),
        payload=payload,
    )
    writer.submit(intent)
    writer.approve(approval_id, request_id=request_id, scope_value=slot_id)
    return writer.apply(approval_id, request_id=request_id, scope_value=slot_id)


def _trade_sides(db_path: Path, slot_id: int, ticker: str) -> tuple[str, ...]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT side FROM trades WHERE slot_id=? AND ticker=? ORDER BY id",
            (slot_id, ticker),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _position_absent(db_path: Path, slot_id: int, ticker: str) -> bool:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM positions WHERE slot_id=? AND ticker=?",
            (slot_id, ticker),
        ).fetchone()
    return row is None


def _slot_capital(db_path: Path, slot_id: int) -> float:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT current_capital FROM slots WHERE id=?", (slot_id,)
        ).fetchone()
    if row is None:
        raise PaperWriteRollbackError("rehearsal_slot_unavailable")
    return float(row[0])


def rehearse_paper_write_rollback(
    manifest_path: Path,
    workspace: Path,
    *,
    now: datetime | None = None,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
) -> PaperWriteRollbackReport:
    """Exercise API and legacy Paper writers only on verified backup copies."""
    checked_at = now or datetime.now().astimezone()
    if checked_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if max_backup_age <= timedelta(0):
        raise ValueError("max_backup_age must be positive")
    try:
        work = _require_empty_private_workspace(Path(workspace).resolve())
        backup_path, baseline_signature = _load_verified_backup(
            Path(manifest_path).resolve(),
            now=checked_at,
            max_backup_age=max_backup_age,
            database_name="paper.db",
        )
    except PrivateWriteRollbackError as exc:
        raise PaperWriteRollbackError(exc.code) from exc
    backup_before = (_sha256(backup_path), backup_path.stat().st_mtime_ns)

    active_dir = work / "active-clone"
    restored_dir = work / "emergency-restore"
    active_dir.mkdir(mode=0o700)
    restored_dir.mkdir(mode=0o700)
    active_db = active_dir / "paper.db"
    restored_db = restored_dir / "paper.db"
    backup_database(backup_path, active_db)

    slot_id, api_ticker, legacy_ticker, initial_capital = _rehearsal_scope(active_db)
    writer = PaperTradeWriteStore(active_db, writes_enabled=True)
    api_buy = _apply_api_trade(
        writer,
        operation="paper.buy",
        slot_id=slot_id,
        ticker=api_ticker,
        name="API rollback rehearsal",
    )
    api_sell = _apply_api_trade(
        writer,
        operation="paper.sell",
        slot_id=slot_id,
        ticker=api_ticker,
    )
    api_sides_before_rollback = _trade_sides(active_db, slot_id, api_ticker)
    api_balanced = (
        _position_absent(active_db, slot_id, api_ticker)
        and _slot_capital(active_db, slot_id) == initial_capital
    )

    legacy_buy = paper_db.record_buy(
        slot_id,
        legacy_ticker,
        "Legacy rollback rehearsal",
        1,
        1_000,
        fees=0,
        notes="Paper rollback rehearsal",
        db_path=active_db,
    )
    legacy_sell = paper_db.record_sell(
        slot_id,
        legacy_ticker,
        1,
        1_000,
        fees=0,
        notes="Paper rollback rehearsal",
        db_path=active_db,
    )
    legacy_sides = _trade_sides(active_db, slot_id, legacy_ticker)
    api_sides_after_rollback = _trade_sides(active_db, slot_id, api_ticker)

    active_signature = verify_database(active_db)
    backup_database(backup_path, restored_db)
    restored_signature = verify_database(restored_db)
    backup_after = (_sha256(backup_path), backup_path.stat().st_mtime_ns)

    report = PaperWriteRollbackReport(
        api_buy_applied=(
            getattr(api_buy, "state", None) == "applied"
            and api_sides_before_rollback == ("buy", "sell")
        ),
        api_sell_applied=(
            getattr(api_sell, "state", None) == "applied"
            and api_sides_before_rollback == ("buy", "sell")
        ),
        api_round_trip_balanced=api_balanced,
        legacy_buy_resumed=(
            legacy_buy.get("side") == "buy" and legacy_sides == ("buy", "sell")
        ),
        legacy_sell_resumed=(
            legacy_sell.get("side") == "sell"
            and legacy_sides == ("buy", "sell")
            and _position_absent(active_db, slot_id, legacy_ticker)
        ),
        service_rollback_preserved_api_writes=(
            api_sides_after_rollback == api_sides_before_rollback
        ),
        emergency_restore_verified=(
            restored_signature == baseline_signature
            and active_signature != baseline_signature
        ),
        backup_unchanged=backup_before == backup_after,
    )
    if not report.verified:
        raise PaperWriteRollbackError("rehearsal_verification_failed")
    return report
