"""_run_quant — 콴텍 계산 시간 제한·멈춘 스레드 누적 방지 (2026-10-28).

10-01 quant_run이 KRX 응답 없이 끝나지 않아 월간 잡이 25일간 건너뛰어졌다.
wait_for는 기다림만 끊고 스레드는 못 죽이므로, 멈춘 호출이 있으면 새로 넣지 않는다.
"""
import asyncio
import threading

import pytest

import telegram_bot as tb


def test_timeout_then_busy_until_the_stuck_call_finishes(monkeypatch):
    monkeypatch.setattr(tb, "QUANT_REBALANCE_TIMEOUT", 0.05)
    monkeypatch.setattr(tb, "_QUANT_INFLIGHT", None)
    release = threading.Event()

    with pytest.raises(TimeoutError):
        asyncio.run(tb._run_quant(release.wait))
    with pytest.raises(tb.QuantBusy):            # 멈춘 스레드 위에 또 쌓지 않는다
        asyncio.run(tb._run_quant(lambda: 1))

    release.set()
    tb._QUANT_INFLIGHT.result(timeout=2)
    assert asyncio.run(tb._run_quant(lambda x, *, y: x + y, 1, y=2)) == 3


def test_recommend_does_not_wait_for_a_hung_ohlcv_fetch(monkeypatch):
    # 2026-11-10: with ThreadPoolExecutor가 빠져나갈 때 멈춘 fetch를 끝까지 기다려
    # "3초만 대기"가 무의미했다 — 응답 없는 KRX 한 종목이 월간 추천 전체를 붙잡는다.
    import threading
    import time

    import quant_bot

    release = threading.Event()
    monkeypatch.setattr(quant_bot, "_load_ohlcv_cache", lambda *a, **k: None)
    monkeypatch.setattr(quant_bot, "fetch_ohlcv_cached", lambda *a, **k: release.wait(30))
    try:
        t0 = time.monotonic()
        out = quant_bot.recommend_top_n(
            "Expansion", universe=[("000001", "멈춤")], fundamentals={}, market_caps={},
            weights={"Expansion": {"momentum": 1.0}})
        assert out == []
        assert time.monotonic() - t0 < 10
    finally:
        release.set()
