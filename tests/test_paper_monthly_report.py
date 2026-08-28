"""test_paper_monthly_report.py — 월간 성과 리포트(순수) 검증 (hermetic)."""
from __future__ import annotations

import json
from datetime import date

import paper_monthly_report as mr


def _rt(pnl=100_000, ret=0.05, slot="키움", reason="익절", sell="2026-08-10",
        buy="2026-08-03", notes=None, cost=2_000_000, hold=7, name="삼성전자"):
    return {"slot": slot, "ticker": "005930", "name": name, "pnl": pnl, "cost": cost,
            "qty": 10, "ret": ret, "buy_at": f"{buy} 10:00:00",
            "sell_at": f"{sell} 10:00:00", "buy_notes": notes,
            "hold_days": hold, "reason": reason}


# ─── 기간 ────────────────────────────────────────────


def test_month_bounds_handles_every_length():
    assert mr.month_bounds(date(2026, 8, 15)) == (date(2026, 8, 1), date(2026, 8, 31))
    assert mr.month_bounds(date(2026, 2, 5)) == (date(2026, 2, 1), date(2026, 2, 28))
    assert mr.month_bounds(date(2026, 12, 31)) == (date(2026, 12, 1), date(2026, 12, 31))


def test_prev_month_crosses_the_year():
    assert mr.prev_month(date(2026, 1, 3)) == date(2025, 12, 31)
    assert mr.prev_month(date(2026, 3, 1)) == date(2026, 2, 28)


def test_month_label():
    assert mr.month_label(date(2026, 8, 5)) == "2026-08"


# ─── 자산곡선 자르기 ─────────────────────────────────


def _curve():
    days = [{"date": d, "total": v} for d, v in
            (("20260731", 100.0), ("20260803", 101.0), ("20260831", 105.0),
             ("20260901", 106.0))]
    return {"days": days, "n_days": 4, "coverage": 0.8,
            "missing_days": ["20260805", "20260902"], "missing_tickers": {"x": 2}}


def test_slice_keeps_only_the_month():
    c = mr.slice_curve(_curve(), date(2026, 8, 1), date(2026, 8, 31))
    assert [d["date"] for d in c["days"]] == ["20260803", "20260831"]
    assert c["missing_days"] == ["20260805"]


def test_slice_recomputes_coverage_for_the_month():
    """전체 커버리지를 그대로 쓰면 그 달에 결측이 몰려도 드러나지 않는다."""
    c = mr.slice_curve(_curve(), date(2026, 8, 1), date(2026, 8, 31))
    assert c["coverage"] == round(2 / 3, 3)          # 2일 평가 / 1일 결측


def test_slice_of_an_empty_month_is_safe():
    c = mr.slice_curve(_curve(), date(2026, 5, 1), date(2026, 5, 31))
    assert c["n_days"] == 0 and c["coverage"] is None


# ─── 집계 ────────────────────────────────────────────


def test_report_counts_only_this_month_by_sell_date():
    """귀속은 청산일 기준 — 지난달에 사서 이번 달에 판 것은 이번 달이다."""
    rts = [_rt(buy="2026-07-28", sell="2026-08-03"), _rt(sell="2026-07-30")]
    r = mr.build_report(rts, as_of=date(2026, 8, 15))
    assert r["total"]["n"] == 1


def test_report_keeps_sufficient_statistics():
    r = mr.build_report([_rt(pnl=100), _rt(pnl=-40)], as_of=date(2026, 8, 15))
    assert {"n", "wins", "losses", "sum_win", "sum_loss", "cost"} <= set(r["total"])
    assert r["total"]["sum_win"] == 100 and r["total"]["sum_loss"] == 40


def test_slot_order_is_respected_and_idle_slots_listed():
    r = mr.build_report([_rt(slot="키움")], as_of=date(2026, 8, 15),
                        slot_order=["콴텍", "키움", "IPO"])
    assert [s["label"] for s in r["slots"]] == ["키움"]
    assert r["idle_slots"] == ["콴텍", "IPO"]


