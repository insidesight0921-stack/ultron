"""Read-only domain store used by the authenticated Private Data API.

Only fixed, explicitly implemented Private domains belong here.  The module
never accepts SQL, table names, or paths from an HTTP request and opens SQLite
with ``mode=ro`` plus ``query_only`` so GET routes cannot create or mutate data.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from math import isfinite
from pathlib import Path

from paper_metrics import compute_performance_stats
from storage_paths import PATHS


MAX_WATCHLIST_ITEMS = 500
MAX_SCHEDULE_EVENTS = 100
MAX_PAPER_PORTFOLIOS = 10
MAX_PAPER_POSITIONS = 1_000
MAX_PAPER_SLOTS = 20
MAX_PAPER_TRADES = 1_000
MAX_PAPER_IPO_RECORDS = 500
MAX_PAPER_IPO_STATS = 20
MAX_PAPER_ANALYTICS_TRADES = 100_000


class PrivateReadStoreError(RuntimeError):
    """A fixed Private read could not be completed safely."""


def _watchlist_item(row: sqlite3.Row) -> dict[str, str]:
    ticker = str(row["ticker"] or "").strip()
    name = " ".join(str(row["name"] or "").split())
    created_at = str(row["created_at"] or "").strip()
    if len(ticker) != 6 or not ticker.isdigit():
        raise PrivateReadStoreError("watchlist ticker 형식이 올바르지 않습니다.")
    if not name or len(name) > 100:
        raise PrivateReadStoreError("watchlist name 형식이 올바르지 않습니다.")
    if not created_at or len(created_at) > 64:
        raise PrivateReadStoreError("watchlist created_at 형식이 올바르지 않습니다.")
    return {"ticker": ticker, "name": name, "created_at": created_at}


def list_watchlist_items(
    *, db_path: Path | str = PATHS.watchlist_db
) -> list[dict[str, str]]:
    """Read the fixed watchlist table without creating a DB, table, or WAL."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise PrivateReadStoreError("Private watchlist 저장소를 사용할 수 없습니다.")

    uri = f"{path.as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            rows = con.execute(
                "SELECT ticker, name, created_at FROM watchlist "
                "ORDER BY created_at, ticker LIMIT ?",
                (MAX_WATCHLIST_ITEMS + 1,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise PrivateReadStoreError(
            "Private watchlist 저장소를 사용할 수 없습니다."
        ) from exc

    if len(rows) > MAX_WATCHLIST_ITEMS:
        raise PrivateReadStoreError("watchlist 허용 개수를 초과했습니다.")
    return [_watchlist_item(row) for row in rows]


def normalize_chat_id(chat_id: str | int) -> str:
    value = str(chat_id or "").strip()
    digits = value[1:] if value.startswith("-") else value
    if not digits.isdigit() or len(digits) > 19 or (value.startswith("-") and not digits):
        raise PrivateReadStoreError("schedule chat 범위가 올바르지 않습니다.")
    return value


def _optional_text(value, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if len(normalized) > max_length:
        raise PrivateReadStoreError(f"schedule {field} 형식이 올바르지 않습니다.")
    return normalized or None


def _schedule_event(
    row: sqlite3.Row,
    *,
    pre_notify_minutes_list: list[int],
) -> dict[str, object]:
    try:
        event_id = int(row["id"])
        completed = bool(int(row["completed"]))
        pre_notify_minutes = int(row["pre_notify_minutes"] or 0)
    except (TypeError, ValueError) as exc:
        raise PrivateReadStoreError("schedule 숫자 필드가 올바르지 않습니다.") from exc
    title = " ".join(str(row["title"] or "").split())
    when_at = str(row["when_at"] or "").strip()
    if event_id < 1 or not title or len(title) > 300:
        raise PrivateReadStoreError("schedule 기본 필드가 올바르지 않습니다.")
    try:
        datetime.fromisoformat(when_at)
    except ValueError as exc:
        raise PrivateReadStoreError("schedule when_at 형식이 올바르지 않습니다.") from exc
    if completed or pre_notify_minutes < 0:
        raise PrivateReadStoreError("schedule 상태 필드가 올바르지 않습니다.")

    rrule_freq = _optional_text(row["rrule_freq"], field="rrule_freq", max_length=16)
    if rrule_freq not in {None, "daily", "weekly", "monthly"}:
        raise PrivateReadStoreError("schedule rrule_freq 형식이 올바르지 않습니다.")
    rrule_until = _optional_text(row["rrule_until"], field="rrule_until", max_length=32)
    if rrule_until:
        try:
            datetime.fromisoformat(rrule_until)
        except ValueError as exc:
            raise PrivateReadStoreError(
                "schedule rrule_until 형식이 올바르지 않습니다."
            ) from exc
    if any(value < 1 for value in pre_notify_minutes_list):
        raise PrivateReadStoreError("schedule 사전알림 형식이 올바르지 않습니다.")

    return {
        "id": event_id,
        "title": title,
        "when_at": when_at,
        "notes": _optional_text(row["notes"], field="notes", max_length=10_000),
        "completed": False,
        "rrule_freq": rrule_freq,
        "rrule_byday": _optional_text(
            row["rrule_byday"], field="rrule_byday", max_length=32
        ),
        "rrule_until": rrule_until,
        "pre_notify_minutes": pre_notify_minutes,
        "pre_notify_minutes_list": pre_notify_minutes_list,
    }


def list_schedule_events(
    chat_id: str | int,
    *,
    upcoming_only: bool,
    db_path: Path | str = PATHS.schedule_db,
) -> list[dict[str, object]]:
    """Read active events for one Telegram chat from the fixed Private DB."""
    chat_id_n = normalize_chat_id(chat_id)
    path = Path(db_path).resolve()
    if not path.is_file():
        raise PrivateReadStoreError("Private schedule 저장소를 사용할 수 없습니다.")

    where = ["chat_id = ?", "completed = 0"]
    params: list[object] = [chat_id_n]
    if upcoming_only:
        where.append("when_at >= ?")
        params.append(datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    params.append(MAX_SCHEDULE_EVENTS + 1)
    sql = (
        "SELECT id, title, when_at, notes, completed, rrule_freq, rrule_byday, "
        "rrule_until, pre_notify_minutes FROM events WHERE "
        + " AND ".join(where)
        + " ORDER BY when_at, id LIMIT ?"
    )
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            rows = con.execute(sql, params).fetchall()
            if len(rows) > MAX_SCHEDULE_EVENTS:
                raise PrivateReadStoreError("schedule 허용 개수를 초과했습니다.")
            events = []
            for row in rows:
                pre_rows = con.execute(
                    "SELECT minutes_before FROM event_pre_notifications "
                    "WHERE event_id = ? ORDER BY minutes_before DESC",
                    (int(row["id"]),),
                ).fetchall()
                pre_list = [int(item["minutes_before"]) for item in pre_rows]
                events.append(
                    _schedule_event(row, pre_notify_minutes_list=pre_list)
                )
            return events
    except PrivateReadStoreError:
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise PrivateReadStoreError(
            "Private schedule 저장소를 사용할 수 없습니다."
        ) from exc


def _paper_portfolio(row: sqlite3.Row) -> dict[str, object]:
    portfolio_id = row["id"]
    seed_capital = row["seed_capital"]
    name = " ".join(str(row["name"] or "").split())
    created_at = str(row["created_at"] or "").strip()
    if (
        not isinstance(portfolio_id, int)
        or isinstance(portfolio_id, bool)
        or portfolio_id < 1
        or not name
        or len(name) > 100
    ):
        raise PrivateReadStoreError("paper portfolio 기본 필드가 올바르지 않습니다.")
    if (
        isinstance(seed_capital, bool)
        or not isinstance(seed_capital, (int, float))
        or not isfinite(seed_capital)
        or seed_capital < 0
    ):
        raise PrivateReadStoreError("paper portfolio 자본 필드가 올바르지 않습니다.")
    if not created_at or len(created_at) > 64:
        raise PrivateReadStoreError("paper portfolio created_at 형식이 올바르지 않습니다.")
    return {
        "id": portfolio_id,
        "name": name,
        "seed_capital": seed_capital,
        "created_at": created_at,
    }


def list_paper_portfolios(
    *, db_path: Path | str = PATHS.paper_db
) -> list[dict[str, object]]:
    """Read the fixed Paper portfolio table without initializing its schema."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.")
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            rows = con.execute(
                "SELECT id, name, seed_capital, created_at FROM portfolios "
                "ORDER BY id LIMIT ?",
                (MAX_PAPER_PORTFOLIOS + 1,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.") from exc
    if len(rows) > MAX_PAPER_PORTFOLIOS:
        raise PrivateReadStoreError("paper portfolio 허용 개수를 초과했습니다.")
    return [_paper_portfolio(row) for row in rows]


def _paper_position(row: sqlite3.Row) -> dict[str, object]:
    position_id = row["id"]
    slot_id = row["slot_id"]
    quantity = row["quantity"]
    avg_price = row["avg_price"]
    slot_name = " ".join(str(row["slot_name"] or "").split())
    ticker = str(row["ticker"] or "").strip()
    raw_name = row["name"]
    name = None if raw_name is None else " ".join(str(raw_name).split()) or None
    opened_at = str(row["opened_at"] or "").strip()
    updated_at = str(row["updated_at"] or "").strip()
    if (
        not isinstance(position_id, int)
        or isinstance(position_id, bool)
        or position_id < 1
        or not isinstance(slot_id, int)
        or isinstance(slot_id, bool)
        or slot_id < 1
        or not slot_name
        or len(slot_name) > 100
    ):
        raise PrivateReadStoreError("paper position 기본 필드가 올바르지 않습니다.")
    if len(ticker) != 6 or not ticker.isdigit():
        raise PrivateReadStoreError("paper position ticker 형식이 올바르지 않습니다.")
    if name is not None and len(name) > 100:
        raise PrivateReadStoreError("paper position name 형식이 올바르지 않습니다.")
    if (
        not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or quantity < 1
        or isinstance(avg_price, bool)
        or not isinstance(avg_price, (int, float))
        or not isfinite(avg_price)
        or avg_price <= 0
    ):
        raise PrivateReadStoreError("paper position 보유 필드가 올바르지 않습니다.")
    if (
        not opened_at
        or len(opened_at) > 64
        or not updated_at
        or len(updated_at) > 64
    ):
        raise PrivateReadStoreError("paper position 시간 필드가 올바르지 않습니다.")
    return {
        "id": position_id,
        "slot_id": slot_id,
        "ticker": ticker,
        "name": name,
        "quantity": quantity,
        "avg_price": avg_price,
        "opened_at": opened_at,
        "updated_at": updated_at,
        "slot_name": slot_name,
    }


def list_paper_positions(
    *, db_path: Path | str = PATHS.paper_db
) -> list[dict[str, object]]:
    """Read all current Paper positions from a fixed, read-only join."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.")
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            rows = con.execute(
                "SELECT p.id, p.slot_id, p.ticker, p.name, p.quantity, p.avg_price, "
                "p.opened_at, p.updated_at, s.name AS slot_name "
                "FROM positions p JOIN slots s ON p.slot_id = s.id "
                "ORDER BY s.id, p.ticker LIMIT ?",
                (MAX_PAPER_POSITIONS + 1,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.") from exc
    if len(rows) > MAX_PAPER_POSITIONS:
        raise PrivateReadStoreError("paper position 허용 개수를 초과했습니다.")
    return [_paper_position(row) for row in rows]


def _paper_slot(row: sqlite3.Row) -> dict[str, object]:
    slot_id = row["id"]
    portfolio_id = row["portfolio_id"]
    allocation_pct = row["allocation_pct"]
    current_capital = row["current_capital"]
    n_positions = row["n_positions"]
    n_trades = row["n_trades"]
    name = " ".join(str(row["name"] or "").split())
    created_at = str(row["created_at"] or "").strip()
    if (
        not isinstance(slot_id, int)
        or isinstance(slot_id, bool)
        or slot_id < 1
        or not isinstance(portfolio_id, int)
        or isinstance(portfolio_id, bool)
        or portfolio_id < 1
        or not name
        or len(name) > 100
    ):
        raise PrivateReadStoreError("paper slot 기본 필드가 올바르지 않습니다.")
    if (
        isinstance(allocation_pct, bool)
        or not isinstance(allocation_pct, (int, float))
        or not isfinite(allocation_pct)
        or not 0 <= allocation_pct <= 1
        or isinstance(current_capital, bool)
        or not isinstance(current_capital, (int, float))
        or not isfinite(current_capital)
        or current_capital < 0
    ):
        raise PrivateReadStoreError("paper slot 자본 필드가 올바르지 않습니다.")
    if (
        not isinstance(n_positions, int)
        or isinstance(n_positions, bool)
        or n_positions < 0
        or not isinstance(n_trades, int)
        or isinstance(n_trades, bool)
        or n_trades < 0
    ):
        raise PrivateReadStoreError("paper slot 집계 필드가 올바르지 않습니다.")
    if not created_at or len(created_at) > 64:
        raise PrivateReadStoreError("paper slot created_at 형식이 올바르지 않습니다.")
    return {
        "id": slot_id,
        "portfolio_id": portfolio_id,
        "name": name,
        "allocation_pct": allocation_pct,
        "current_capital": current_capital,
        "created_at": created_at,
        "n_positions": n_positions,
        "n_trades": n_trades,
    }


def list_paper_slots(
    *, db_path: Path | str = PATHS.paper_db
) -> list[dict[str, object]]:
    """Read Paper slots with the two counts used by the dashboard."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.")
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            rows = con.execute(
                "SELECT s.id, s.portfolio_id, s.name, s.allocation_pct, "
                "s.current_capital, s.created_at, "
                "(SELECT COUNT(*) FROM positions p WHERE p.slot_id = s.id) AS n_positions, "
                "(SELECT COUNT(*) FROM trades t WHERE t.slot_id = s.id) AS n_trades "
                "FROM slots s ORDER BY s.id LIMIT ?",
                (MAX_PAPER_SLOTS + 1,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.") from exc
    if len(rows) > MAX_PAPER_SLOTS:
        raise PrivateReadStoreError("paper slot 허용 개수를 초과했습니다.")
    return [_paper_slot(row) for row in rows]


def _paper_trade(row: sqlite3.Row) -> dict[str, object]:
    trade_id = row["id"]
    slot_id = row["slot_id"]
    quantity = row["quantity"]
    price = row["price"]
    fees = row["fees"]
    ticker = str(row["ticker"] or "").strip()
    slot_name = " ".join(str(row["slot_name"] or "").split())
    raw_name = row["name"]
    name = None if raw_name is None else " ".join(str(raw_name).split()) or None
    raw_notes = row["notes"]
    notes = None if raw_notes is None else str(raw_notes).strip() or None
    side = str(row["side"] or "").strip()
    executed_at = str(row["executed_at"] or "").strip()
    if (
        not isinstance(trade_id, int)
        or isinstance(trade_id, bool)
        or trade_id < 1
        or not isinstance(slot_id, int)
        or isinstance(slot_id, bool)
        or slot_id < 1
        or not slot_name
        or len(slot_name) > 100
    ):
        raise PrivateReadStoreError("paper trade 기본 필드가 올바르지 않습니다.")
    if len(ticker) != 6 or not ticker.isdigit() or side not in {"buy", "sell"}:
        raise PrivateReadStoreError("paper trade 종목·구분 필드가 올바르지 않습니다.")
    if name is not None and len(name) > 100:
        raise PrivateReadStoreError("paper trade name 형식이 올바르지 않습니다.")
    if notes is not None and len(notes) > 500:
        raise PrivateReadStoreError("paper trade notes 형식이 올바르지 않습니다.")
    if (
        not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or quantity < 1
        or isinstance(price, bool)
        or not isinstance(price, (int, float))
        or not isfinite(price)
        or price <= 0
        or isinstance(fees, bool)
        or not isinstance(fees, (int, float))
        or not isfinite(fees)
        or fees < 0
    ):
        raise PrivateReadStoreError("paper trade 체결 필드가 올바르지 않습니다.")
    if not executed_at or len(executed_at) > 64:
        raise PrivateReadStoreError("paper trade executed_at 형식이 올바르지 않습니다.")
    return {
        "id": trade_id,
        "slot_id": slot_id,
        "ticker": ticker,
        "name": name,
        "side": side,
        "quantity": quantity,
        "price": price,
        "fees": fees,
        "notes": notes,
        "executed_at": executed_at,
        "slot_name": slot_name,
    }


def list_paper_trades(
    *, db_path: Path | str = PATHS.paper_db
) -> list[dict[str, object]]:
    """Read a bounded, newest-first Paper trade history."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.")
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            rows = con.execute(
                "SELECT t.id, t.slot_id, t.ticker, t.name, t.side, t.quantity, "
                "t.price, t.fees, t.notes, t.executed_at, s.name AS slot_name "
                "FROM trades t JOIN slots s ON t.slot_id = s.id "
                "ORDER BY t.executed_at DESC, t.id DESC LIMIT ?",
                (MAX_PAPER_TRADES,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.") from exc
    return [_paper_trade(row) for row in rows]


def _optional_paper_text(
    value: object, *, field: str, max_length: int
) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or len(normalized) > max_length:
        raise PrivateReadStoreError(f"paper IPO {field} 형식이 올바르지 않습니다.")
    return normalized


def _optional_paper_number(
    value: object, *, field: str, nonnegative: bool
) -> int | float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or (nonnegative and value < 0)
    ):
        raise PrivateReadStoreError(f"paper IPO {field} 형식이 올바르지 않습니다.")
    return value


def _ipo_date(value: object, *, field: str) -> str | None:
    normalized = _optional_paper_text(value, field=field, max_length=10)
    if normalized is None:
        return None
    compact = normalized.replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise PrivateReadStoreError(f"paper IPO {field} 형식이 올바르지 않습니다.")
    return normalized


def _paper_ipo_record(row: sqlite3.Row) -> dict[str, object]:
    record_id = row["id"]
    subscribed = row["subscribed"]
    name = " ".join(str(row["name"] or "").split())
    factors = _optional_paper_text(row["factors"], field="factors", max_length=10_000)
    if (
        not isinstance(record_id, int)
        or isinstance(record_id, bool)
        or record_id < 1
        or not name
        or len(name) > 200
        or not isinstance(subscribed, int)
        or isinstance(subscribed, bool)
        or subscribed not in {0, 1}
    ):
        raise PrivateReadStoreError("paper IPO 기본 필드가 올바르지 않습니다.")
    if factors is not None:
        try:
            parsed_factors = json.loads(factors)
        except (TypeError, ValueError) as exc:
            raise PrivateReadStoreError("paper IPO factors 형식이 올바르지 않습니다.") from exc
        if not isinstance(parsed_factors, dict):
            raise PrivateReadStoreError("paper IPO factors 형식이 올바르지 않습니다.")
    score = _optional_paper_number(row["score"], field="score", nonnegative=False)
    if score is not None and not 0 <= score <= 100:
        raise PrivateReadStoreError("paper IPO score 형식이 올바르지 않습니다.")
    created_at = str(row["created_at"] or "").strip()
    updated_at = str(row["updated_at"] or "").strip()
    if not created_at or len(created_at) > 64 or not updated_at or len(updated_at) > 64:
        raise PrivateReadStoreError("paper IPO 시간 필드가 올바르지 않습니다.")
    return {
        "id": record_id,
        "name": name,
        "sub_start": _ipo_date(row["sub_start"], field="sub_start"),
        "sub_end": _ipo_date(row["sub_end"], field="sub_end"),
        "listing_date": _ipo_date(row["listing_date"], field="listing_date"),
        "grade": _optional_paper_text(row["grade"], field="grade", max_length=20),
        "score": score,
        "factors": factors,
        "subscribed": subscribed,
        "alloc_amount": _optional_paper_number(
            row["alloc_amount"], field="alloc_amount", nonnegative=True
        ),
        "listing_price": _optional_paper_number(
            row["listing_price"], field="listing_price", nonnegative=True
        ),
        "return_pct": _optional_paper_number(
            row["return_pct"], field="return_pct", nonnegative=False
        ),
        "notes": _optional_paper_text(row["notes"], field="notes", max_length=1_000),
        "created_at": created_at,
        "updated_at": updated_at,
    }


def list_paper_ipo_records(
    *, db_path: Path | str = PATHS.paper_db
) -> list[dict[str, object]]:
    """Read the bounded IPO paper ledger without exposing write operations."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.")
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            rows = con.execute(
                "SELECT id, name, sub_start, sub_end, listing_date, grade, score, "
                "factors, subscribed, alloc_amount, listing_price, return_pct, notes, "
                "created_at, updated_at FROM ipo_records "
                "ORDER BY sub_end DESC, created_at DESC LIMIT ?",
                (MAX_PAPER_IPO_RECORDS,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.") from exc
    return [_paper_ipo_record(row) for row in rows]


def _paper_ipo_stat(row: sqlite3.Row) -> dict[str, object]:
    grade = _optional_paper_text(row["grade"], field="grade", max_length=20)
    n = row["n"]
    n_pos = row["n_pos"]
    if (
        not isinstance(n, int)
        or isinstance(n, bool)
        or n < 1
        or not isinstance(n_pos, int)
        or isinstance(n_pos, bool)
        or not 0 <= n_pos <= n
    ):
        raise PrivateReadStoreError("paper IPO 통계 개수 필드가 올바르지 않습니다.")
    result: dict[str, object] = {"grade": grade, "n": n}
    for field in ("avg_return", "min_return", "max_return"):
        value = _optional_paper_number(row[field], field=field, nonnegative=False)
        if value is None:
            raise PrivateReadStoreError("paper IPO 통계 수익률 필드가 올바르지 않습니다.")
        result[field] = value
    result["n_pos"] = n_pos
    return result


def list_paper_ipo_stats(
    *, db_path: Path | str = PATHS.paper_db
) -> list[dict[str, object]]:
    """Read the same grade aggregate shown by the Paper IPO dashboard."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.")
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            rows = con.execute(
                "SELECT grade, COUNT(*) AS n, ROUND(AVG(return_pct), 2) AS avg_return, "
                "ROUND(MIN(return_pct), 2) AS min_return, "
                "ROUND(MAX(return_pct), 2) AS max_return, "
                "SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) AS n_pos "
                "FROM ipo_records WHERE return_pct IS NOT NULL AND subscribed = 1 "
                "GROUP BY grade ORDER BY grade LIMIT ?",
                (MAX_PAPER_IPO_STATS + 1,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.") from exc
    if len(rows) > MAX_PAPER_IPO_STATS:
        raise PrivateReadStoreError("paper IPO 통계 허용 개수를 초과했습니다.")
    return [_paper_ipo_stat(row) for row in rows]


def _paper_analysis_inputs(
    *, db_path: Path | str
) -> tuple[list[dict], list[dict], list[dict], float]:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.")
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            portfolio_rows = con.execute(
                "SELECT id, name, seed_capital, created_at FROM portfolios ORDER BY id LIMIT 2"
            ).fetchall()
            slot_rows = con.execute(
                "SELECT s.id, s.portfolio_id, s.name, s.allocation_pct, "
                "s.current_capital, s.created_at, "
                "(SELECT COUNT(*) FROM positions p WHERE p.slot_id = s.id) AS n_positions, "
                "(SELECT COUNT(*) FROM trades t WHERE t.slot_id = s.id) AS n_trades "
                "FROM slots s ORDER BY s.id LIMIT ?",
                (MAX_PAPER_SLOTS + 1,),
            ).fetchall()
            position_rows = con.execute(
                "SELECT p.id, p.slot_id, p.ticker, p.name, p.quantity, p.avg_price, "
                "p.opened_at, p.updated_at, s.name AS slot_name "
                "FROM positions p JOIN slots s ON p.slot_id = s.id "
                "ORDER BY s.id, p.ticker LIMIT ?",
                (MAX_PAPER_POSITIONS + 1,),
            ).fetchall()
            trade_rows = con.execute(
                "SELECT t.id, t.slot_id, t.ticker, t.name, t.side, t.quantity, "
                "t.price, t.fees, t.notes, t.executed_at, s.name AS slot_name "
                "FROM trades t JOIN slots s ON t.slot_id = s.id "
                "ORDER BY t.executed_at, t.id LIMIT ?",
                (MAX_PAPER_ANALYTICS_TRADES + 1,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise PrivateReadStoreError("Private paper 저장소를 사용할 수 없습니다.") from exc
    if len(portfolio_rows) != 1:
        raise PrivateReadStoreError("paper performance portfolio 범위가 올바르지 않습니다.")
    if len(slot_rows) > MAX_PAPER_SLOTS or len(position_rows) > MAX_PAPER_POSITIONS:
        raise PrivateReadStoreError("paper performance 보유 범위를 초과했습니다.")
    if len(trade_rows) > MAX_PAPER_ANALYTICS_TRADES:
        raise PrivateReadStoreError("paper performance 거래 범위를 초과했습니다.")
    portfolio = _paper_portfolio(portfolio_rows[0])
    return (
        [_paper_slot(row) for row in slot_rows],
        [_paper_trade(row) for row in trade_rows],
        [_paper_position(row) for row in position_rows],
        float(portfolio["seed_capital"]),
    )


def get_paper_performance(
    *, db_path: Path | str = PATHS.paper_db
) -> list[dict[str, object]]:
    """Return calculated slot metrics while keeping raw history inside the API."""
    slots, trades, positions, seed_capital = _paper_analysis_inputs(db_path=db_path)
    return compute_performance_stats(
        slots=slots,
        trades=trades,
        positions=positions,
        seed_capital=seed_capital,
    )


def get_paper_myquant_tags(
    *, db_path: Path | str = PATHS.paper_db
) -> dict[str, object]:
    """Return only aggregate MyQuant tag metrics, never the raw trade history."""
    _, trades, _, _ = _paper_analysis_inputs(db_path=db_path)
    try:
        from trade_analytics import (
            format_tag_performance,
            roundtrips_for_analysis,
            tag_performance,
        )

        # 성과 집계 — 품질 규칙 적용분을 쓴다(2026-08-31). 원본은 "오늘의 실제
        # 위험"을 보는 곳(슬롯 일일 한도)에서만 쓴다.
        roundtrips, _ = roundtrips_for_analysis(trades)
        return {
            "tags": tag_performance(roundtrips),
            "text": format_tag_performance(roundtrips),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise PrivateReadStoreError("paper myquant 계산에 실패했습니다.") from exc
