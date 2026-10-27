"""paper_view — 화면 평가·주석 계산 (2026-10-27)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import exit_rules  # noqa: E402
import paper_view as pv  # noqa: E402


def _pos(**kw):
    base = {"slot_id": 2, "slot_name": "키움", "ticker": "005930", "name": "삼성전자",
            "quantity": 10, "avg_price": 100.0}
    base.update(kw)
    return base


def test_exit_line_is_stop_until_trailing_arms():
    assert pv.exit_line(None) == (exit_rules.STOP_PCT, "손절선")
    assert pv.exit_line(exit_rules.TRAIL_ARM_PCT - 0.001) == (exit_rules.STOP_PCT, "손절선")


def test_exit_line_follows_peak_once_armed():
    level, kind = pv.exit_line(0.15)
    assert kind == "트레일링"
    assert abs(level - (0.15 - exit_rules.TRAIL_DROP_PCT)) < 1e-12


def test_position_view_values_at_market_price():
    v = pv.position_view(_pos(), 110.0)
    assert v["book"] == 1000 and v["value"] == 1100 and v["pnl"] == 100
    assert v["pnl_pct"] == 10.0
    # 피크 기록이 없어도 지금 수익률이 피크다 — 봇의 update_peak와 같게
    assert v["exit_kind"] == "트레일링"
    assert abs(v["room"] - exit_rules.TRAIL_DROP_PCT * 100) < 1e-9


def test_position_view_room_to_stop():
    v = pv.position_view(_pos(), 97.0)
    assert v["exit_kind"] == "손절선"
    assert abs(v["room"] - (-3.0 - exit_rules.STOP_PCT * 100)) < 1e-9


def test_position_view_without_price_keeps_book_and_says_so():
    v = pv.position_view(_pos(), None)
    assert v["value"] == 1000 and v["price"] is None and v["pnl"] is None and v["room"] is None


def test_slot_equity_adds_stock_value_to_cash():
    slots = [{"name": "키움", "current_capital": 500.0}, {"name": "콴텍", "current_capital": 300.0}]
    rows = [pv.position_view(_pos(), 110.0), pv.position_view(_pos(ticker="000660"), None)]
    out = {s["name"]: s for s in pv.slot_equity(slots, rows)}
    assert out["키움"] == {**slots[0], "cash": 500, "stock": 2100, "equity": 2600}
    assert out["콴텍"]["stock"] == 0 and out["콴텍"]["equity"] == 300


def test_annotate_trades_fills_name_and_sell_pnl():
    trades = [
        {"slot_name": "키움", "ticker": "005930", "name": "", "side": "sell",
         "executed_at": "2026-10-20 10:00:00"},
        {"slot_name": "키움", "ticker": "005930", "name": "삼성전자", "side": "buy",
         "executed_at": "2026-10-10 10:00:00"},
    ]
    rts = [{"slot": "키움", "ticker": "005930", "sell_at": "2026-10-20 10:00:00",
            "pnl": -1234.4, "ret": -0.0567}]
    out = pv.annotate_trades(trades, rts, [], pv.name_map(trades))
    assert out[0]["name"] == "삼성전자"
    assert out[0]["rt_pnl"] == -1234 and out[0]["rt_ret"] == -5.67
    assert "excluded_reason" not in out[0]
    assert "rt_pnl" not in out[1]                 # 매수 행엔 손익이 없다


def test_annotate_trades_marks_excluded_sell():
    trades = [{"slot_name": "키움", "ticker": "1", "name": "A", "side": "sell", "executed_at": "t"}]
    rt = {"slot": "키움", "ticker": "1", "sell_at": "t", "pnl": 10.0, "ret": 0.1,
          "excluded_reason": "범위 밖"}
    out = pv.annotate_trades(trades, [rt], [rt], {})
    assert out[0]["excluded_reason"] == "범위 밖" and out[0]["rt_ret"] == 10.0


def test_annotate_trades_does_not_cross_slots():
    trades = [{"slot_name": "콴텍", "ticker": "1", "name": "A", "side": "sell", "executed_at": "t"}]
    rts = [{"slot": "키움", "ticker": "1", "sell_at": "t", "pnl": 10.0, "ret": 0.1}]
    assert "rt_pnl" not in pv.annotate_trades(trades, rts, [], {})[0]


def test_win_rate_matches_count_table():
    # 20/53 = 37.74 → 37.7, 9/23 = 39.13 → 39.1. 반올림된 승률을 가중하면 38.1이 나오지만
    # 실제 29/76 = 38.16 → 38.2 (전략 비교 표와 같아야 한다)
    perf = [{"n_closed": 53, "win_rate": 37.7}, {"n_closed": 23, "win_rate": 39.1},
            {"n_closed": 0, "win_rate": None}]
    assert pv.win_rate(perf) == 38.2
    assert pv.win_rate([{"n_closed": 0, "win_rate": None}]) is None
