"""
paper_db 단위 테스트 (v3.18 — paper trading 인프라).

- 스키마 idempotent
- 시드 (portfolio + 3 슬롯 자동 생성, 멱등)
- 슬롯 식별 (id / 이름)
- record_buy: 가중평균, 잔고 차감, 잔고 부족
- record_sell: 부분/전량, 자본 추가, 보유 검증
- 거래 내역 정렬·필터
- edge cases (수량 0/음수, 가격 0/음수, 없는 슬롯·종목)
"""
from __future__ import annotations
import sqlite3
from pathlib import Path

import pytest

import paper_db as pdb


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """매 테스트마다 fresh paper.db."""
    return tmp_path / "test_paper.db"


@pytest.fixture
def seeded(db: Path) -> Path:
    """시드 완료된 db."""
    pdb.ensure_seed(db_path=db, seed_capital=100_000_000)
    return db


# ─── 시드 ────────────────────────────────────────────


def test_ensure_seed_creates_portfolio_and_slots(db):
    out = pdb.ensure_seed(db_path=db)
    assert out["portfolio"]["seed_capital"] == pdb.DEFAULT_SEED_KRW
    assert len(out["slots"]) == 3
    names = {s["name"] for s in out["slots"]}
    assert names == {"콴텍", "키움", "IPO"}


def test_ensure_seed_slot_allocations(db):
    out = pdb.ensure_seed(db_path=db, seed_capital=100_000_000)
    by_name = {s["name"]: s for s in out["slots"]}
    assert by_name["콴텍"]["current_capital"] == 40_000_000
    assert by_name["키움"]["current_capital"] == 40_000_000
    assert by_name["IPO"]["current_capital"] == 20_000_000
    assert by_name["콴텍"]["allocation_pct"] == 0.40
    assert by_name["IPO"]["allocation_pct"] == 0.20


def test_ensure_seed_idempotent(db):
    """두 번 호출해도 에러 없고 같은 결과."""
    out1 = pdb.ensure_seed(db_path=db, seed_capital=100_000_000)
    out2 = pdb.ensure_seed(db_path=db, seed_capital=999_999_999)  # 다른 값
    # 첫 시드 보존 (UNIQUE name → 두 번째는 무시)
    assert out2["portfolio"]["seed_capital"] == 100_000_000
    assert len(out2["slots"]) == 3


def test_ensure_seed_custom_capital(db):
    out = pdb.ensure_seed(db_path=db, seed_capital=10_000_000)
    by_name = {s["name"]: s for s in out["slots"]}
    assert by_name["콴텍"]["current_capital"] == 4_000_000


# ─── 슬롯 식별 ──────────────────────────────────────


def test_resolve_slot_by_name(seeded):
    slots = pdb.list_slots(db_path=seeded)
    by_name = {s["name"]: s["id"] for s in slots}
    pos_by_name = pdb.list_positions(slot="콴텍", db_path=seeded)
    pos_by_id = pdb.list_positions(slot=by_name["콴텍"], db_path=seeded)
    assert pos_by_name == pos_by_id  # 동일


def test_unknown_slot_name_returns_empty(seeded):
    assert pdb.list_positions(slot="없는슬롯", db_path=seeded) == []
    assert pdb.list_trades(slot="없는슬롯", db_path=seeded) == []


# ─── 매수 ────────────────────────────────────────────


def test_record_buy_creates_position(seeded):
    out = pdb.record_buy(
        "콴텍", "005930", "삼성전자",
        quantity=10, price=80_000, fees=0,
        db_path=seeded,
    )
    assert out["side"] == "buy"
    assert out["total_cost"] == 10 * 80_000

    positions = pdb.list_positions(slot="콴텍", db_path=seeded)
    assert len(positions) == 1
    p = positions[0]
    assert p["ticker"] == "005930"
    assert p["quantity"] == 10
    assert p["avg_price"] == 80_000


def test_record_buy_decreases_slot_capital(seeded):
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000, fees=0,
                   db_path=seeded)
    summary = pdb.slot_summary("콴텍", db_path=seeded)
    assert summary["current_capital"] == 40_000_000 - 800_000


def test_record_buy_includes_fees_in_cost(seeded):
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000, fees=1_000,
                   db_path=seeded)
    summary = pdb.slot_summary("콴텍", db_path=seeded)
    assert summary["current_capital"] == 40_000_000 - 800_000 - 1_000


def test_record_buy_default_fee_15bps(seeded):
    """수수료 명시 안 하면 15bp 자동 계산."""
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000,
                   db_path=seeded)
    trades = pdb.list_trades(slot="콴텍", db_path=seeded)
    expected_fee = 10 * 80_000 * 15 / 10000  # 1,200원
    assert abs(trades[0]["fees"] - expected_fee) < 0.01