def test_reasons_and_tags_are_grouped():
    rts = [_rt(reason="손절", notes="MQ[키움모멘텀]"), _rt(reason="익절")]
    r = mr.build_report(rts, as_of=date(2026, 8, 15))
    assert {x["label"] for x in r["reasons"]} == {"손절", "익절"}
    assert [x["tag"] for x in r["tags"]] == ["키움모멘텀"]


def test_trend_covers_requested_months_oldest_first():
    r = mr.build_report([_rt()], as_of=date(2026, 8, 15), trend_months=3)
    assert [m["month"] for m in r["trend"]] == ["2026-06", "2026-07", "2026-08"]


def test_cumulative_covers_every_roundtrip():
    rts = [_rt(sell="2026-08-10"), _rt(sell="2026-06-10")]
    r = mr.build_report(rts, as_of=date(2026, 8, 15))
    assert r["total"]["n"] == 1 and r["cumulative"]["n"] == 2


def test_metrics_absent_without_a_curve():
    r = mr.build_report([_rt()], as_of=date(2026, 8, 15))
    assert r["month_metrics"] is None and r["cumulative_metrics"] is None


def test_metrics_split_month_and_cumulative():
    days = [{"date": f"202607{d:02d}", "total": 100.0 + d} for d in range(1, 29)]
    days += [{"date": f"202608{d:02d}", "total": 130.0 + d} for d in range(1, 29)]
    curve = {"days": days, "n_days": len(days), "coverage": 1.0,
             "missing_days": [], "missing_tickers": {}}
    bench = {d["date"]: 2000.0 for d in days}
    r = mr.build_report([_rt()], as_of=date(2026, 8, 15), curve=curve, bench=bench)
    assert r["month_metrics"]["n_days"] == 28          # 8월분만
    assert r["cumulative_metrics"]["n_days"] == 56     # 전체
    assert "verdict" in r["cumulative_metrics"]        # 전환 기준은 누적에만


def test_excluded_summary_is_carried():
    dropped = [dict(_rt(pnl=-500_000), excluded_kind="stale_price",
                    excluded_reason="진입가가 과거 종가와 일치")]
    r = mr.build_report([_rt()], as_of=date(2026, 8, 15), excluded=dropped)
    assert r["excluded"]["n"] == 1 and r["excluded"]["pnl"] == -500_000


# ─── 렌더 ────────────────────────────────────────────


def test_note_says_it_is_paper_not_real():
    text = mr.render_note(mr.build_report([_rt()], as_of=date(2026, 8, 15)))
    assert "모의투자(페이퍼)" in text and "실계좌가 아닙니다" in text


def test_note_has_month_in_the_title():
    text = mr.render_note(mr.build_report([_rt()], as_of=date(2026, 8, 15)))
    assert text.startswith("# 페이퍼 월간 성과 — 2026-08")


def test_note_warns_that_criteria_need_six_months():
    days = [{"date": f"202608{d:02d}", "total": 100.0 + d} for d in range(1, 29)]
    curve = {"days": days, "n_days": 28, "coverage": 1.0,
             "missing_days": [], "missing_tickers": {}}
    r = mr.build_report([_rt()], as_of=date(2026, 8, 15), curve=curve,
                        bench={d["date"]: 2000.0 for d in days})
    text = mr.render_note(r)
    assert "실전 전환 기준" in text and "6개월에 못 미치는" in text


def test_note_lists_excluded_records_and_says_they_are_kept():
    dropped = [dict(_rt(pnl=-500_000), excluded_kind="stale_price",
                    excluded_reason="진입가가 과거 종가와 일치")]
    text = mr.render_note(mr.build_report([_rt()], as_of=date(2026, 8, 15),
                                          excluded=dropped))
    assert "집계에서 제외한 기록" in text and "DB에 그대로 남아" in text


def test_note_marks_small_samples():
    text = mr.render_note(mr.build_report([_rt()], as_of=date(2026, 8, 15)))
    assert "†" in text and "원칙을 고치지 않습니다" in text


