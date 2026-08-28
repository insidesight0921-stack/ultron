"""test_execution_price.py — 체결가 확정(순수) 검증 (hermetic)."""
from __future__ import annotations

from pathlib import Path

import execution_price as ep


def _item(ticker="005930", price=100_000, name="삼성전자"):
    return {"ticker": ticker, "name": name, "price": price}


# ─── 괴리 ────────────────────────────────────────────


def test_drift_is_measured_against_the_scan_price():
    assert ep.drift_pct(100_000, 110_000) == 10.0
    assert ep.drift_pct(100_000, 90_000) == -10.0


def test_drift_none_on_missing_values():
    for a, b in ((None, 100), (100, None), (0, 100), (100, 0)):
        assert ep.drift_pct(a, b) is None


# ─── 체결가 확정 ─────────────────────────────────────


def test_fill_uses_the_live_price_not_the_scan_price():
    """일봉 종가로 사면 실제로는 살 수 없었던 가격에 산 것이 된다."""
    r = ep.resolve(_item(price=100_000), 120_000, alloc=1_000_000)
    assert r["verdict"] == "fill" and r["price"] == 120_000


def test_quantity_is_recomputed_from_the_live_price():
    """스캔가로 계산한 수량을 그대로 쓰면 배정 금액이 어긋난다."""
    r = ep.resolve(_item(price=100_000), 120_000, alloc=1_000_000)
    assert r["qty"] == 8            # 1,000,000 // 120,000, 스캔가 기준이면 10
    assert r["qty"] * r["price"] <= 1_000_000


def test_missing_live_price_defers_instead_of_falling_back():
    """모르는 가격으로 사느니 안 사는 편이 낫다."""
    for bad in (None, 0, -1):
        r = ep.resolve(_item(), bad, alloc=1_000_000)
        assert r["verdict"] == "defer" and r["qty"] == 0
        assert "일봉 종가로 대체하지 않고" in r["reason"]


def test_defer_when_allocation_cannot_buy_one_share():
    r = ep.resolve(_item(price=100_000), 2_000_000, alloc=1_000_000)
    assert r["verdict"] == "defer" and "부족" in r["reason"]


def test_fill_records_the_drift():
    r = ep.resolve(_item(price=100_000), 105_000, alloc=1_000_000)
    assert r["drift"] == 5.0 and "+5.00%" in r["reason"]


def test_signal_price_is_kept_for_the_record():
    r = ep.resolve(_item(price=100_000), 120_000, alloc=1_000_000)
    assert r["signal_price"] == 100_000


# ─── 배치 ────────────────────────────────────────────


def test_batch_resolves_each_ticker_independently():
    items = [_item("005930", 100_000), _item("000660", 200_000, "SK하이닉스")]
    out = ep.resolve_batch(items, {"005930": 110_000}, alloc=1_000_000)
    assert out[0]["verdict"] == "fill" and out[1]["verdict"] == "defer"


def test_batch_empty_is_safe():
    assert ep.resolve_batch([], {}, alloc=1_000_000) == []


# ─── 큰 괴리 알림 ────────────────────────────────────


def test_large_drift_is_reported_not_blocked():
    """스캔 후 실제로 움직였을 수도 있다 — 그 자체가 정보다."""
    out = ep.resolve_batch([_item(price=100_000)], {"005930": 130_000}, alloc=10_000_000)
    assert out[0]["verdict"] == "fill"
    assert len(ep.large_drifts(out)) == 1


def test_small_drift_is_quiet():
    out = ep.resolve_batch([_item(price=100_000)], {"005930": 103_000}, alloc=10_000_000)
    assert ep.large_drifts(out) == []


def test_large_drift_threshold_is_adjustable():
    out = ep.resolve_batch([_item(price=100_000)], {"005930": 105_000}, alloc=10_000_000)
    assert len(ep.large_drifts(out, threshold=3.0)) == 1


# ─── 메시지 ──────────────────────────────────────────


def test_message_explains_why_a_deferral_is_safer():
    out = ep.resolve_batch([_item()], {}, alloc=1_000_000)
    text = ep.format_resolution(out)
    assert "대기 큐" in text and "실제로는 살 수 없었던 가격" in text


def test_message_lists_large_drifts():
    out = ep.resolve_batch([_item(price=100_000)], {"005930": 130_000}, alloc=10_000_000)
    assert "100,000 → 130,000원" in ep.format_resolution(out)


def test_message_empty_when_everything_is_ordinary():
    out = ep.resolve_batch([_item(price=100_000)], {"005930": 101_000}, alloc=10_000_000)
    assert ep.format_resolution(out) == ""


# ─── 배선 검증 ───────────────────────────────────────


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_both_auto_buy_paths_resolve_execution_price():
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    assert src.count("resolve_batch") == 2


def test_auto_buy_uses_the_realtime_price_helper():
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    assert src.count("_get_realtime_price") >= 2


def test_deferred_items_go_to_the_pending_queue():
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    assert src.count('r["verdict"] == "defer"') == 2