def test_record_buy_weighted_avg(seeded):
    """추가 매수 시 평균가 가중평균. 수수료는 평균가에 포함."""
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000, fees=0,
                   db_path=seeded)
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=100_000, fees=0,
                   db_path=seeded)
    positions = pdb.list_positions(slot="콴텍", db_path=seeded)
    p = positions[0]
    assert p["quantity"] == 20
    # (10×80000 + 10×100000) / 20 = 90000
    assert p["avg_price"] == 90_000


def test_record_buy_insufficient_balance_raises(seeded):
    with pytest.raises(ValueError, match="잔고 부족"):
        pdb.record_buy("IPO", "005930", "삼성전자",
                       quantity=1000, price=80_000, fees=0,
                       db_path=seeded)
    # IPO 슬롯은 2천만원, 1000주 × 8만원 = 8천만원 → 초과


def test_record_buy_zero_quantity_raises(seeded):
    with pytest.raises(ValueError):
        pdb.record_buy("콴텍", "005930", "삼성전자",
                       quantity=0, price=80_000, db_path=seeded)


def test_record_buy_negative_price_raises(seeded):
    with pytest.raises(ValueError):
        pdb.record_buy("콴텍", "005930", "삼성전자",
                       quantity=10, price=-1, db_path=seeded)


def test_record_buy_unknown_slot_raises(seeded):
    with pytest.raises(ValueError, match="존재하지 않는 슬롯"):
        pdb.record_buy("ABC", "005930", "삼성전자",
                       quantity=10, price=80_000, db_path=seeded)


# ─── 매도 ────────────────────────────────────────────


def test_record_sell_partial_keeps_position(seeded):
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000, fees=0, db_path=seeded)
    pdb.record_sell("콴텍", "005930", quantity=4, price=90_000, fees=0,
                    db_path=seeded)
    positions = pdb.list_positions(slot="콴텍", db_path=seeded)
    assert len(positions) == 1
    assert positions[0]["quantity"] == 6
    # 평균가는 매도해도 변하지 않음
    assert positions[0]["avg_price"] == 80_000


def test_record_sell_full_removes_position(seeded):
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000, fees=0, db_path=seeded)
    pdb.record_sell("콴텍", "005930", quantity=10, price=90_000, fees=0,
                    db_path=seeded)
    positions = pdb.list_positions(slot="콴텍", db_path=seeded)
    assert positions == []


def test_record_sell_increases_capital(seeded):
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000, fees=0, db_path=seeded)
    pdb.record_sell("콴텍", "005930", quantity=10, price=90_000, fees=0,
                    db_path=seeded)
    summary = pdb.slot_summary("콴텍", db_path=seeded)
    # 4천만 - 80만 + 90만 = 4천 + 10만 = 40,100,000
    assert summary["current_capital"] == 40_000_000 + 100_000


def test_record_sell_exceeds_holdings_raises(seeded):
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000, fees=0, db_path=seeded)
    with pytest.raises(ValueError, match="매도 수량 초과"):
        pdb.record_sell("콴텍", "005930", quantity=20, price=90_000,
                        db_path=seeded)


def test_record_sell_no_position_raises(seeded):
    with pytest.raises(ValueError, match="보유 중인 포지션 없음"):
        pdb.record_sell("콴텍", "005930", quantity=1, price=80_000,
                        db_path=seeded)


def test_record_sell_negative_quantity_raises(seeded):
    with pytest.raises(ValueError):
        pdb.record_sell("콴텍", "005930", quantity=-1, price=80_000,
                        db_path=seeded)


# ─── 거래 내역 ──────────────────────────────────────


def test_list_trades_records_buy_and_sell(seeded):
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000, fees=0, db_path=seeded)
    pdb.record_sell("콴텍", "005930", quantity=4, price=90_000, fees=0,
                    db_path=seeded)
    trades = pdb.list_trades(slot="콴텍", db_path=seeded)
    assert len(trades) == 2
    sides = [t["side"] for t in trades]
    # 최신 먼저 (sell이 먼저)
    assert sides[0] == "sell"
    assert sides[1] == "buy"


def test_list_trades_filters_by_slot(seeded):
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000, fees=0, db_path=seeded)
    pdb.record_buy("키움", "000660", "SK하이닉스",
                   quantity=5, price=200_000, fees=0, db_path=seeded)
    quantec = pdb.list_trades(slot="콴텍", db_path=seeded)
    kium = pdb.list_trades(slot="키움", db_path=seeded)
    all_t = pdb.list_trades(db_path=seeded)
    assert len(quantec) == 1 and quantec[0]["ticker"] == "005930"
    assert len(kium) == 1 and kium[0]["ticker"] == "000660"
    assert len(all_t) == 2


