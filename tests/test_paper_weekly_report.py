"""test_paper_weekly_report.py — 주간 성과 노트 생성(순수 계산·렌더) 검증 (hermetic)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import paper_weekly_report as wr


def _rt(slot, pnl, sell_at, buy_notes=None, cost=1000.0):
    return {"slot": slot, "ticker": "005930", "name": "삼성전자", "pnl": pnl,
            "cost": cost, "ret": pnl / cost, "buy_at": "2026-08-20 10:00",
            "sell_at": sell_at, "buy_notes": buy_notes}


# ─── 기간 ────────────────────────────────────────────


def test_iso_week_label():
    assert wr.iso_week_label(date(2026, 8, 26)) == "2026-W35"


def test_week_bounds_is_monday_to_sunday():
    start, end = wr.week_bounds(date(2026, 8, 26))  # 수요일
    assert start == date(2026, 8, 24) and end == date(2026, 8, 30)
    assert start.weekday() == 0 and end.weekday() == 6


def test_in_period_uses_sell_date_not_buy_date():
    start, end = date(2026, 8, 24), date(2026, 8, 30)
    inside = {"buy_at": "2026-07-01 10:00", "sell_at": "2026-08-26 10:00"}
    outside = {"buy_at": "2026-08-25 10:00", "sell_at": "2026-09-02 10:00"}
    assert wr.in_period(inside, start, end)
    assert not wr.in_period(outside, start, end)


def test_open_position_is_never_in_period():
    """미청산(sell_at 없음)은 어떤 주간에도 귀속되지 않는다 — 평가손익 미반영."""
    assert not wr.in_period({"sell_at": None}, date(2026, 8, 24), date(2026, 8, 30))


def test_bad_date_is_excluded_not_raised():
    assert not wr.in_period({"sell_at": "미상"}, date(2026, 8, 24), date(2026, 8, 30))


# ─── 집계 ────────────────────────────────────────────


def test_aggregate_by_slot_and_total():
    rts = [_rt("키움", 300, "2026-08-25"), _rt("키움", -100, "2026-08-26"),
           _rt("콴텍", 50, "2026-08-24")]
    rep = wr.build_report(rts, as_of=date(2026, 8, 26))
    slots = {row["slot"]: row for row in rep["slots"]}
    assert slots["키움"]["n"] == 2 and slots["키움"]["pnl"] == 200
    assert slots["키움"]["win_rate"] == 50.0
    assert slots["키움"]["payoff"] == 3.0          # 평균이익 300 / 평균손실 100
    assert rep["total"]["n"] == 3 and rep["total"]["pnl"] == 250


def test_other_week_trades_are_excluded():
    rts = [_rt("키움", 999, "2026-08-10"), _rt("키움", 100, "2026-08-26")]
    rep = wr.build_report(rts, as_of=date(2026, 8, 26))
    assert rep["total"]["n"] == 1 and rep["total"]["pnl"] == 100


def test_empty_week_is_zero_not_error():
    rep = wr.build_report([], as_of=date(2026, 8, 26))
    assert rep["total"]["n"] == 0 and rep["total"]["pnl"] == 0
    assert rep["total"]["win_rate"] is None and rep["total"]["payoff"] is None
    assert rep["slots"] == [] and rep["tags"] == []


def test_payoff_is_none_without_both_sides():
    rep = wr.build_report([_rt("키움", 300, "2026-08-25")], as_of=date(2026, 8, 26))
    assert rep["slots"][0]["payoff"] is None       # 손실 표본 없음 → 계산 불가


# ─── 마이퀀트 태그 ───────────────────────────────────


def test_extract_tags():
    assert wr.extract_tags("MQ[추세위+눌림,정배열] 스캔매수") == ["추세위+눌림", "정배열"]
    assert wr.extract_tags("그냥 메모") == []
    assert wr.extract_tags(None) == []
    assert wr.extract_tags("MQ[") == []            # 닫는 괄호 없음


def test_multi_tag_counts_in_each_tag():
    rts = [_rt(wr.MYQUANT_SLOT, 200, "2026-08-25", "MQ[정배열,신고가돌파]")]
    rep = wr.build_report(rts, as_of=date(2026, 8, 26))
    tags = {row["tag"]: row for row in rep["tags"]}
    assert tags["정배열"]["pnl"] == 200 and tags["신고가돌파"]["pnl"] == 200
    assert rep["total"]["n"] == 1                  # 합계는 중복 계상하지 않는다


def test_tags_only_from_myquant_slot():
    rts = [_rt("키움", 100, "2026-08-25", "MQ[정배열]")]
    assert wr.build_report(rts, as_of=date(2026, 8, 26))["tags"] == []


# ─── 백링크 ──────────────────────────────────────────


def test_principles_include_traded_slots_and_common():
    rep = wr.build_report([_rt("IPO", 10, "2026-08-25")], as_of=date(2026, 8, 26))
    assert "IPO_매력지수_기준" in rep["principles"]
    assert "리스크_관리_원칙" in rep["principles"]
    assert "모멘텀_전략_원칙" not in rep["principles"]   # 거래 없는 슬롯은 제외


# ─── 렌더 ────────────────────────────────────────────


def test_render_has_frontmatter_with_split_timestamps():
    rep = wr.build_report([_rt("키움", 100, "2026-08-25")], as_of=date(2026, 8, 26))
    text = wr.render_note(rep)
    assert text.startswith("---\ntype: paper-weekly\n")
    assert "판단시점: 2026-08-24" in text          # 주 시작
    assert "평가시점: 2026-08-26" in text          # 집계 실행일


def test_render_marks_small_sample():
    rep = wr.build_report([_rt("키움", 100, "2026-08-25")], as_of=date(2026, 8, 26))
    assert "| 키움† |" in wr.render_note(rep)


def test_render_omits_dagger_at_min_sample():
    rts = [_rt("키움", 10, "2026-08-25") for _ in range(wr.MIN_SAMPLE_N)]
    assert "| 키움 |" in wr.render_note(wr.build_report(rts, as_of=date(2026, 8, 26)))


def test_render_backlinks_are_wikilinks():
    rep = wr.build_report([_rt("콴텍", 10, "2026-08-25")], as_of=date(2026, 8, 26))
    assert "- [[국면별_팩터_가중]]" in wr.render_note(rep)


def test_render_empty_week_says_so():
    text = wr.render_note(wr.build_report([], as_of=date(2026, 8, 26)))
    assert "완결 거래가 없다" in text


def test_render_is_deterministic():
    rep = wr.build_report([_rt("키움", 100, "2026-08-25")], as_of=date(2026, 8, 26))
    assert wr.render_note(rep) == wr.render_note(rep)


# ─── 파일 쓰기 ───────────────────────────────────────


def test_note_path_layout(tmp_path):
    path = wr.note_path(tmp_path, "2026-W35")
    assert path == tmp_path / "wiki" / "투자" / "성과" / "주간_2026-W35.md"


def test_write_atomic_creates_and_overwrites(tmp_path):
    target = wr.note_path(tmp_path, "2026-W35")
    wr.write_atomic(target, "첫 실행")
    wr.write_atomic(target, "두 번째 실행")
    assert target.read_text(encoding="utf-8") == "두 번째 실행"
    leftovers = list(target.parent.glob(".tmp_*"))
    assert leftovers == []                          # 임시 파일이 남지 않는다


# ─── 거래 없는 슬롯 처리 ─────────────────────────────


def test_slot_order_does_not_inject_empty_rows():
    rep = wr.build_report([_rt("키움", 100, "2026-08-25")], as_of=date(2026, 8, 26),
                          slot_order=["콴텍", "키움", "IPO", wr.MYQUANT_SLOT])
    assert [row["slot"] for row in rep["slots"]] == ["키움"]
    assert rep["idle_slots"] == ["콴텍", "IPO", wr.MYQUANT_SLOT]


def test_slot_order_controls_display_order():
    rts = [_rt(wr.MYQUANT_SLOT, 10, "2026-08-25"), _rt("콴텍", 10, "2026-08-25")]
    rep = wr.build_report(rts, as_of=date(2026, 8, 26),
                          slot_order=["콴텍", "키움", "IPO", wr.MYQUANT_SLOT])
    assert [row["slot"] for row in rep["slots"]] == ["콴텍", wr.MYQUANT_SLOT]


def test_unknown_slot_still_appears():
    """slot_order에 없는 슬롯도 빠뜨리지 않는다."""
    rep = wr.build_report([_rt("신규봇", 10, "2026-08-25")], as_of=date(2026, 8, 26),
                          slot_order=["콴텍", "키움"])
    assert [row["slot"] for row in rep["slots"]] == ["신규봇"]


def test_render_lists_idle_slots():
    rep = wr.build_report([_rt("키움", 100, "2026-08-25")], as_of=date(2026, 8, 26),
                          slot_order=["콴텍", "키움", "IPO"])
    text = wr.render_note(rep)
    assert "이번 주 완결 거래 없음: 콴텍, IPO" in text
    assert "| 콴텍" not in text


# ─── v2: 충분통계 · 누적 · 추이 · JSON ───────────────


def test_group_stats_keeps_sufficient_statistics():
    """승률·손익비만 있으면 주차를 합칠 수 없다. 합칠 수 있는 원자료를 남긴다."""
    rts = [_rt("키움", 300, "2026-08-25"), _rt("키움", -100, "2026-08-26"),
           _rt("키움", 0, "2026-08-26")]
    s = wr.group_stats(rts)
    assert (s["n"], s["wins"], s["losses"], s["flats"]) == (3, 1, 1, 1)
    assert s["sum_win"] == 300 and s["sum_loss"] == 100
    assert s["cost"] == 3000 and s["pnl"] == 200
    assert s["profit_factor"] == 3.0


def test_group_stats_are_additive_across_weeks():
    """두 주차의 충분통계를 더한 값 == 합쳐서 계산한 값."""
    w1 = [_rt("키움", 300, "2026-08-25"), _rt("키움", -100, "2026-08-26")]
    w2 = [_rt("키움", 200, "2026-09-01")]
    a, b, both = wr.group_stats(w1), wr.group_stats(w2), wr.group_stats(w1 + w2)
    for key in ("n", "wins", "losses", "sum_win", "sum_loss", "cost", "pnl"):
        assert a[key] + b[key] == both[key], key


def test_cumulative_includes_past_weeks_only_up_to_period_end():
    rts = [_rt("키움", 100, "2026-08-10"), _rt("키움", 50, "2026-08-25"),
           _rt("키움", 999, "2026-09-10")]           # 이번 주 이후 → 제외
    rep = wr.build_report(rts, as_of=date(2026, 8, 26))
    assert rep["cumulative"]["n"] == 2 and rep["cumulative"]["pnl"] == 150
    assert rep["total"]["n"] == 1                    # 주간은 이번 주만


def test_trend_is_oldest_to_newest_and_has_requested_length():
    rts = [_rt("키움", 100, "2026-08-25"), _rt("키움", 40, "2026-08-18")]
    rep = wr.build_report(rts, as_of=date(2026, 8, 26), trend_weeks=3)
    weeks = [row["week"] for row in rep["trend"]]
    assert weeks == ["2026-W33", "2026-W34", "2026-W35"]
    assert rep["trend"][-1]["pnl"] == 100 and rep["trend"][-2]["pnl"] == 40


def test_rows_carry_tags_and_are_sorted_by_sell_date():
    rts = [_rt(wr.MYQUANT_SLOT, 10, "2026-08-26", "MQ[정배열]"),
           _rt("키움", 20, "2026-08-24")]
    rows = wr.build_report(rts, as_of=date(2026, 8, 26))["rows"]
    assert [r["sell_at"] for r in rows] == ["2026-08-24", "2026-08-26"]
    assert rows[1]["tags"] == ["정배열"]


def test_frontmatter_is_flat_scalars_for_dataview():
    rep = wr.build_report([_rt("키움", 100, "2026-08-25")], as_of=date(2026, 8, 26))
    fm = wr.frontmatter(rep, data_file="/x/2026-W35.json")
    assert "schema: 1" in fm and "closed_n: 1" in fm and "pnl: 100" in fm
    assert 'slots: ["키움"]' in fm
    assert "cum_pnl: 100" in fm
    assert "data_file: /x/2026-W35.json" in fm
    assert "roundtrips" not in fm                     # 큰 데이터는 노트에 넣지 않는다


def test_data_payload_roundtrips_and_stats():
    rep = wr.build_report([_rt("키움", 100, "2026-08-25")], as_of=date(2026, 8, 26))
    payload = wr.build_data_payload(rep)
    assert payload["schema"] == wr.SCHEMA_VERSION
    assert payload["period"] == "2026-W35"
    assert len(payload["roundtrips"]) == 1
    assert payload["total"]["sum_win"] == 100


def test_write_report_writes_note_and_json(tmp_path):
    rep = wr.build_report([_rt("키움", 100, "2026-08-25")], as_of=date(2026, 8, 26))
    note, data = wr.write_report(rep, vault_root=tmp_path / "vault",
                                 state_root=tmp_path / "state")
    assert note.exists() and data.exists()
    payload = json.loads(data.read_text(encoding="utf-8"))
    assert payload["period"] == "2026-W35"
    assert str(data) in note.read_text(encoding="utf-8")   # 노트가 JSON을 가리킨다


def test_write_report_is_idempotent(tmp_path):
    rep = wr.build_report([_rt("키움", 100, "2026-08-25")], as_of=date(2026, 8, 26))
    first = wr.write_report(rep, vault_root=tmp_path / "v", state_root=tmp_path / "s")
    second = wr.write_report(rep, vault_root=tmp_path / "v", state_root=tmp_path / "s")
    assert first == second
    assert len(list((tmp_path / "s" / "paper_weekly").glob("*"))) == 1
