"""test_slot_hard_stop.py — 슬롯별 일일 손실 한도(순수) 검증 (hermetic)."""
from __future__ import annotations

import json
from pathlib import Path

import slot_hard_stop as hs

DAY = "2026-08-25"


def _rt(slot, pnl, sell_at=DAY):
    return {"slot": slot, "ticker": "005930", "pnl": pnl, "sell_at": sell_at}


# ─── 일자별 실현손익 ─────────────────────────────────


def test_realized_by_slot_counts_only_that_day():
    rts = [_rt("키움", -100), _rt("키움", -50), _rt("키움", -9999, "2026-08-24"),
           _rt("콴텍", 30)]
    out = hs.realized_by_slot(rts, DAY)
    assert out == {"키움": -150.0, "콴텍": 30.0}


def test_realized_ignores_open_positions():
    assert hs.realized_by_slot([{"slot": "키움", "pnl": -500, "sell_at": None}], DAY) == {}


def test_realized_handles_datetime_strings():
    assert hs.realized_by_slot([_rt("키움", -10, DAY + " 14:22")], DAY) == {"키움": -10.0}


# ─── 판정 ────────────────────────────────────────────


def test_trips_when_loss_exceeds_limit():
    v = hs.evaluate_slot("키움", -400_000, 10_000_000, limit_pct=-3.0)
    assert v["tripped"] and v["loss_pct"] == -4.0


def test_does_not_trip_within_limit():
    v = hs.evaluate_slot("키움", -200_000, 10_000_000, limit_pct=-3.0)
    assert not v["tripped"] and v["loss_pct"] == -2.0


def test_exact_limit_trips():
    """한도와 같으면 발동한다 — 경계에서 애매하게 두지 않는다."""
    assert hs.evaluate_slot("키움", -300_000, 10_000_000, limit_pct=-3.0)["tripped"]


def test_profit_never_trips():
    assert not hs.evaluate_slot("콴텍", 500_000, 10_000_000)["tripped"]


def test_limit_sign_is_normalized():
    """+3.0으로 줘도 손실 한도로 해석한다."""
    assert hs.evaluate_slot("키움", -400_000, 10_000_000, limit_pct=3.0)["tripped"]


def test_unknown_capital_does_not_trip():
    """분모를 모르는 상태에서 매수를 막으면 원인 모를 차단이 된다."""
    v = hs.evaluate_slot("키움", -1_000_000, 0)
    assert not v["tripped"] and v["loss_pct"] is None
    assert "자본" in v["reason"]


def test_evaluate_all_sorted_by_loss():
    out = hs.evaluate_all({"키움": -400_000, "콴텍": 10_000},
                          {"키움": 10_000_000, "콴텍": 10_000_000})
    assert [v["slot"] for v in out] == ["키움", "콴텍"]


def test_evaluate_all_includes_slots_without_trades():
    out = hs.evaluate_all({}, {"IPO": 20_000_000})
    assert len(out) == 1 and out[0]["slot"] == "IPO" and not out[0]["tripped"]


# ─── 상태 ────────────────────────────────────────────


def test_state_resets_on_new_day():
    old = {"date": "2026-08-24", "tripped": {"키움": {"loss_pct": -5}}}
    assert hs.normalize_state(old, DAY) == {"date": DAY, "tripped": {}}


def test_state_survives_same_day():
    st = {"date": DAY, "tripped": {"키움": {"loss_pct": -5}}}
    assert hs.normalize_state(st, DAY)["tripped"] == {"키움": {"loss_pct": -5}}


def test_state_handles_garbage():
    assert hs.normalize_state("깨진 값", DAY) == {"date": DAY, "tripped": {}}
    assert hs.normalize_state({"date": DAY, "tripped": "이상"}, DAY)["tripped"] == {}


def test_apply_verdicts_records_only_tripped():
    v = [hs.evaluate_slot("키움", -400_000, 10_000_000),
         hs.evaluate_slot("콴텍", -10_000, 10_000_000)]
    state, fresh = hs.apply_verdicts(hs.empty_state(DAY), v, at="09:06")
    assert list(state["tripped"]) == ["키움"] and len(fresh) == 1
    assert state["tripped"]["키움"]["at"] == "09:06"


def test_apply_verdicts_is_idempotent():
    """5분마다 같은 경보가 오면 사람은 곧 무시하게 된다."""
    v = [hs.evaluate_slot("키움", -400_000, 10_000_000)]
    state, _ = hs.apply_verdicts(hs.empty_state(DAY), v, at="09:06")
    state2, fresh2 = hs.apply_verdicts(state, v, at="09:11")
    assert fresh2 == [] and state2["tripped"]["키움"]["at"] == "09:06"


