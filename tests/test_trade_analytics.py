"""test_trade_analytics.py — 트레이드 단위 심화 분석(순수) + 라우팅 (hermetic)."""
from __future__ import annotations
import pytest

import trade_analytics as ta
import router
import system_info as si


def _trades():
    # 키움: 삼성전자 +200(익절), 대우건설 -100(손절)
    return [
        {"slot_name": "키움", "ticker": "005930", "name": "삼성전자", "side": "buy",
         "quantity": 10, "price": 100, "fees": 0, "notes": "", "executed_at": "2026-05-01 10:00", "id": 1},
        {"slot_name": "키움", "ticker": "005930", "name": "삼성전자", "side": "sell",
         "quantity": 10, "price": 120, "fees": 0, "notes": "익절 자동청산", "executed_at": "2026-05-08 10:00", "id": 2},
        {"slot_name": "키움", "ticker": "047040", "name": "대우건설", "side": "buy",
         "quantity": 10, "price": 100, "fees": 0, "notes": "", "executed_at": "2026-06-01 10:00", "id": 3},
        {"slot_name": "키움", "ticker": "047040", "name": "대우건설", "side": "sell",
         "quantity": 10, "price": 90, "fees": 0, "notes": "손절 자동청산", "executed_at": "2026-06-05 10:00", "id": 4},
    ]


# ─── FIFO 라운드트립 ─────────────────────────────────


def test_roundtrips_count_and_pnl():
    rts = ta.compute_roundtrips(_trades())
    assert len(rts) == 2
    by = {r["name"]: r for r in rts}
    assert round(by["삼성전자"]["pnl"]) == 200
    assert round(by["대우건설"]["pnl"]) == -100


def test_roundtrip_name_from_buy_leg_when_sell_missing():
    trades = _trades()
    trades[1]["name"] = None  # 매도 name 결측 → 매수 leg에서 보충
    rts = ta.compute_roundtrips(trades)
    assert any(r["name"] == "삼성전자" for r in rts)


def test_roundtrip_reason_and_hold_days():
    rts = {r["name"]: r for r in ta.compute_roundtrips(_trades())}
    assert rts["삼성전자"]["reason"] == "익절"
    assert rts["대우건설"]["reason"] == "손절"
    assert rts["삼성전자"]["hold_days"] == 7


# ─── 매매 습관 ───────────────────────────────────────


def test_habit_stats():
    h = ta.habit_stats(ta.compute_roundtrips(_trades()))
    assert h["n_closed"] == 2 and h["wins"] == 1 and h["losses"] == 1
    assert h["win_rate"] == 50.0
    assert h["avg_win"] == 200 and h["avg_loss"] == -100
    assert h["profit_factor"] == 2.0
    assert h["payoff_ratio"] == 2.0
    assert h["reason_counts"] == {"익절": 1, "손절": 1}


def test_habit_empty():
    h = ta.habit_stats([])
    assert h["n_closed"] == 0 and h["win_rate"] is None


# ─── 종목별 / 월별 ───────────────────────────────────


def test_by_ticker_sorted():
    rows = ta.by_ticker(ta.compute_roundtrips(_trades()))
    assert rows[0]["name"] == "삼성전자"  # +200 최상단
    assert rows[-1]["name"] == "대우건설"  # -100 최하단


def test_monthly_pnl():
    m = ta.monthly_pnl(ta.compute_roundtrips(_trades()))
    assert m == {"2026-05": 200, "2026-06": -100}


# ─── 포매팅 / LLM ────────────────────────────────────


def test_format_deep_sections():
    out = ta.format_deep(ta.compute_roundtrips(_trades()))
    assert "매매 습관" in out and "종목별" in out and "월별" in out


def test_format_deep_empty():
    assert "거래가 아직 없습니다" in ta.format_deep([])


def test_llm_summary_uses_injected_fn():
    out = ta.llm_summary(ta.compute_roundtrips(_trades()), llm_fn=lambda p: "총평입니다")
    assert out == "총평입니다"


def test_llm_summary_graceful_on_failure():
    def boom(p):
        raise RuntimeError("llm down")
    assert ta.llm_summary(ta.compute_roundtrips(_trades()), llm_fn=boom) == ""


def test_llm_summary_empty_trades():
    assert ta.llm_summary([]) == ""


