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


# ─── 시세 소스 분리 (2026-08-28) ─────────────────────
#
# `_get_realtime_price`는 네이버 실패 시 pykrx '최신 일봉 종가'로 폴백한다.
# 장중에 그것은 전 거래일 종가다. 매수에 쓰면 스테일 진입가가 폴백 경로로 되살아난다.


def test_buy_paths_use_the_no_fallback_price_source():
    """매수는 안 사면 그만이다 — 모르는 가격으로 사지 않는다."""
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    fn = src[src.index("def _get_execution_price"):
             src.index("def ", src.index("def _get_execution_price") + 10)]
    assert "_get_pykrx_price" not in fn.split('"""')[-1]   # 본문에 폴백이 없다
    assert "_get_naver_price" in fn


def test_no_buy_path_uses_the_fallback_source():
    """체결가를 만드는 조회 블록에는 폴백 소스가 없어야 한다.

    같은 콜백 안에서도 퇴출청산(매도)은 `_get_realtime_price`를 쓰므로,
    함수 전체가 아니라 **resolve_batch로 들어가는 조회 구간**만 본다.
    """
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    for body in _buy_callbacks(src):
        start = body.index("_live = ")
        block = body[start:body.index("resolve_batch", start)]
        assert "_get_execution_price" in block
        assert "_get_realtime_price" not in block
        assert "_get_pykrx_price" not in block


def test_pending_drain_also_refuses_the_fallback():
    """대기 주문 체결도 매수다 — 일봉 종가로 채우면 대기시킨 의미가 없다."""
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    fn = src[src.index("async def _drain_pending_orders"):
             src.index("async def intraday_monitor_job")]
    assert "_get_execution_price" in fn and "_get_pykrx_price" not in fn


def test_exit_paths_keep_the_fallback():
    """청산은 가격을 모르면 판정 자체를 못 한다 — 폴백을 남긴다."""
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    monitor = src[src.index("async def intraday_monitor_job"):]
    assert "_get_pykrx_price" in monitor


def test_exit_does_not_fabricate_a_break_even_price():
    """평균단가로 채우면 손익 0%인 가짜 청산이 기록된다."""
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    assert "_get_pykrx_price, ticker) or avg" not in src


def _buy_callbacks(src):
    kium = src[src.index("async def handle_kium_paper_callback"):
               src.index("async def handle_quant_paper_callback")]
    quant_start = src.index("async def handle_quant_paper_callback")
    rest = src[quant_start + 10:]
    return kium, src[quant_start:quant_start + 10 + rest.index("\nasync def ")]
