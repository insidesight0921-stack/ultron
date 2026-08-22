#!/usr/bin/env python3
"""
Paper Trading DB — 5단계 검증 인프라 (v3.18).

SQLite 자체 관리. 슬롯 분리 모델: 콴텍(40%) / 키움(40%) / IPO(20%).
실주문 절대 안 함 — pykrx 현재가만 가져와서 가상 매매로 6개월 검증 후 KIS 입문.

스키마:
  portfolios — 전체 자산. 시드 1회 (single row).
  slots      — 콴텍·키움·IPO 슬롯 분리 + 할당 비율·현재 자본.
  positions  — slot × ticker 단위 보유 (수량·평균가).
  trades     — 모든 매수/매도 기록 (감사 흔적).

핵심 규칙:
  - 매수: trades insert + positions upsert(가중평균) + slot.current_capital -= 비용
  - 매도: 부분/전량 모두 지원. 수량 0이면 position 행 삭제. 자본 += 수익
  - position.avg_price = (기존 비용 + 신규 비용) / 총 수량  (수수료 포함)
  - 시드는 PAPER_SEED_CAPITAL_KRW 환경변수 (기본 1억)
  - 수수료는 PAPER_FEE_BPS 환경변수 (기본 15bp = 0.15%, KIS 기준)

API:
  ensure_seed(db_path) -> dict           # portfolio + 3 슬롯 자동 생성 (idempotent)
  list_portfolios(db_path) -> list[dict]
  list_slots(db_path) -> list[dict]
  list_positions(slot_id?, db_path) -> list[dict]
  list_trades(slot_id?, limit?, db_path) -> list[dict]
  record_buy(slot, ticker, name, quantity, price, fees?, notes?) -> dict
  record_sell(slot, ticker, quantity, price, fees?, notes?) -> dict

slot 식별은 id(int) 또는 name("콴텍"/"키움"/"IPO") 모두 허용.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Iterator

from storage_paths import PATHS

HOME = Path.home()
PROJECT = HOME / "울트론" / "ai-agent"
DEFAULT_DB_PATH = PATHS.paper_db

log = logging.getLogger("paper_db")
_LOCK = Lock()

# 디폴트 (환경변수 override 가능)
DEFAULT_SEED_KRW = int(os.getenv("PAPER_SEED_CAPITAL_KRW", "100000000"))  # 1억
DEFAULT_FEE_BPS = float(os.getenv("PAPER_FEE_BPS", "15"))  # 15 bp = 0.15%

# 슬롯 정의 — plan §5 분산 슬롯 구조
SLOT_DEFINITIONS: tuple[tuple[str, float], ...] = (
    ("콴텍", 0.40),
    ("키움", 0.40),
    ("IPO", 0.20),
)


# ─── DB 헬퍼 ─────────────────────────────────────────


@contextmanager
def _conn(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        _ensure_schema(con)
        yield con
    finally:
        con.close()


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolios (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            seed_capital    REAL NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS slots (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id      INTEGER NOT NULL,
            name              TEXT NOT NULL,
            allocation_pct    REAL NOT NULL,
            current_capital   REAL NOT NULL,
            created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(portfolio_id, name),
            FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE CASCADE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id         INTEGER NOT NULL,
            ticker          TEXT NOT NULL,
            name            TEXT,
            quantity        INTEGER NOT NULL,
            avg_price       REAL NOT NULL,
            opened_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(slot_id, ticker),
            FOREIGN KEY (slot_id) REFERENCES slots(id) ON DELETE CASCADE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id         INTEGER NOT NULL,
            ticker          TEXT NOT NULL,
            name            TEXT,
            side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
            quantity        INTEGER NOT NULL,
            price           REAL NOT NULL,
            fees            REAL NOT NULL DEFAULT 0,
            notes           TEXT,
            executed_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (slot_id) REFERENCES slots(id) ON DELETE CASCADE
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_positions_slot ON positions(slot_id);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_trades_slot ON trades(slot_id, executed_at DESC);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker, executed_at DESC);")
    # ── IPO 페이퍼 청약 기록 (v3.29) ─────────────────────────────────────
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS ipo_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            sub_start       TEXT,
            sub_end         TEXT,
            listing_date    TEXT,
            grade           TEXT,
            score           REAL,
            factors         TEXT,
            subscribed      INTEGER NOT NULL DEFAULT 0,
            alloc_amount    REAL,
            listing_price   REAL,
            return_pct      REAL,
            notes           TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_ipo_sub_end ON ipo_records(sub_end DESC);")


# ─── 시드 ────────────────────────────────────────────


def ensure_seed(
    db_path: Path | str = DEFAULT_DB_PATH,
    portfolio_name: str = "default",
    seed_capital: float | None = None,
) -> dict:
    """포트폴리오 + 3 슬롯 자동 생성. 멱등 — 이미 있으면 그대로 반환.

    seed_capital은 PAPER_SEED_CAPITAL_KRW 환경변수가 디폴트 (1억).
    슬롯 비율은 plan §5 — 콴텍 40 / 키움 40 / IPO 20.
    """
    if seed_capital is None:
        seed_capital = DEFAULT_SEED_KRW

    with _LOCK, _conn(db_path) as con:
        # portfolio (UNIQUE name)
        existing = con.execute(
            "SELECT id, seed_capital FROM portfolios WHERE name = ?",
            (portfolio_name,),
        ).fetchone()

        if existing is None:
            cur = con.execute(
                "INSERT INTO portfolios(name, seed_capital) VALUES (?, ?)",
                (portfolio_name, float(seed_capital)),
            )
            portfolio_id = int(cur.lastrowid)
            log.info(f"📊 portfolio 시드: '{portfolio_name}' = {seed_capital:,.0f}원")
        else:
            portfolio_id = int(existing["id"])

        # slots (UNIQUE portfolio_id+name)
        for slot_name, pct in SLOT_DEFINITIONS:
            slot_capital = float(seed_capital) * pct
            con.execute(
                "INSERT OR IGNORE INTO slots(portfolio_id, name, allocation_pct, current_capital) "
                "VALUES (?, ?, ?, ?)",
                (portfolio_id, slot_name, pct, slot_capital),
            )

        # 결과 반환
        portfolio = con.execute(
            "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
        ).fetchone()
        slots = con.execute(
            "SELECT * FROM slots WHERE portfolio_id = ? ORDER BY id",
            (portfolio_id,),
        ).fetchall()

    return {
        "portfolio": _row_to_dict(portfolio),
        "slots": [_row_to_dict(s) for s in slots],
    }


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# ─── 슬롯 식별 헬퍼 ──────────────────────────────────


def _resolve_slot_id(con: sqlite3.Connection, slot) -> int | None:
    """slot이 int면 그대로, str이면 이름으로 lookup. 없으면 None."""
    if isinstance(slot, int):
        row = con.execute("SELECT id FROM slots WHERE id = ?", (slot,)).fetchone()
        return int(row["id"]) if row else None
    if isinstance(slot, str):
        row = con.execute(
            "SELECT id FROM slots WHERE name = ? ORDER BY id LIMIT 1",
            (slot.strip(),),
        ).fetchone()
        return int(row["id"]) if row else None
    return None


def ensure_slot(name: str, allocation_pct: float = 0.1,
                capital: float = 10_000_000.0, portfolio_id: int = 1,
                db_path: Path | str = DEFAULT_DB_PATH) -> int:
    """이름으로 슬롯 보장(멱등) — 있으면 id 반환, 없으면 생성. v3.47 마이퀀트용."""
    with _conn(db_path) as con:
        sid = _resolve_slot_id(con, name)
        if sid is not None:
            return sid
        cur = con.execute(
            "INSERT INTO slots (portfolio_id, name, allocation_pct, current_capital) "
            "VALUES (?, ?, ?, ?)",
            (portfolio_id, name.strip(), allocation_pct, capital),
        )
        return int(cur.lastrowid)


# ─── 조회 API ────────────────────────────────────────


def list_portfolios(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    with _conn(db_path) as con:
        rows = con.execute("SELECT * FROM portfolios ORDER BY id").fetchall()
    return [_row_to_dict(r) for r in rows]


def list_slots(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    with _conn(db_path) as con:
        rows = con.execute("SELECT * FROM slots ORDER BY id").fetchall()
    return [_row_to_dict(r) for r in rows]


def list_positions(
    slot=None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict]:
    with _conn(db_path) as con:
        if slot is None:
            rows = con.execute(
                "SELECT p.*, s.name AS slot_name "
                "FROM positions p JOIN slots s ON p.slot_id = s.id "
                "ORDER BY s.id, p.ticker"
            ).fetchall()
        else:
            sid = _resolve_slot_id(con, slot)
            if sid is None:
                return []
            rows = con.execute(
                "SELECT p.*, s.name AS slot_name "
                "FROM positions p JOIN slots s ON p.slot_id = s.id "
                "WHERE p.slot_id = ? ORDER BY p.ticker",
                (sid,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_trades(
    slot=None,
    limit: int = 100,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict]:
    with _conn(db_path) as con:
        if slot is None:
            rows = con.execute(
                "SELECT t.*, s.name AS slot_name "
                "FROM trades t JOIN slots s ON t.slot_id = s.id "
                "ORDER BY t.executed_at DESC, t.id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        else:
            sid = _resolve_slot_id(con, slot)
            if sid is None:
                return []
            rows = con.execute(
                "SELECT t.*, s.name AS slot_name "
                "FROM trades t JOIN slots s ON t.slot_id = s.id "
                "WHERE t.slot_id = ? "
                "ORDER BY t.executed_at DESC, t.id DESC LIMIT ?",
                (sid, int(limit)),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ─── 매매 기록 ──────────────────────────────────────


def _compute_fee(quantity: int, price: float, fee_bps: float | None = None) -> float:
    """수수료 = 수량 × 가격 × bps/10000."""
    if fee_bps is None:
        fee_bps = DEFAULT_FEE_BPS
    return float(int(quantity) * float(price) * float(fee_bps) / 10000.0)


def record_buy(
    slot,
    ticker: str,
    name: str | None,
    quantity: int,
    price: float,
    fees: float | None = None,
    notes: str | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict:
    """매수 기록.

    1) trades insert
    2) positions upsert — 가중평균(avg_price = (기존 비용 + 신규 비용) / 총 수량)
       비용엔 수수료 포함.
    3) slot.current_capital -= 수량 × 가격 + 수수료

    잔고 부족 시 ValueError. 수량/가격 ≤ 0이면 ValueError.
    """
    qty = int(quantity)
    px = float(price)
    if qty <= 0:
        raise ValueError(f"매수 수량은 양수여야: {qty}")
    if px <= 0:
        raise ValueError(f"매수 가격은 양수여야: {px}")

    if fees is None:
        fees = _compute_fee(qty, px)
    fees = float(fees)
    total_cost = qty * px + fees

    with _LOCK, _conn(db_path) as con:
        sid = _resolve_slot_id(con, slot)
        if sid is None:
            raise ValueError(f"존재하지 않는 슬롯: {slot!r}")

        # 잔고 체크
        slot_row = con.execute(
            "SELECT current_capital FROM slots WHERE id = ?", (sid,)
        ).fetchone()
        current = float(slot_row["current_capital"])
        if total_cost > current:
            raise ValueError(
                f"슬롯 잔고 부족 — 필요 {total_cost:,.0f}원 / 보유 {current:,.0f}원"
            )

        # trades insert
        con.execute(
            "INSERT INTO trades(slot_id, ticker, name, side, quantity, price, fees, notes) "
            "VALUES (?, ?, ?, 'buy', ?, ?, ?, ?)",
            (sid, str(ticker), name, qty, px, fees, notes),
        )

        # positions upsert (가중평균)
        existing = con.execute(
            "SELECT quantity, avg_price FROM positions WHERE slot_id = ? AND ticker = ?",
            (sid, str(ticker)),
        ).fetchone()
        if existing is None:
            new_avg = (qty * px + fees) / qty  # 수수료 분배
            con.execute(
                "INSERT INTO positions(slot_id, ticker, name, quantity, avg_price) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, str(ticker), name, qty, new_avg),
            )
        else:
            old_qty = int(existing["quantity"])
            old_avg = float(existing["avg_price"])
            new_qty = old_qty + qty
            new_avg = (old_qty * old_avg + qty * px + fees) / new_qty
            con.execute(
                "UPDATE positions SET quantity = ?, avg_price = ?, "
                "updated_at = datetime('now', 'localtime') "
                "WHERE slot_id = ? AND ticker = ?",
                (new_qty, new_avg, sid, str(ticker)),
            )

        # slot.current_capital
        con.execute(
            "UPDATE slots SET current_capital = current_capital - ? WHERE id = ?",
            (total_cost, sid),
        )

    log.info(f"🟢 매수 #{ticker} {qty}주 @ {px:,.0f}원 (수수료 {fees:,.0f}, 슬롯={slot})")
    return {
        "side": "buy", "slot_id": sid, "ticker": str(ticker),
        "quantity": qty, "price": px, "fees": fees, "total_cost": total_cost,
    }


def record_sell(
    slot,
    ticker: str,
    quantity: int,
    price: float,
    fees: float | None = None,
    notes: str | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict:
    """매도 기록.

    1) 보유 수량 검증
    2) trades insert
    3) positions update (qty 차감, 0이면 행 삭제)
    4) slot.current_capital += 수량 × 가격 - 수수료
    """
    qty = int(quantity)
    px = float(price)
    if qty <= 0:
        raise ValueError(f"매도 수량은 양수여야: {qty}")
    if px <= 0:
        raise ValueError(f"매도 가격은 양수여야: {px}")

    if fees is None:
        fees = _compute_fee(qty, px)
    fees = float(fees)
    proceeds = qty * px - fees

    with _LOCK, _conn(db_path) as con:
        sid = _resolve_slot_id(con, slot)
        if sid is None:
            raise ValueError(f"존재하지 않는 슬롯: {slot!r}")

        pos = con.execute(
            "SELECT quantity, avg_price FROM positions WHERE slot_id = ? AND ticker = ?",
            (sid, str(ticker)),
        ).fetchone()
        if pos is None:
            raise ValueError(f"보유 중인 포지션 없음: {ticker} (슬롯={slot})")
        held = int(pos["quantity"])
        if qty > held:
            raise ValueError(f"매도 수량 초과 — 요청 {qty} / 보유 {held}")

        # trades insert
        con.execute(
            "INSERT INTO trades(slot_id, ticker, name, side, quantity, price, fees, notes) "
            "VALUES (?, ?, ?, 'sell', ?, ?, ?, ?)",
            (sid, str(ticker), None, qty, px, fees, notes),
        )

        # positions update
        new_qty = held - qty
        if new_qty == 0:
            con.execute(
                "DELETE FROM positions WHERE slot_id = ? AND ticker = ?",
                (sid, str(ticker)),
            )
        else:
            con.execute(
                "UPDATE positions SET quantity = ?, "
                "updated_at = datetime('now', 'localtime') "
                "WHERE slot_id = ? AND ticker = ?",
                (new_qty, sid, str(ticker)),
            )

        # slot.current_capital
        con.execute(
            "UPDATE slots SET current_capital = current_capital + ? WHERE id = ?",
            (proceeds, sid),
        )

    log.info(f"🔴 매도 #{ticker} {qty}주 @ {px:,.0f}원 (수수료 {fees:,.0f}, 슬롯={slot})")
    return {
        "side": "sell", "slot_id": sid, "ticker": str(ticker),
        "quantity": qty, "price": px, "fees": fees, "proceeds": proceeds,
    }


# ─── 요약 ────────────────────────────────────────────


def slot_summary(
    slot,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict | None:
    """슬롯 단위 요약 — 자본 + 보유 포지션 수 + 거래 수."""
    with _conn(db_path) as con:
        sid = _resolve_slot_id(con, slot)
        if sid is None:
            return None
        slot_row = con.execute("SELECT * FROM slots WHERE id = ?", (sid,)).fetchone()
        n_positions = con.execute(
            "SELECT COUNT(*) AS c FROM positions WHERE slot_id = ?", (sid,)
        ).fetchone()["c"]
        n_trades = con.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE slot_id = ?", (sid,)
        ).fetchone()["c"]
    out = _row_to_dict(slot_row)
    out["n_positions"] = int(n_positions)
    out["n_trades"] = int(n_trades)
    return out


# ─── CLI ────────────────────────────────────────────



# ─── IPO 페이퍼 청약 CRUD (v3.29) ────────────────────────────────────────────

def ipo_upsert(
    name: str,
    sub_start: str | None = None,
    sub_end: str | None = None,
    listing_date: str | None = None,
    grade: str | None = None,
    score: float | None = None,
    factors: dict | None = None,
    subscribed: bool = False,
    alloc_amount: float | None = None,
    db_path=DEFAULT_DB_PATH,
) -> dict:
    """IPO 종목 신규 등록 또는 업데이트 (name 기준 upsert)."""
    import json as _json
    factors_json = _json.dumps(factors, ensure_ascii=False) if factors else None
    with _conn(db_path) as con:
        existing = con.execute(
            "SELECT id FROM ipo_records WHERE name=?", (name,)
        ).fetchone()
        if existing:
            con.execute(
                """UPDATE ipo_records SET
                    sub_start=COALESCE(?,sub_start),
                    sub_end=COALESCE(?,sub_end),
                    listing_date=COALESCE(?,listing_date),
                    grade=COALESCE(?,grade),
                    score=COALESCE(?,score),
                    factors=COALESCE(?,factors),
                    subscribed=?,
                    alloc_amount=COALESCE(?,alloc_amount),
                    updated_at=datetime('now','localtime')
                WHERE name=?""",
                (sub_start, sub_end, listing_date, grade, score,
                 factors_json, int(subscribed), alloc_amount, name),
            )
            row_id = existing[0]
        else:
            cur = con.execute(
                """INSERT INTO ipo_records
                   (name,sub_start,sub_end,listing_date,grade,score,factors,subscribed,alloc_amount)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (name, sub_start, sub_end, listing_date, grade, score,
                 factors_json, int(subscribed), alloc_amount),
            )
            row_id = cur.lastrowid
        row = con.execute("SELECT * FROM ipo_records WHERE id=?", (row_id,)).fetchone()
        return _row_to_dict(row)


