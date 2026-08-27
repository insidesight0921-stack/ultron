"""Pure Paper performance calculations shared by DB and API boundaries."""
from __future__ import annotations

import math
from collections import defaultdict, deque


def compute_performance_stats(
    *,
    slots: list[dict],
    trades: list[dict],
    positions: list[dict],
    seed_capital: float,
) -> list[dict]:
    """Calculate the legacy slot performance response without storage I/O.

    데이터 품질 규칙(`data_quality`)에 걸린 라운드트립은 집계에서 뺀다.
    기록은 DB에 그대로 남는다.
    """
    try:
        import data_quality

        _rules = [r for r in data_quality.load_rules() if data_quality.is_exclusion(r)]
        _keys = {data_quality.rule_key(r) for r in _rules}
    except Exception:  # noqa: BLE001
        _keys = set()

    def is_excluded(slot_name, ticker, buy_at) -> bool:
        if not _keys:
            return False
        return (str(slot_name or ""), str(ticker or ""), str(buy_at or "")[:16]) in _keys

    open_map: dict[int, dict[str, float | int]] = defaultdict(
        lambda: {"n": 0, "open_cost": 0.0}
    )
    for position in positions:
        slot_id = int(position["slot_id"])
        open_map[slot_id]["n"] = int(open_map[slot_id]["n"]) + 1
        open_map[slot_id]["open_cost"] = float(open_map[slot_id]["open_cost"]) + (
            int(position["quantity"]) * float(position["avg_price"])
        )

    by_slot: dict[int, list[dict]] = defaultdict(list)
    for trade in trades:
        by_slot[int(trade["slot_id"])].append(trade)

    results = []
    for slot in slots:
        slot_id = int(slot["id"])
        slot_name = slot["name"]
        slot_seed = float(slot["allocation_pct"]) * float(seed_capital)
        slot_trades = sorted(
            by_slot.get(slot_id, []),
            key=lambda item: (item.get("executed_at") or "", item.get("id") or 0),
        )

        buy_queues: dict[str, deque] = defaultdict(deque)
        completed: list[dict[str, float]] = []
        for trade in slot_trades:
            ticker = trade["ticker"]
            quantity = int(trade["quantity"])
            price = float(trade["price"])
            fees = float(trade["fees"])
            if trade["side"] == "buy":
                fee_per_unit = fees / quantity if quantity else 0.0
                buy_queues[ticker].append(
                    (quantity, price, fee_per_unit, trade.get("executed_at"))
                )
                continue

            remaining = quantity
            buy_cost = 0.0
            matched_quantity = 0
            buy_at = None
            while remaining > 0 and buy_queues[ticker]:
                buy_quantity, buy_price, buy_fee_per_unit, queued_at = buy_queues[
                    ticker
                ][0]
                take = min(remaining, buy_quantity)
                buy_cost += take * buy_price + take * buy_fee_per_unit
                remaining -= take
                matched_quantity += take
                buy_at = buy_at or queued_at
                if take >= buy_quantity:
                    buy_queues[ticker].popleft()
                else:
                    buy_queues[ticker][0] = (
                        buy_quantity - take,
                        buy_price,
                        buy_fee_per_unit,
                        queued_at,
                    )
            if matched_quantity > 0:
                if is_excluded(slot_name, ticker, buy_at):
                    continue          # 가격 오류로 만들어진 손익은 성과에 넣지 않는다
                sell_revenue = (
                    matched_quantity * price - fees * (matched_quantity / quantity)
                )
                completed.append(
                    {"pnl": sell_revenue - buy_cost, "buy_cost": buy_cost}
                )

        n_closed = len(completed)
        open_info = open_map[slot_id]
        if n_closed == 0:
            results.append(
                {
                    "slot_id": slot_id,
                    "slot_name": slot_name,
                    "n_closed": 0,
                    "win_rate": None,
                    "total_pnl": 0,
                    "total_return_pct": 0.0,
                    "max_drawdown_pct": 0.0,
                    "sharpe": None,
                    "trade_sharpe": None,
                    "n_open_positions": int(open_info["n"]),
                    "open_cost": round(float(open_info["open_cost"])),
                }
            )
            continue

        wins = sum(1 for completed_trade in completed if completed_trade["pnl"] > 0)
        total_pnl = sum(completed_trade["pnl"] for completed_trade in completed)
        equity = slot_seed
        equity_curve = [equity]
        trade_returns = []
        for completed_trade in completed:
            equity += completed_trade["pnl"]
            equity_curve.append(equity)
            trade_returns.append(
                completed_trade["pnl"] / (completed_trade["buy_cost"] or 1.0)
            )

        peak = equity_curve[0]
        max_drawdown = 0.0
        for equity_value in equity_curve:
            peak = max(peak, equity_value)
            drawdown = (peak - equity_value) / peak if peak > 0 else 0.0
            max_drawdown = max(max_drawdown, drawdown)

        # 거래당 수익률의 샤프. **연환산하지 않는다.**
        # 이전 구현은 √252를 곱했는데, 그것은 완결 거래 1건을 거래일 1일로 본다는 뜻이다.
        # 실제로는 16~62건이 4개월에 걸쳐 있어 배수가 몇 배로 부풀었다(예: 3.42).
        # 실전 전환 기준의 "샤프 1.0"은 일간 수익률 기준이므로, 일간 마크투마켓
        # 곡선이 생기기 전까지 `sharpe`는 None으로 둔다 — 틀린 값을 채워 넣는 것보다
        # 비어 있는 편이 낫다.
        trade_sharpe = None
        if len(trade_returns) >= 2:
            mean_return = sum(trade_returns) / len(trade_returns)
            variance = sum(
                (value - mean_return) ** 2 for value in trade_returns
            ) / (len(trade_returns) - 1)
            standard_deviation = math.sqrt(variance) if variance > 0 else 0.0
            if standard_deviation > 0:
                trade_sharpe = round(mean_return / standard_deviation, 2)
        sharpe = None

        results.append(
            {
                "slot_id": slot_id,
                "slot_name": slot_name,
                "n_closed": n_closed,
                "win_rate": round(wins / n_closed * 100, 1),
                "total_pnl": round(total_pnl),
                "total_return_pct": (
                    round(total_pnl / slot_seed * 100, 2) if slot_seed > 0 else 0.0
                ),
                "max_drawdown_pct": round(max_drawdown * 100, 2),
                "sharpe": sharpe,
                "trade_sharpe": trade_sharpe,
                "n_open_positions": int(open_info["n"]),
                "open_cost": round(float(open_info["open_cost"])),
            }
        )
    return results
