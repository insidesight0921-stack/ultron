"""test_entry_backtest.py — 진입 조건 이벤트 스터디 순수 코어 (hermetic, 합성 시계열)."""
from __future__ import annotations

import entry_backtest as eb


def _flat(n, px=100.0):
    return [px] * n


def _trend(n, daily=0.005, px=100.0):
    out = [px]
    for _ in range(n - 1):
        out.append(out[-1] * (1 + daily))
    return out


# ─── 피처 ────────────────────────────────────────────


def test_features_need_history():
    assert eb.compute_features(_flat(50), 49) is None          # 이력 부족
    assert eb.compute_features(_flat(100), 99) is not None


def test_features_flat_series():
    f = eb.compute_features(_flat(100), 99)
    assert abs(f["ma20_gap"]) < 1e-9 and abs(f["ma60_gap"]) < 1e-9
    assert abs(f["dd20"]) < 1e-9 and abs(f["ret5"]) < 1e-9
    assert f["new_high20"] is True   # 동일가는 신고가 취급(>=)
    assert f["vol_z"] is None        # 거래량 미제공


def test_features_uptrend_positive_gaps():
    closes = _trend(100)
    f = eb.compute_features(closes, 99)
    assert f["ma20_gap"] > 0 and f["ma60_gap"] > 0
    assert f["new_high20"] and f["ret5"] > 0


def test_features_drawdown():
    closes = _trend(90) + [_trend(90)[-1] * m for m in (0.95, 0.90, 0.88)]
    f = eb.compute_features(closes, len(closes) - 1)
    assert f["dd20"] < -0.10 and f["ret5"] < 0


def test_features_vol_z():
    closes = _flat(100)
    vols = [1000.0] * 99 + [3000.0]   # 마지막 날 거래량 급증
    f = eb.compute_features(closes, 99, vols)
    assert f["vol_z"] > 2.0


# ─── 조건 세트 ───────────────────────────────────────


def test_conditions_are_mutually_sane():
    up = eb.compute_features(_trend(100), 99)
    assert eb.CONDITIONS["정배열"](up) and eb.CONDITIONS["신고가돌파"](up)
    assert not eb.CONDITIONS["역추세"](up)
    assert not eb.CONDITIONS["하락나이프"](up)
    assert eb.CONDITIONS["베이스라인(무조건)"](up)


# ─── 이벤트 스터디 ───────────────────────────────────


def test_event_study_uptrend_baseline_wins():
    series = [{"closes": _trend(200, 0.012)}]   # 강한 상승 추세 → 익절 반복
    r = eb.event_study(series, eb.CONDITIONS["베이스라인(무조건)"], cost=0.0)
    assert r["n"] >= 2 and r["avg_ret"] > 0 and r["win_rate"] == 100.0


def test_event_study_no_overlap():
    # 청산 전 재진입 금지 — 200봉 상승추세에서 이벤트 수는 200보다 훨씬 작아야
    series = [{"closes": _trend(200, 0.012)}]
    r = eb.event_study(series, lambda f: True, cost=0.0)
    assert 0 < r["n"] < 30


def test_event_study_cost_reduces_ret():
    series = [{"closes": _trend(200, 0.012)}]
    free = eb.event_study(series, lambda f: True, cost=0.0)
    paid = eb.event_study(series, lambda f: True, cost=0.01)
    assert paid["avg_ret"] < free["avg_ret"]


def test_event_study_empty_when_condition_never_met():
    r = eb.event_study([{"closes": _flat(200)}], lambda f: f["ret5"] > 0.5)
    assert r["n"] == 0 and r["avg_ret"] is None


def test_split_halves_preserves_history_overlap():
    series = [{"closes": _trend(300), "volumes": None}]
    fh, sh = eb.split_halves(series)
    assert len(fh[0]["closes"]) == 150
    assert len(sh[0]["closes"]) == 150 + eb.MIN_HISTORY  # 이력 겹침 포함


def test_run_and_format_conditions():
    series = [{"closes": _trend(300, 0.008)}]
    rows = eb.run_conditions(series, cost=0.0)
    assert {r["name"] for r in rows} == set(eb.CONDITIONS)
    out = eb.format_conditions(rows)
    assert "이벤트 스터디" in out and "베이스라인" in out and "전반" in out


# ─── 현재 시점 스캔 (scan_signals) ───────────────────


def test_scan_signals_matches_uptrend():
    series = [{"ticker": "A", "name": "종목A", "closes": _trend(100)},
              {"ticker": "B", "name": "종목B", "closes": _flat(50)}]  # 이력 부족
    out = eb.scan_signals(series)
    assert len(out) == 1 and out[0]["ticker"] == "A"
    assert "정배열" in out[0]["matched"] and "신고가돌파" in out[0]["matched"]
    assert out[0]["price"] == _trend(100)[-1]


def test_scan_signals_excludes_controls():
    # 하락 추세 → 대조군(역추세)만 해당 = 스캔 결과에서 제외돼야
    down = [100 * (0.995 ** i) for i in range(100)]
    assert eb.scan_signals([{"ticker": "D", "closes": down}]) == []


def test_scan_conditions_subset_of_conditions():
    assert set(eb.SCAN_CONDITIONS) <= set(eb.CONDITIONS)
