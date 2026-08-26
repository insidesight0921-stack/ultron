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
    """Calculate the legacy slot performance response without storage I/O."""
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
                buy_queues[ticker].append((quantity, price, fee_per_unit))
                continue

            remaining = quantity
            buy_cost = 0.0
            matched_quantity = 0
            while remaining > 0 and buy_queues[ticker]:
                buy_quantity, buy_price, buy_fee_per_unit = buy_queues[ticker][0]
                take = min(remaining, buy_quantity)
                buy_cost += take * buy_price + take * buy_fee_per_unit
                remaining -= take
                matched_quantity += take
                if take >= buy_quantity:
                    buy_queues[ticker].popleft()
                else:
                    buy_queues[ticker][0] = (
                        buy_quantity - take,
                        buy_price,
                        buy_fee_per_unit,
                    )
            if matched_quantity > 0:
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

        sharpe = None
        if len(trade_returns) >= 2:
            mean_return = sum(trade_returns) / len(trade_returns)
            variance = sum(
                (value - mean_return) ** 2 for value in trade_returns
            ) / (len(trade_returns) - 1)
            standard_deviation = math.sqrt(variance) if variance > 0 else 0.0
            if standard_deviation > 0:
                sharpe = round(
                    mean_return / standard_deviation * math.sqrt(252), 2
                )

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
                "n_open_positions": int(open_info["n"]),
                "open_cost": round(float(open_info["open_cost"])),
            }
        )
    return results