def test_note_reports_an_empty_curve_plainly():
    r = mr.build_report([_rt()], as_of=date(2026, 8, 15),
                        curve={"days": [], "n_days": 0, "coverage": None,
                               "missing_days": [], "missing_tickers": {}},
                        bench={})
    assert "평가할 거래일이 없습니다" in mr.render_note(r)


def test_note_links_principle_notes():
    text = mr.render_note(mr.build_report([_rt(slot="키움")], as_of=date(2026, 8, 15)))
    assert "[[" in text and "연결된 원칙 노트" in text


# ─── JSON ────────────────────────────────────────────


def test_payload_is_json_serialisable_with_dates():
    payload = mr.build_data_payload(mr.build_report([_rt()], as_of=date(2026, 8, 15)))
    assert json.loads(json.dumps(payload, ensure_ascii=False))["month"] == "2026-08"
    assert payload["start"] == "2026-08-01"


def test_payload_drops_the_daily_series():
    """노트 옆 JSON이 수 MB가 되면 아무도 안 읽는다."""
    days = [{"date": f"202608{d:02d}", "total": 100.0 + d} for d in range(1, 29)]
    r = mr.build_report([_rt()], as_of=date(2026, 8, 15),
                        curve={"days": days, "n_days": 28, "coverage": 1.0,
                               "missing_days": [], "missing_tickers": {}},
                        bench={d["date"]: 2000.0 for d in days})
    payload = mr.build_data_payload(r)
    assert "series" not in payload["cumulative_metrics"]
    assert "sharpe" in payload["cumulative_metrics"]


def test_payload_keeps_raw_rows_for_reaggregation():
    payload = mr.build_data_payload(mr.build_report([_rt()], as_of=date(2026, 8, 15)))
    assert payload["rows"][0]["ticker"] == "005930"


# ─── 파일 ────────────────────────────────────────────


def test_paths_are_month_scoped(tmp_path):
    assert mr.note_path(tmp_path, "2026-08").name == "월간_2026-08.md"
    assert mr.data_path(tmp_path, "2026-08").name == "2026-08.json"


def test_write_report_creates_both_and_links_them(tmp_path):
    r = mr.build_report([_rt()], as_of=date(2026, 8, 15))
    note, data = mr.write_report(r, vault_root=tmp_path / "v", state_root=tmp_path / "s")
    assert note.exists() and data.exists()
    assert str(data) in note.read_text(encoding="utf-8")
    assert list(note.parent.glob(".tmp_*")) == []


def test_rerun_overwrites_the_same_month(tmp_path):
    for pnl in (100, 200):
        r = mr.build_report([_rt(pnl=pnl)], as_of=date(2026, 8, 15))
        note, _ = mr.write_report(r, vault_root=tmp_path / "v", state_root=tmp_path / "s")
    assert len(list(note.parent.glob("월간_*.md"))) == 1


def test_cost_is_not_signed_like_a_gain():
    """비용은 손익이 아니다 — '+80,445,032'로 찍히면 이익처럼 읽힌다."""
    text = mr.render_note(mr.build_report([_rt(cost=2_000_000)], as_of=date(2026, 8, 15)))
    assert "진입 원가 2,000,000원" in text and "비용 +" not in text


def test_missing_metric_does_not_render_a_bare_unit():
    """'—%'는 0%처럼 읽힌다."""
    assert mr._unit(None, "%") == "—" and mr._unit(1.5, "%") == "1.5%"
    days = [{"date": f"202608{d:02d}", "total": 100.0 + d} for d in range(1, 6)]
    r = mr.build_report([_rt()], as_of=date(2026, 8, 15),
                        curve={"days": days, "n_days": 5, "coverage": 1.0,
                               "missing_days": [], "missing_tickers": {}},
                        bench={d["date"]: 2000.0 for d in days})
    text = mr.render_note(r)          # 표본이 짧아 베타·알파가 None
    assert "알파 —" in text and "알파 —%" not in text
