"""test_paper_analytics.py — paper 성과 분석(순수) + 성과 라우팅 (hermetic)."""
from __future__ import annotations
import pytest

import paper_analytics as pa
import router
import system_info as si


def _stats():
    return [
        {"slot_id": 1, "slot_name": "콴텍", "n_closed": 1, "win_rate": 100.0,
         "total_pnl": 1984615, "total_return_pct": 4.96, "max_drawdown_pct": 0.0,
         "sharpe": None, "n_open_positions": 0, "open_cost": 0},
        {"slot_id": 2, "slot_name": "키움", "n_closed": 22, "win_rate": 36.4,
         "total_pnl": 40578, "total_return_pct": 0.10, "max_drawdown_pct": 19.88,
         "sharpe": 0.90, "n_open_positions": 3, "open_cost": 8847852},
        {"slot_id": 3, "slot_name": "IPO", "n_closed": 0, "win_rate": None,
         "total_pnl": 0, "total_return_pct": 0.0, "max_drawdown_pct": 0.0,
         "sharpe": None, "n_open_positions": 0, "open_cost": 0},
    ]


# ─── summarize ──────────────────────────────────────


def test_summarize_aggregates():
    s = pa.summarize(_stats())
    assert s["total_realized_pnl"] == 2025193
    assert s["total_closed"] == 23
    # wins: 콴텍 1 + 키움 round(0.364*22)=8 → 9/23
    assert s["overall_win_rate"] == round(9 / 23 * 100, 1)


def test_summarize_best_worst():
    s = pa.summarize(_stats())
    assert s["best"]["name"] == "콴텍"
    assert s["worst"]["name"] == "키움"


def test_summarize_stale_detection():
    s = pa.summarize(_stats())
    assert s["stale_slots"] == ["IPO"]
    assert "콴텍" in s["active_slots"] and "키움" in s["active_slots"]


def test_summarize_empty():
    s = pa.summarize([])
    assert s["total_closed"] == 0
    assert s["overall_win_rate"] is None
    assert s["best"] is None


# ─── format_report ──────────────────────────────────


def test_format_report_has_slots_and_total():
    out = pa.format_report(_stats())
    assert "콴텍" in out and "키움" in out
    assert "합계" in out
    assert "정체" in out  # IPO 정체 표기


def test_format_report_empty():
    assert "데이터가 없습니다" in pa.format_report([])


def test_run_returns_tuple(monkeypatch):
    monkeypatch.setattr(pa, "report", lambda *a, **k: "X")
    ans, chunks = pa.run()
    assert ans == "X" and chunks == []


def test_report_handles_db_error(monkeypatch):
    import paper_db
    monkeypatch.setattr(paper_db, "performance_stats",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = pa.report("/nonexistent.db")
    assert "실패" in out


# ─── 성과 라우팅 ────────────────────────────────────


@pytest.mark.parametrize("q", [
    "내 성과 어때", "paper 수익률 보여줘", "봇 승률 어때",
    "전체 손익 알려줘", "트레이딩 성과 정리해줘",
])
def test_detect_performance_positive(q):
    assert router._detect_system_info(q) == {"topic": "performance"}


@pytest.mark.parametrize("q", [
    "삼성전자 수익률 어때",   # 개별 종목 → invest
    "성과", "안녕",
])
def test_detect_performance_negative(q):
    assert router._detect_system_info(q) != {"topic": "performance"}


def test_performance_topic_valid():
    assert "performance" in router._SI_TOPICS
    assert router._validate_system_info({"topic": "performance"}) == {"topic": "performance"}


def test_schedule_performance_report():
    # '매주 성과 리포트 보내' → action_schedule(performance) (route 순서상 예약이 우선)
    out = router._detect_action_schedule("매주 월 9시 성과 리포트 보내")
    assert out["op"] == "add" and out["action"] == "performance"


def test_system_info_performance_dispatch(monkeypatch):
    import paper_analytics
    monkeypatch.setattr(paper_analytics, "report", lambda *a, **k: "REPORT_OK")
    assert si.answer("performance") == "REPORT_OK"


# ─── 성과 스냅샷 history (EWMA 입력) ─────────────────
from datetime import datetime as _dtp


def test_record_and_load_snapshot(tmp_path):
    p = tmp_path / "hist.json"
    st = [{"slot_name": "키움", "n_closed": 22, "total_return_pct": 0.1,
           "sharpe": 0.9, "total_pnl": 40578, "win_rate": 36.4}]
    pa.record_snapshot(st, _dtp(2026, 6, 11, 9, 0), p)
    pa.record_snapshot(st, _dtp(2026, 6, 18, 9, 0), p)
    hist = pa.load_history(p)
    assert len(hist) == 2
    assert hist[-1]["ts"] == "2026-06-18 09:00"
    assert hist[-1]["slots"]["키움"]["total_return_pct"] == 0.1


def test_load_history_missing(tmp_path):
    assert pa.load_history(tmp_path / "none.json") == []