def test_apply_verdicts_does_not_mutate_input():
    base = hs.empty_state(DAY)
    hs.apply_verdicts(base, [hs.evaluate_slot("키움", -400_000, 10_000_000)], at="09:06")
    assert base["tripped"] == {}


# ─── 차단 조회 ───────────────────────────────────────


def test_is_blocked_true_after_trip():
    state, _ = hs.apply_verdicts(hs.empty_state(DAY),
                                 [hs.evaluate_slot("키움", -400_000, 10_000_000)], at="09:06")
    assert hs.is_blocked(state, "키움", DAY)
    assert not hs.is_blocked(state, "콴텍", DAY)


def test_is_blocked_false_on_next_day():
    state, _ = hs.apply_verdicts(hs.empty_state(DAY),
                                 [hs.evaluate_slot("키움", -400_000, 10_000_000)], at="09:06")
    assert not hs.is_blocked(state, "키움", "2026-08-26")


def test_block_message_explains_scope():
    state, _ = hs.apply_verdicts(hs.empty_state(DAY),
                                 [hs.evaluate_slot("키움", -400_000, 10_000_000)], at="09:06")
    msg = hs.block_message(state, "키움")
    assert "신규 매수" in msg and "손절" in msg and "내일" in msg


def test_format_alert_says_no_auto_liquidation():
    v = [hs.evaluate_slot("키움", -400_000, 10_000_000)]
    text = hs.format_alert(v)
    assert "자동 청산은 하지 않습니다" in text and "키움" in text


def test_format_alert_empty_when_nothing_fresh():
    assert hs.format_alert([]) == ""


# ─── 파일 경계 ───────────────────────────────────────


def test_load_state_missing_file(tmp_path):
    assert hs.load_state(tmp_path / "none.json", day=DAY) == {"date": DAY, "tripped": {}}


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "state" / "slot_hard_stop.json"
    state, _ = hs.apply_verdicts(hs.empty_state(DAY),
                                 [hs.evaluate_slot("키움", -400_000, 10_000_000)], at="09:06")
    hs.save_state(state, p)
    assert hs.load_state(p, day=DAY)["tripped"]["키움"]["loss_pct"] == -4.0
    assert json.loads(p.read_text(encoding="utf-8"))["date"] == DAY


def test_guard_buy_blocks_only_tripped_slot(tmp_path, monkeypatch):
    from datetime import date as real_date
    p = tmp_path / "s.json"
    today = real_date.today().isoformat()
    hs.save_state({"date": today, "tripped": {"키움": {"loss_pct": -4.0, "limit_pct": -3.0,
                                                     "at": "09:06"}}}, p)
    assert hs.guard_buy("키움", state_path=p) is not None
    assert hs.guard_buy("콴텍", state_path=p) is None


def test_guard_buy_allows_when_no_state(tmp_path):
    assert hs.guard_buy("키움", state_path=tmp_path / "none.json") is None


# ─── 배선 검증 (소스 수준) ───────────────────────────


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _read(name):
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_paper_ui_guards_buy_only():
    """매수는 막고 매도는 막지 않는다 — 보유 정리를 못 하게 만들면 더 위험하다."""
    src = _read("paper_ui.py")
    buy = src[src.index('@app.post("/api/paper/buy")'):src.index('@app.post("/api/paper/sell")')]
    sell = src[src.index('@app.post("/api/paper/sell")'):]
    assert "_slot_hard_stop_reason(slot)" in buy
    assert "_slot_hard_stop_reason" not in sell.split("@app.")[0]


def test_paper_ui_guard_fails_open():
    src = _read("paper_ui.py")
    fn = src[src.index("def _slot_hard_stop_reason"):src.index('@app.post("/api/paper/buy")')]
    assert "return None" in fn and "except Exception" in fn


def test_telegram_monitor_records_and_alerts():
    src = _read("telegram_bot.py")
    assert "slot_hard_stop.check_and_record" in src
    assert "slot_hard_stop.format_alert" in src


def test_telegram_auto_buy_paths_guarded():
    """키움·콴텍 두 자동 매수 경로 모두 관문을 지난다."""
    src = _read("telegram_bot.py")
    assert src.count("hard_stop = _slot_hard_stop_reason(slot_name)") == 2
    # 각 관문이 자기 목록을 비우는지 — 카운트가 아니라 관문 바로 아래를 본다
    for anchor in ("new_results = []", "new_recs = []"):
        i = src.index(f"        {anchor}")
        assert "hard_stop" in src[max(0, i - 200):i]