def test_list_trades_includes_slot_name(seeded):
    pdb.record_buy("키움", "005930", "삼성전자",
                   quantity=1, price=80_000, fees=0, db_path=seeded)
    trades = pdb.list_trades(db_path=seeded)
    assert trades[0]["slot_name"] == "키움"


def test_list_positions_filters_by_slot(seeded):
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000, fees=0, db_path=seeded)
    pdb.record_buy("키움", "000660", "SK하이닉스",
                   quantity=5, price=200_000, fees=0, db_path=seeded)
    q_pos = pdb.list_positions(slot="콴텍", db_path=seeded)
    all_pos = pdb.list_positions(db_path=seeded)
    assert len(q_pos) == 1
    assert len(all_pos) == 2


# ─── 슬롯 요약 ──────────────────────────────────────


def test_slot_summary_counts(seeded):
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=10, price=80_000, fees=0, db_path=seeded)
    pdb.record_buy("콴텍", "000660", "SK하이닉스",
                   quantity=5, price=200_000, fees=0, db_path=seeded)
    pdb.record_sell("콴텍", "005930", quantity=10, price=90_000, fees=0,
                    db_path=seeded)
    summary = pdb.slot_summary("콴텍", db_path=seeded)
    assert summary["n_positions"] == 1  # SK하이닉스만 남음
    assert summary["n_trades"] == 3  # 매수 2 + 매도 1


def test_slot_summary_unknown_returns_none(seeded):
    assert pdb.slot_summary("ABC", db_path=seeded) is None


# ─── 스키마 idempotent ──────────────────────────────


def test_schema_idempotent(seeded):
    """두 번째 연결도 정상 동작 (CREATE IF NOT EXISTS)."""
    pdb.list_slots(db_path=seeded)  # 다시 _conn → _ensure_schema 호출
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=1, price=80_000, fees=0, db_path=seeded)
    # 깨지지 않으면 OK


def test_indices_created(seeded):
    con = sqlite3.connect(str(seeded))
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    names = {r[0] for r in cur.fetchall()}
    con.close()
    assert "idx_positions_slot" in names
    assert "idx_trades_slot" in names
    assert "idx_trades_ticker" in names


# ─── 통합 시나리오 ──────────────────────────────────


def test_full_buy_sell_pnl_scenario(seeded):
    """매수 → 가격 상승 → 매도 → 자본 + 손익 계산."""
    # 콴텍 4천만에서 시작
    pdb.record_buy("콴텍", "005930", "삼성전자",
                   quantity=100, price=80_000, fees=0, db_path=seeded)
    # 자본 = 4천만 - 800만 = 3,200만
    summary = pdb.slot_summary("콴텍", db_path=seeded)
    assert summary["current_capital"] == 32_000_000

    pdb.record_sell("콴텍", "005930", quantity=100, price=100_000, fees=0,
                    db_path=seeded)
    # 자본 = 3,200만 + 1,000만 = 4,200만 (이익 +200만)
    summary = pdb.slot_summary("콴텍", db_path=seeded)
    assert summary["current_capital"] == 42_000_000
    assert summary["n_positions"] == 0


# ─── performance_stats (v3.31) ──────────────────────────────────────────────