def ipo_close(name: str, listing_price: float, db_path=DEFAULT_DB_PATH) -> dict | None:
    """상장 결과 입력 — 공모가 대비 수익률 자동 계산."""
    import json as _json
    with _conn(db_path) as con:
        row = con.execute(
            "SELECT id, factors FROM ipo_records WHERE name=?", (name,)
        ).fetchone()
        if not row:
            return None
        row_id, factors_json = row
        return_pct = None
        if factors_json:
            try:
                fac = _json.loads(factors_json)
                offer_price = fac.get("final_price") or fac.get("offer_price")
                if offer_price and offer_price > 0:
                    return_pct = round((listing_price - offer_price) / offer_price * 100, 2)
            except Exception:
                pass
        con.execute(
            """UPDATE ipo_records SET
               listing_price=?, return_pct=?,
               updated_at=datetime('now','localtime')
            WHERE id=?""",
            (listing_price, return_pct, row_id),
        )
        row = con.execute("SELECT * FROM ipo_records WHERE id=?", (row_id,)).fetchone()
        return _row_to_dict(row)


def ipo_list(db_path=DEFAULT_DB_PATH) -> list:
    """전체 IPO 기록 (최신 sub_end 순)."""
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT * FROM ipo_records ORDER BY sub_end DESC, created_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def ipo_stats(db_path=DEFAULT_DB_PATH) -> list:
    """등급별 평균 수익률 집계 (상장 완료 + 청약한 건)."""
    with _conn(db_path) as con:
        rows = con.execute(
            """SELECT grade,
                      COUNT(*) as n,
                      ROUND(AVG(return_pct),2) as avg_return,
                      ROUND(MIN(return_pct),2) as min_return,
                      ROUND(MAX(return_pct),2) as max_return,
                      SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) as n_pos
               FROM ipo_records
               WHERE return_pct IS NOT NULL AND subscribed=1
               GROUP BY grade ORDER BY grade"""
        ).fetchall()
        cols = ["grade", "n", "avg_return", "min_return", "max_return", "n_pos"]
        return [dict(zip(cols, r)) for r in rows]




