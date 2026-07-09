"""test_strategy_feedback.py — 성과→비중 피드백 골격 (순수, hermetic)."""
from __future__ import annotations
import pytest

import strategy_feedback as sf


def _stat(name, n, ret, sharpe=None):
    return {"slot_name": name, "n_closed": n, "total_return_pct": ret, "sharpe": sharpe}


# ─── 소표본 가드 ─────────────────────────────────────


def test_all_below_min_sample_are_neutral():
    stats = [_stat("키움", 3, 5.0), _stat("콴텍", 1, 10.0)]
    alloc = {"키움": 0.4, "콴텍": 0.4}
    out = sf.compute_tilts(stats, alloc)
    assert all(x["delta_pct"] == 0.0 for x in out)


def test_single_qualifying_slot_is_neutral():
    # 자격 슬롯 1개뿐 → 재배분 불가 → 중립
    stats = [_stat("키움", 20, 5.0, 0.9), _stat("콴텍", 2, 10.0)]
    out = sf.compute_tilts(stats, {"키움": 0.4, "콴텍": 0.4})
    assert all(x["delta_pct"] == 0.0 for x in out)


# ─── 정상 조정 ───────────────────────────────────────


def _two_qual():
    stats = [_stat("키움", 20, -2.0, 0.1), _stat("콴텍", 20, 8.0, 1.2)]
    alloc = {"키움": 0.4, "콴텍": 0.4}
    return stats, alloc


def test_better_slot_gets_higher_allocation():
    stats, alloc = _two_qual()
    out = {x["slot_name"]: x for x in sf.compute_tilts(stats, alloc)}
    assert out["콴텍"]["delta_pct"] > 0
    assert out["키움"]["delta_pct"] < 0


def test_total_allocation_preserved():
    stats, alloc = _two_qual()
    out = sf.compute_tilts(stats, alloc)
    tot_cur = sum(x["current_pct"] for x in out)
    tot_sug = sum(x["suggested_pct"] for x in out)
    assert abs(tot_cur - tot_sug) < 0.2  # 반올림 오차 내 보존


def test_delta_capped_by_max_tilt():
    # 점수 차이를 크게 줘도 ±max_tilt(5%p) 이내
    stats = [_stat("A", 50, -50.0, -2.0), _stat("B", 50, 80.0, 3.0)]
    out = sf.compute_tilts(stats, {"A": 0.4, "B": 0.4}, max_tilt=0.05)
    assert all(abs(x["delta_pct"]) <= 5.0 + 1e-6 for x in out)


def test_custom_max_tilt_respected():
    stats = [_stat("A", 50, -50.0, -2.0), _stat("B", 50, 80.0, 3.0)]
    out = sf.compute_tilts(stats, {"A": 0.4, "B": 0.4}, max_tilt=0.02)
    assert all(abs(x["delta_pct"]) <= 2.0 + 1e-6 for x in out)


def test_slot_without_allocation_skipped():
    stats = [_stat("키움", 20, 5.0, 1.0), _stat("콴텍", 20, 1.0, 0.5),
             _stat("미지정", 20, 9.0, 2.0)]
    out = sf.compute_tilts(stats, {"키움": 0.4, "콴텍": 0.4})
    assert {x["slot_name"] for x in out} == {"키움", "콴텍"}


def test_suggested_never_negative():
    stats = [_stat("A", 50, -99.0, -3.0), _stat("B", 50, 99.0, 3.0)]
    out = sf.compute_tilts(stats, {"A": 0.02, "B": 0.4}, max_tilt=0.05)
    assert all(x["suggested_pct"] >= 1.0 for x in out)


# ─── perf_score / 포매팅 / run ───────────────────────


def test_perf_score_combines_return_and_sharpe():
    assert sf.perf_score({"total_return_pct": 5.0, "sharpe": 1.0}) == 6.0
    assert sf.perf_score({"total_return_pct": 5.0, "sharpe": None}) == 5.0


def test_format_neutral_mentions_no_change():
    stats = [_stat("키움", 3, 5.0)]
    out = sf.format_suggestions(sf.compute_tilts(stats, {"키움": 0.4}))
    assert "변경 제안 없음" in out


def test_format_has_arrows_on_change():
    stats, alloc = _two_qual()
    out = sf.format_suggestions(sf.compute_tilts(stats, alloc))
    assert "▲" in out and "▼" in out


def test_run_returns_tuple(monkeypatch):
    monkeypatch.setattr(sf, "proposal", lambda *a, **k: "P")
    ans, chunks = sf.run()
    assert ans == "P" and chunks == []