def test_deep_report_handles_error(monkeypatch):
    import paper_db
    monkeypatch.setattr(paper_db, "list_trades",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert "실패" in ta.deep_report("/nonexistent.db")


# ─── 라우팅 ──────────────────────────────────────────


@pytest.mark.parametrize("q", [
    "매매 습관 분석해줘", "내 거래 분석해줘", "성과 분석해줘",
    "손익비 어때", "심화 분석 보여줘",
])
def test_router_detects_analysis(q):
    assert router._detect_system_info(q) == {"topic": "analysis"}


@pytest.mark.parametrize("q", [
    "삼성전자 분석해줘",   # 개별 종목 → invest
    "내 성과 어때",        # 빠른 성과 → performance
])
def test_analysis_does_not_swallow_others(q):
    assert router._detect_system_info(q) != {"topic": "analysis"}


def test_analysis_topic_registered():
    assert "analysis" in router._SI_TOPICS
    assert router._validate_system_info({"topic": "analysis"}) == {"topic": "analysis"}


def test_system_info_analysis_dispatch(monkeypatch):
    import trade_analytics
    monkeypatch.setattr(trade_analytics, "deep_report", lambda *a, **k: "DEEP_OK")
    assert si.answer("analysis") == "DEEP_OK"


# ─── 정밀 진단: 손절/휩쏘/집중 ───────────────────────
def test_stop_loss_diagnosis():
    sl = ta.stop_loss_diagnosis(ta.compute_roundtrips(_trades()))
    assert sl["n"] == 1              # 대우건설만 손절
    assert sl["avg_ret"] == -10.0    # -100/1000
    assert sl["threshold"] == -7.0
    assert sl["slippage"] == -3.0    # 손절선보다 3%p 깊게
    assert sl["deeper_than_stop"] == 1
    assert sl["much_deeper"] == 0    # -10% > -12%


def test_stop_loss_diagnosis_no_stops():
    trades = [t for t in _trades() if "손절" not in (t["notes"] or "")]
    assert ta.stop_loss_diagnosis(ta.compute_roundtrips(trades))["n"] == 0


def test_whipsaw_detects_rebuy():
    trades = [
        {"slot_name": "키움", "ticker": "A", "name": "종목A", "side": "buy",
         "quantity": 10, "price": 100, "fees": 0, "notes": "", "executed_at": "2026-06-01 10:00", "id": 1},
        {"slot_name": "키움", "ticker": "A", "name": "종목A", "side": "sell",
         "quantity": 10, "price": 90, "fees": 0, "notes": "손절 자동청산", "executed_at": "2026-06-05 10:00", "id": 2},
        {"slot_name": "키움", "ticker": "A", "name": "종목A", "side": "buy",
         "quantity": 10, "price": 92, "fees": 0, "notes": "", "executed_at": "2026-06-08 10:00", "id": 3},
    ]
    rts = ta.compute_roundtrips(trades)
    wh = ta.whipsaw(trades, rts, days=14)
    assert len(wh) == 1 and wh[0]["name"] == "종목A" and wh[0]["days"] == 3


def test_whipsaw_ignores_late_rebuy():
    trades = [
        {"slot_name": "키움", "ticker": "A", "name": "종목A", "side": "buy",
         "quantity": 10, "price": 100, "fees": 0, "notes": "", "executed_at": "2026-06-01 10:00", "id": 1},
        {"slot_name": "키움", "ticker": "A", "name": "종목A", "side": "sell",
         "quantity": 10, "price": 90, "fees": 0, "notes": "손절", "executed_at": "2026-06-05 10:00", "id": 2},
        {"slot_name": "키움", "ticker": "A", "name": "종목A", "side": "buy",
         "quantity": 10, "price": 92, "fees": 0, "notes": "", "executed_at": "2026-07-05 10:00", "id": 3},
    ]
    assert ta.whipsaw(trades, ta.compute_roundtrips(trades), days=14) == []


def test_loss_concentration():
    lc = ta.loss_concentration(ta.compute_roundtrips(_trades()))
    assert lc["n_losers"] == 1
    assert lc["top"][0]["name"] == "대우건설"
    assert lc["top_share_pct"] == 100


def test_loss_concentration_no_losses():
    winners = _trades()[:2]  # 삼성전자 익절만
    assert ta.loss_concentration(ta.compute_roundtrips(winners))["n_losers"] == 0


def test_format_precision_has_sections():
    trades = _trades()
    out = ta.format_precision(ta.compute_roundtrips(trades), trades)
    assert "정밀 진단" in out and "손절" in out


def test_format_deep_includes_precision():
    trades = _trades()
    out = ta.format_deep(ta.compute_roundtrips(trades), trades=trades)
    assert "정밀 진단" in out


# ─── 국면·섹터 상관 (v1.2) ───────────────────────────


def test_correlate_pnl_by_injected_label():
    corr = ta.correlate_pnl_by(ta.compute_roundtrips(_trades()), lambda r: "L")
    assert corr["L"]["pnl"] == 100 and corr["L"]["n"] == 2
    assert corr["L"]["win_rate"] == 50.0
    assert corr["L"]["loss"] == -100 and corr["L"]["loss_share_pct"] == 100


def test_correlate_by_sector_manual_map():
    corr = ta.correlate_pnl_by(ta.compute_roundtrips(_trades()), ta.sector_label)
    assert corr["반도체"]["pnl"] == 200 and corr["반도체"]["win_rate"] == 100.0
    assert corr["건설"]["pnl"] == -100 and corr["건설"]["loss_share_pct"] == 100


def test_correlate_by_phase_month_map():
    corr = ta.correlate_pnl_by(ta.compute_roundtrips(_trades()), ta.phase_label)
    # 실현(매도)월 기준: 2026-05 Expansion(+200), 2026-06 Slowdown(-100)
    assert corr["Expansion"]["pnl"] == 200
    assert corr["Slowdown"]["pnl"] == -100


def test_labels_fallback_unlabeled():
    rt = {"ticker": "999999", "sell_at": "2030-01-05 10:00"}
    assert ta.sector_label(rt) == ta.UNLABELED
    assert ta.phase_label(rt) == ta.UNLABELED


def test_label_mapping_override():
    rt = {"ticker": "X", "sell_at": "2026-05-01"}
    assert ta.sector_label(rt, {"X": "테스트섹터"}) == "테스트섹터"
    assert ta.phase_label(rt, {"2026-05": "Recovery"}) == "Recovery"


def test_top_loss_label():
    corr = ta.correlate_pnl_by(ta.compute_roundtrips(_trades()), ta.sector_label)
    top = ta.top_loss_label(corr)
    assert top["label"] == "건설" and top["loss_share_pct"] == 100


def test_top_loss_label_none_when_no_losses():
    corr = ta.correlate_pnl_by(ta.compute_roundtrips(_trades()[:2]),
                               ta.sector_label)
    assert ta.top_loss_label(corr) is None


def test_format_correlation_sections():
    out = ta.format_correlation(ta.compute_roundtrips(_trades()))
    assert "국면·섹터 상관" in out and "국면별" in out and "섹터별" in out
    assert "손실 집중" in out


def test_format_correlation_empty():
    assert ta.format_correlation([]) == ""


def test_format_deep_includes_correlation():
    out = ta.format_deep(ta.compute_roundtrips(_trades()))
    assert "국면·섹터 상관" in out


def test_llm_prompt_includes_correlation():
    captured = {}

    def fake(p):
        captured["p"] = p
        return "ok"

    ta.llm_summary(ta.compute_roundtrips(_trades()), llm_fn=fake)
    assert "국면별 손익" in captured["p"] and "섹터별 손익" in captured["p"]


# ─── 보완: 슬리피지 전/후 · 소표본 · 국면 캐시 · 미분류 (v1.3) ────


def test_slippage_comparison_split():
    cmp = ta.slippage_comparison(ta.compute_roundtrips(_trades()),
                                 cutoff="2026-06-01")
    assert cmp["before"]["n"] == 0          # 5월엔 손절 없음(익절만)
    assert cmp["after"]["n"] == 1           # 6월 대우건설 손절
    assert cmp["after"]["avg_ret"] == -10.0


def test_format_precision_shows_comparison_only_with_both():
    trades = _trades()
    out = ta.format_precision(ta.compute_roundtrips(trades), trades)
    assert "전/후" not in out               # 기본 cutoff(2026-07-09) 이후 손절 없음


def test_small_sample_marker():
    out = ta.format_correlation(ta.compute_roundtrips(_trades()))
    assert "†" in out and "표본 5건 미만" in out


def test_phase_cache_record_and_merge(tmp_path):
    p = tmp_path / "phase.json"
    ta.record_phase_month("Recovery", month="2026-08", path=p)
    ta.record_phase_month("Contraction", month="2026-05", path=p)  # 수동값 덮어씀
    assert ta.load_phase_months(p) == {"2026-08": "Recovery",
                                       "2026-05": "Contraction"}
    merged = ta.merged_phase_map(p)
    assert merged["2026-05"] == "Contraction"   # 실측 우선
    assert merged["2026-06"] == "Slowdown"      # 수동 근사 유지
    assert merged["2026-08"] == "Recovery"


def test_phase_cache_graceful(tmp_path):
    assert ta.load_phase_months(tmp_path / "none.json") == {}
    ta.record_phase_month("", path=tmp_path / "none.json")  # 빈 phase 무시
    assert not (tmp_path / "none.json").exists()


def test_format_deep_uses_injected_phase_map():
    rts = ta.compute_roundtrips(_trades())
    out = ta.format_deep(rts, phase_map={"2026-05": "Recovery",
                                         "2026-06": "Recovery"})
    assert "Recovery" in out and "Expansion" not in out


def test_unmapped_sector_warning():
    trades = [
        {"slot_name": "키움", "ticker": "999999", "name": "신규종목", "side": "buy",
         "quantity": 1, "price": 100, "fees": 0, "notes": "", "executed_at": "2026-06-01 10:00", "id": 1},
        {"slot_name": "키움", "ticker": "999999", "name": "신규종목", "side": "sell",
         "quantity": 1, "price": 110, "fees": 0, "notes": "익절", "executed_at": "2026-06-02 10:00", "id": 2},
    ]
    out = ta.format_correlation(ta.compute_roundtrips(trades))
    assert "미분류 1종목" in out and "신규종목" in out


def test_no_unmapped_warning_when_all_mapped():
    out = ta.format_correlation(ta.compute_roundtrips(_trades()))
    assert "미분류" not in out