class TestPerformanceStats:
    """performance_stats() FIFO PnL 검증 (v3.31 신규)."""

    def test_no_trades_returns_zero_stats(self, seeded):
        stats = pdb.performance_stats(db_path=seeded)
        assert len(stats) == 3  # 슬롯 3개 모두 반환
        for s in stats:
            assert s["n_closed"] == 0
            assert s["win_rate"] is None
            assert s["total_pnl"] == 0
            assert s["sharpe"] is None

    def test_single_win_trade(self, seeded):
        pdb.record_buy("콴텍", "005930", "삼성전자",
                       quantity=10, price=80_000, fees=0, db_path=seeded)
        pdb.record_sell("콴텍", "005930", quantity=10, price=90_000, fees=0,
                        db_path=seeded)
        stats = pdb.performance_stats(db_path=seeded)
        q = next(s for s in stats if s["slot_name"] == "콴텍")
        assert q["n_closed"] == 1
        assert q["win_rate"] == 100.0
        assert q["total_pnl"] == 100_000   # (90000 - 80000) × 10
        assert q["total_return_pct"] > 0

    def test_single_loss_trade(self, seeded):
        pdb.record_buy("콴텍", "005930", "삼성전자",
                       quantity=10, price=90_000, fees=0, db_path=seeded)
        pdb.record_sell("콴텍", "005930", quantity=10, price=80_000, fees=0,
                        db_path=seeded)
        stats = pdb.performance_stats(db_path=seeded)
        q = next(s for s in stats if s["slot_name"] == "콴텍")
        assert q["n_closed"] == 1
        assert q["win_rate"] == 0.0
        assert q["total_pnl"] == -100_000   # (80000 - 90000) × 10
        assert q["max_drawdown_pct"] > 0

    def test_fifo_partial_sell(self, seeded):
        # 1차 매수 10주 @80,000 → 5주 매도 @90,000
        # 5주 남음 → open position
        pdb.record_buy("콴텍", "005930", "삼성전자",
                       quantity=10, price=80_000, fees=0, db_path=seeded)
        pdb.record_sell("콴텍", "005930", quantity=5, price=90_000, fees=0,
                        db_path=seeded)
        stats = pdb.performance_stats(db_path=seeded)
        q = next(s for s in stats if s["slot_name"] == "콴텍")
        assert q["n_closed"] == 1
        assert q["total_pnl"] == 50_000   # (90000 - 80000) × 5
        assert q["n_open_positions"] == 1

    def test_open_positions_counted(self, seeded):
        pdb.record_buy("콴텍", "005930", "삼성전자",
                       quantity=5, price=80_000, fees=0, db_path=seeded)
        pdb.record_buy("콴텍", "000660", "SK하이닉스",
                       quantity=3, price=200_000, fees=0, db_path=seeded)
        stats = pdb.performance_stats(db_path=seeded)
        q = next(s for s in stats if s["slot_name"] == "콴텍")
        assert q["n_open_positions"] == 2
        assert q["open_cost"] == 5 * 80_000 + 3 * 200_000

    def test_trade_sharpe_requires_at_least_two_trades(self, seeded):
        # 완결 거래 1건 → trade_sharpe = None
        pdb.record_buy("콴텍", "005930", "삼성전자",
                       quantity=10, price=80_000, fees=0, db_path=seeded)
        pdb.record_sell("콴텍", "005930", quantity=10, price=90_000, fees=0,
                        db_path=seeded)
        stats = pdb.performance_stats(db_path=seeded)
        q = next(s for s in stats if s["slot_name"] == "콴텍")
        assert q["trade_sharpe"] is None, "거래 1건으로는 표준편차 계산 불가 → None이어야 함"

        # 완결 거래 2건 → trade_sharpe 숫자
        pdb.record_buy("콴텍", "000660", "SK하이닉스",
                       quantity=5, price=200_000, fees=0, db_path=seeded)
        pdb.record_sell("콴텍", "000660", quantity=5, price=210_000, fees=0,
                        db_path=seeded)
        stats2 = pdb.performance_stats(db_path=seeded)
        q2 = next(s for s in stats2 if s["slot_name"] == "콴텍")
        assert q2["trade_sharpe"] is not None

    def test_annualized_sharpe_is_not_reported(self, seeded):
        """거래 1건을 거래일 1일로 보고 √252를 곱하던 값은 실제의 몇 배였다.

        실전 전환 기준의 「샤프 1.0」은 일간 수익률 기준이다. 일간 마크투마켓 곡선이
        생기기 전까지는 비워 둔다 — 틀린 값이 채워져 있으면 기준을 통과한 것처럼 보인다.
        """
        for ticker, buy, sell in (("005930", 80_000, 90_000), ("000660", 200_000, 210_000)):
            pdb.record_buy("콴텍", ticker, ticker, quantity=5, price=buy, fees=0,
                           db_path=seeded)
            pdb.record_sell("콴텍", ticker, quantity=5, price=sell, fees=0,
                            db_path=seeded)
        q = next(s for s in pdb.performance_stats(db_path=seeded)
                 if s["slot_name"] == "콴텍")
        assert q["sharpe"] is None
        assert abs(q["trade_sharpe"]) < 3, "연환산 배수가 남아 있으면 값이 부풀어 있다"

    def test_other_slots_unaffected(self, seeded):
        pdb.record_buy("콴텍", "005930", "삼성전자",
                       quantity=10, price=80_000, fees=0, db_path=seeded)
        pdb.record_sell("콴텍", "005930", quantity=10, price=90_000, fees=0,
                        db_path=seeded)
        stats = pdb.performance_stats(db_path=seeded)
        for s in stats:
            if s["slot_name"] != "콴텍":
                assert s["n_closed"] == 0, f"{s['slot_name']} 슬롯에 영향 없어야"