def test_proposal_handles_error(monkeypatch):
    import paper_db
    monkeypatch.setattr(paper_db, "performance_stats",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert "실패" in sf.proposal("/nonexistent.db")


# ─── feedback 라우팅 + system_info 연결 ──────────────
import router as _router
import system_info as _si


@pytest.mark.parametrize("q", [
    "비중 제안 보여줘", "슬롯 비중 조정해줘", "리밸런싱 제안 줘봐",
    "성과 기반 비중 추천해줘",
])
def test_router_detects_feedback(q):
    assert _router._detect_system_info(q) == {"topic": "feedback"}


def test_feedback_does_not_collide_with_rebalance_status():
    # '리밸런싱 됐어?'(상태질문)은 rebalance, '리밸런싱 제안'은 feedback
    assert _router._detect_system_info("리밸런싱 됐어?") == {"topic": "rebalance"}


def test_feedback_topic_registered():
    assert "feedback" in _router._SI_TOPICS
    assert _router._validate_system_info({"topic": "feedback"}) == {"topic": "feedback"}


def test_system_info_feedback_dispatch(monkeypatch):
    import strategy_feedback
    monkeypatch.setattr(strategy_feedback, "proposal", lambda *a, **k: "PROP_OK")
    assert _si.answer("feedback") == "PROP_OK"


# ─── EWMA 평활 ───────────────────────────────────────
def test_ewma_weights_recent_more():
    # 최신 값(10)이 과거 값(0)보다 가중 ↑ → 평균 > 단순중앙(5)
    assert sf.ewma_score([0.0, 10.0]) > 5.0


def test_ewma_empty_is_zero():
    assert sf.ewma_score([]) == 0.0


def test_scores_from_history_per_slot():
    hist = [
        {"slots": {"A": {"total_return_pct": -2, "sharpe": 0.1},
                   "B": {"total_return_pct": 8, "sharpe": 1.2}}},
        {"slots": {"A": {"total_return_pct": 1, "sharpe": 0.5},
                   "B": {"total_return_pct": 6, "sharpe": 1.0}}},
    ]
    sc = sf.scores_from_history(hist)
    assert set(sc) == {"A", "B"}
    assert sc["B"] > sc["A"]


def test_compute_tilts_uses_injected_scores():
    # 최신 stats는 동률이지만 평활점수가 B 우위 → B 비중↑
    stats = [_stat("A", 20, 0.0, 0.0), _stat("B", 20, 0.0, 0.0)]
    out = {x["slot_name"]: x for x in
           sf.compute_tilts(stats, {"A": 0.4, "B": 0.4}, scores={"A": -3.0, "B": 5.0})}
    assert out["B"]["delta_pct"] > 0 and out["A"]["delta_pct"] < 0


# ─── 승인형 적용 골격 (v0.1) ─────────────────────────
def _sugs():
    return [
        {"slot_name": "키움", "current_pct": 40.0, "suggested_pct": 43.0, "delta_pct": 3.0, "reason": "x"},
        {"slot_name": "콴텍", "current_pct": 40.0, "suggested_pct": 37.0, "delta_pct": -3.0, "reason": "y"},
        {"slot_name": "IPO", "current_pct": 20.0, "suggested_pct": 20.2, "delta_pct": 0.2, "reason": "z"},
    ]


def test_build_apply_plan_filters_small_delta():
    plan = sf.build_apply_plan(_sugs())  # 기본 min 0.5%p
    assert {p["slot_name"] for p in plan} == {"키움", "콴텍"}  # IPO 0.2%p 컷


def test_build_apply_plan_all_neutral_is_empty():
    neutral = [{"slot_name": "A", "current_pct": 40.0, "suggested_pct": 40.0, "delta_pct": 0.0, "reason": "-"}]
    assert sf.build_apply_plan(neutral) == []


def test_apply_plan_dry_run_does_not_call_setter():
    calls = []
    res = sf.apply_plan(sf.build_apply_plan(_sugs()), lambda n, f: calls.append((n, f)), dry_run=True)
    assert calls == []
    assert res["applied"] == 0 and res["dry_run"] is True


def test_apply_plan_executes_only_when_not_dry_run():
    calls = []
    res = sf.apply_plan(sf.build_apply_plan(_sugs()), lambda n, f: calls.append((n, f)), dry_run=False)
    assert dict(calls) == {"키움": 0.43, "콴텍": 0.37}  # %→fraction
    assert res["applied"] == 2


def test_apply_plan_empty_is_noop():
    calls = []
    res = sf.apply_plan([], lambda n, f: calls.append(1), dry_run=False)
    assert calls == [] and res["applied"] == 0


def test_format_apply_plan_variants():
    assert "승인 대기" in sf.format_apply_plan(sf.build_apply_plan(_sugs()))
    assert "변경이 없습니다" in sf.format_apply_plan([])


def test_current_suggestions_handles_error(monkeypatch):
    import paper_db
    monkeypatch.setattr(paper_db, "performance_stats",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert sf.current_suggestions("/nonexistent.db") == []