# ─── 성과 통계 (v3.31) ──────────────────────────────────────────────────────


def performance_stats(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """슬롯별 실현 손익 통계 — 승률·총수익률·MDD·샤프.

    매수/매도를 FIFO 방식으로 매칭하여 완결된 거래 단위 PnL을 산출.
    반환 필드: slot_id, slot_name, n_closed, win_rate(%),
               total_pnl(원), total_return_pct, max_drawdown_pct, sharpe(연환산),
               n_open_positions, open_cost(원 미실현 매입금액)
    """
    import math
    from collections import defaultdict, deque

    with _conn(db_path) as con:
        slots = con.execute("SELECT * FROM slots ORDER BY id").fetchall()
        all_trades = con.execute(
            "SELECT t.*, s.name AS slot_name "
            "FROM trades t JOIN slots s ON t.slot_id = s.id "
            "ORDER BY t.executed_at, t.id"
        ).fetchall()
        seed_row = con.execute(
            "SELECT seed_capital FROM portfolios LIMIT 1"
        ).fetchone()
        open_positions = con.execute(
            "SELECT slot_id, COUNT(*) AS n, "
            "SUM(quantity * avg_price) AS open_cost "
            "FROM positions GROUP BY slot_id"
        ).fetchall()

    seed_capital = float(seed_row["seed_capital"]) if seed_row else 100_000_000

    open_map: dict[int, dict] = {}
    for row in open_positions:
        open_map[int(row["slot_id"])] = {
            "n": int(row["n"]),
            "open_cost": float(row["open_cost"] or 0),
        }

    by_slot: dict[int, list] = defaultdict(list)
    for t in all_trades:
        by_slot[int(t["slot_id"])].append(_row_to_dict(t))

    results = []
    for slot_row in slots:
        slot_id = int(slot_row["id"])
        slot_name = slot_row["name"]
        slot_seed = float(slot_row["allocation_pct"]) * seed_capital
        trades = by_slot.get(slot_id, [])

        buy_queues: dict[str, deque] = defaultdict(deque)
        completed: list[dict] = []

        for t in trades:
            ticker = t["ticker"]
            qty = int(t["quantity"])
            price = float(t["price"])
            fees = float(t["fees"])

            if t["side"] == "buy":
                fpu = fees / qty if qty else 0.0
                buy_queues[ticker].append((qty, price, fpu))
            else:
                remaining = qty
                buy_cost = 0.0
                matched_qty = 0
                while remaining > 0 and buy_queues[ticker]:
                    bqty, bprice, bfpu = buy_queues[ticker][0]
                    take = min(remaining, bqty)
                    buy_cost += take * bprice + take * bfpu
                    remaining -= take
                    matched_qty += take
                    if take >= bqty:
                        buy_queues[ticker].popleft()
                    else:
                        buy_queues[ticker][0] = (bqty - take, bprice, bfpu)
                if matched_qty > 0:
                    sell_rev = matched_qty * price - fees * (matched_qty / qty)
                    pnl = sell_rev - buy_cost
                    completed.append({"pnl": pnl, "buy_cost": buy_cost})

        n_closed = len(completed)
        open_info = open_map.get(slot_id, {"n": 0, "open_cost": 0.0})

        if n_closed == 0:
            results.append({
                "slot_id": slot_id, "slot_name": slot_name,
                "n_closed": 0, "win_rate": None,
                "total_pnl": 0, "total_return_pct": 0.0,
                "max_drawdown_pct": 0.0, "sharpe": None,
                "n_open_positions": open_info["n"],
                "open_cost": round(open_info["open_cost"]),
            })
            continue

        wins = sum(1 for c in completed if c["pnl"] > 0)
        win_rate = round(wins / n_closed * 100, 1)
        total_pnl = sum(c["pnl"] for c in completed)
        total_return_pct = round(total_pnl / slot_seed * 100, 2) if slot_seed > 0 else 0.0

        equity = slot_seed
        equity_curve = [equity]
        trade_returns: list[float] = []
        for c in completed:
            equity += c["pnl"]
            equity_curve.append(equity)
            r = c["pnl"] / (c["buy_cost"] or 1.0)
            trade_returns.append(r)

        peak = equity_curve[0]
        max_dd = 0.0
        for e in equity_curve:
            if e > peak:
                peak = e
            dd = (peak - e) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        sharpe = None
        if len(trade_returns) >= 2:
            mean_r = sum(trade_returns) / len(trade_returns)
            var_r = sum((r - mean_r) ** 2 for r in trade_returns) / (len(trade_returns) - 1)
            std_r = math.sqrt(var_r) if var_r > 0 else 0.0
            if std_r > 0:
                sharpe = round(mean_r / std_r * math.sqrt(252), 2)

        results.append({
            "slot_id": slot_id, "slot_name": slot_name,
            "n_closed": n_closed, "win_rate": win_rate,
            "total_pnl": round(total_pnl), "total_return_pct": total_return_pct,
            "max_drawdown_pct": round(max_dd * 100, 2), "sharpe": sharpe,
            "n_open_positions": open_info["n"],
            "open_cost": round(open_info["open_cost"]),
        })

    return results

def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    sub.add_parser("slots")
    sub.add_parser("positions")

    p_buy = sub.add_parser("buy")
    p_buy.add_argument("slot")
    p_buy.add_argument("ticker")
    p_buy.add_argument("name")
    p_buy.add_argument("--qty", type=int, required=True)
    p_buy.add_argument("--price", type=float, required=True)

    p_sell = sub.add_parser("sell")
    p_sell.add_argument("slot")
    p_sell.add_argument("ticker")
    p_sell.add_argument("--qty", type=int, required=True)
    p_sell.add_argument("--price", type=float, required=True)

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "seed":
        out = ensure_seed()
        print(out)
    elif args.cmd == "slots":
        for s in list_slots():
            print(s)
    elif args.cmd == "positions":
        for p in list_positions():
            print(p)
    elif args.cmd == "buy":
        ensure_seed()
        out = record_buy(args.slot, args.ticker, args.name, args.qty, args.price)
        print(out)
    elif args.cmd == "sell":
        out = record_sell(args.slot, args.ticker, args.qty, args.price)
        print(out)


if __name__ == "__main__":
    _cli()
