"""test_pending_orders.py — 장외 신호 대기 큐(순수) 검증 (hermetic)."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pending_orders as po

TODAY = date(2026, 8, 25)


def _order(price=100_000.0, alloc=1_000_000.0, at="2026-08-24 21:02:30",
           ticker="005930", slot="키움"):
    return po.make_order(slot=slot, ticker=ticker, name="삼성전자",
                         signal_price=price, alloc=alloc,
                         signal_at=datetime.strptime(at, "%Y-%m-%d %H:%M:%S"),
                         source="키움봇", tags=["키움모멘텀"])


# ─── 장 시간 ─────────────────────────────────────────


def test_market_hours_boundaries():
    assert po.is_market_hours(datetime(2026, 8, 25, 9, 0))
    assert po.is_market_hours(datetime(2026, 8, 25, 15, 30))
    assert not po.is_market_hours(datetime(2026, 8, 25, 8, 59))
    assert not po.is_market_hours(datetime(2026, 8, 25, 15, 31))


def test_weekend_is_not_market_hours():
    assert not po.is_market_hours(datetime(2026, 8, 29, 11, 0))   # 토
    assert not po.is_market_hours(datetime(2026, 8, 30, 11, 0))   # 일


def test_the_actual_offhours_slots_are_caught():
    """실제로 문제가 된 시각대 — 21시·20시·17시·06시."""
    for h in (21, 20, 17, 6):
        assert not po.is_market_hours(datetime(2026, 8, 24, h, 2))


# ─── 주문 ────────────────────────────────────────────


def test_order_carries_alloc_not_qty():
    """개장가가 달라지면 살 수 있는 수량도 달라진다. 수량을 미리 고정하면 배분이 어긋난다."""
    o = _order()
    assert "alloc" in o and "quantity" not in o


def test_expiry_uses_signal_date():
    assert not po.is_expired(_order(at="2026-08-24 21:00:00"), TODAY, max_age_days=4)
    assert po.is_expired(_order(at="2026-08-18 21:00:00"), TODAY, max_age_days=4)


def test_unknown_date_is_expired():
    """날짜를 모르면 만료로 본다 — 언제 나온 판단인지 모르는 주문은 실행하지 않는다."""
    assert po.is_expired({"signal_at": "이상한값"}, TODAY)
    assert po.is_expired({}, TODAY)


# ─── 갭 ──────────────────────────────────────────────


def test_gap_pct():
    assert po.gap_pct(100_000, 93_000) == -7.0
    assert po.gap_pct(100_000, 105_000) == 5.0
    assert po.gap_pct(0, 100) is None and po.gap_pct(100, 0) is None


def test_gap_buckets():
    assert po.gap_bucket(-5) == "갭하락"
    assert po.gap_bucket(0.5) == "갭보합"
    assert po.gap_bucket(4) == "갭상승"
    assert po.gap_bucket(None) is None


# ─── 재평가 ──────────────────────────────────────────


def test_fills_within_thresholds():
    v = po.revalidate(_order(price=100_000, alloc=1_000_000), 101_000, TODAY)
    assert v["verdict"] == "fill" and v["qty"] == 9 and v["gap"] == 1.0


def test_cancels_on_gap_down_below_stop_loss():
    """사자마자 손절될 자리에는 들어가지 않는다."""
    v = po.revalidate(_order(price=100_000), 92_000, TODAY)
    assert v["verdict"] == "cancel" and "갭 하락" in v["reason"]


def test_gap_down_exactly_at_stop_loss_cancels():
    assert po.revalidate(_order(price=100_000), 93_000, TODAY)["verdict"] == "cancel"


def test_cancels_on_gap_up_chase():
    """신호는 그 가격에서 나온 것이 아니다."""
    v = po.revalidate(_order(price=100_000), 106_000, TODAY)
    assert v["verdict"] == "cancel" and "추격" in v["reason"]


def test_cancels_when_price_missing():
    for bad in (None, 0, -1):
        assert po.revalidate(_order(), bad, TODAY)["verdict"] == "cancel"


def test_cancels_when_expired():
    v = po.revalidate(_order(at="2026-08-01 21:00:00"), 100_000, TODAY)
    assert v["verdict"] == "cancel" and "만료" in v["reason"]


def test_cancels_when_alloc_too_small():
    v = po.revalidate(_order(price=100_000, alloc=50_000), 100_000, TODAY)
    assert v["verdict"] == "cancel" and "부족" in v["reason"]


def test_filled_order_keeps_entry_tags_and_adds_queue_tags():
    """대기 체결분과 즉시 체결분을 나눠 봐야 이 장치가 도움이 됐는지 알 수 있다."""
    v = po.revalidate(_order(price=100_000, alloc=1_000_000), 96_000, TODAY)
    assert v["verdict"] == "fill"
    assert "키움모멘텀" in v["tags"] and po.TAG_FILLED in v["tags"] and "갭하락" in v["tags"]


def test_cancelled_order_gets_no_gap_tag():
    v = po.revalidate(_order(price=100_000), 80_000, TODAY)
    assert "갭하락" not in v["tags"]


def test_thresholds_are_overridable():
    v = po.revalidate(_order(price=100_000), 92_000, TODAY, gap_down_cancel=-15.0)
    assert v["verdict"] == "fill"


def test_revalidate_all_uses_price_map():
    orders = [_order(ticker="005930"), _order(ticker="000660")]
    out = po.revalidate_all(orders, {"005930": 100_000}, TODAY)
    assert out[0]["verdict"] == "fill" and out[1]["verdict"] == "cancel"


# ─── 표시 ────────────────────────────────────────────


def test_queued_message_explains_why_it_waited():
    text = po.format_queued([_order()])
    assert "실제로 살 수 없습니다" in text and "다음 개장" in text


def test_result_message_lists_both_outcomes():
    res = po.revalidate_all([_order(ticker="005930"), _order(ticker="000660")],
                            {"005930": 100_000, "000660": 80_000}, TODAY)
    text = po.format_results(res)
    assert "✅" in text and "⛔" in text


def test_empty_messages_are_empty():
    assert po.format_queued([]) == "" and po.format_results([]) == ""


# ─── 저장 ────────────────────────────────────────────


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "state" / "pending_orders.json"
    po.save([_order()], p)
    assert po.load(p)[0]["ticker"] == "005930"
    assert list(p.parent.glob(".tmp_*")) == []


def test_load_missing_or_broken_is_empty(tmp_path):
    assert po.load(tmp_path / "none.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert po.load(bad) == []


def test_enqueue_replaces_same_slot_and_ticker(tmp_path):
    """같은 종목을 두 번 사는 것보다 최신 판단 하나가 낫다."""
    p = tmp_path / "q.json"
    po.enqueue([_order(price=100_000)], p)
    merged = po.enqueue([_order(price=110_000)], p)
    assert len(merged) == 1 and merged[0]["signal_price"] == 110_000


def test_enqueue_keeps_other_tickers(tmp_path):
    p = tmp_path / "q.json"
    po.enqueue([_order(ticker="005930")], p)
    merged = po.enqueue([_order(ticker="000660")], p)
    assert {o["ticker"] for o in merged} == {"005930", "000660"}


def test_clear_empties_the_queue(tmp_path):
    p = tmp_path / "q.json"
    po.enqueue([_order()], p)
    po.clear(p)
    assert po.load(p) == []


# ─── 배선 검증 (소스 수준) ───────────────────────────


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_both_auto_buy_paths_check_market_hours():
    """키움·콴텍 두 경로 모두 장외면 큐로 보내야 한다 — 한쪽만 고치면 편향이 남는다."""
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    kium, quant = _buy_callbacks(src)
    for body in (kium, quant):
        assert "is_market_hours" in body      # 장외면 큐로
        assert "_po.enqueue" in body          # 실제로 적재까지


def test_intraday_monitor_drains_the_queue():
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    assert "revalidate_all" in src and "_drain_pending_orders" in src


def test_drain_runs_before_the_no_positions_early_return():
    """보유 포지션이 없어도 대기 주문은 체결돼야 한다."""
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    body = src[src.index("async def intraday_monitor_job"):]
    assert body.index("_drain_pending_orders") < body.index("if not positions:")


def test_drain_respects_the_daily_loss_limit():
    """한도에 걸린 슬롯이면 대기 주문도 들어가면 안 된다."""
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    fn = src[src.index("async def _drain_pending_orders"):src.index("async def intraday_monitor_job")]
    assert "_slot_hard_stop_reason" in fn


def test_drain_clears_the_queue_even_on_failure():
    """남겨 두면 다음 사이클에 또 시도한다."""
    src = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")
    fn = src[src.index("async def _drain_pending_orders"):src.index("async def intraday_monitor_job")]
    assert ".clear)" in fn


def _buy_callbacks(src):
    """키움·콴텍 자동 매수 콜백 본문 두 개."""
    kium = src[src.index("async def handle_kium_paper_callback"):
               src.index("async def handle_quant_paper_callback")]
    quant_start = src.index("async def handle_quant_paper_callback")
    rest = src[quant_start + 10:]
    quant_end = quant_start + 10 + rest.index("\nasync def ")
    return kium, src[quant_start:quant_end]
