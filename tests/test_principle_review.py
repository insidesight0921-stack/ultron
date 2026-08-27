"""test_principle_review.py — 원칙 갱신 제안 초안(순수) 검증 (hermetic)."""
from __future__ import annotations

import json
from datetime import date

import principle_review as pr


def _rt(pnl, ticker="005930", tags=None, slot="마이퀀트", period="2026-W35"):
    return {"slot": slot, "ticker": ticker, "name": ticker, "pnl": pnl,
            "cost": 1000.0, "ret": pnl / 1000.0, "hold_days": 3,
            "sell_at": "2026-08-25", "tags": tags or [], "period": period}


def _rows(n_win, n_loss, win=200, loss=-100, **kw):
    return ([_rt(win, ticker=f"T{i:03d}", **kw) for i in range(n_win)]
            + [_rt(loss, ticker=f"L{i:03d}", **kw) for i in range(n_loss)])


# ─── 입력 ────────────────────────────────────────────


def test_load_weekly_payloads_sorted_and_skips_broken(tmp_path):
    (tmp_path / "2026-W34.json").write_text(json.dumps({"period": "2026-W34"}), encoding="utf-8")
    (tmp_path / "2026-W35.json").write_text(json.dumps({"period": "2026-W35"}), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    out = pr.load_weekly_payloads(tmp_path)
    assert [p["period"] for p in out] == ["2026-W34", "2026-W35"]


def test_collect_rows_tags_period():
    payloads = [{"period": "2026-W34", "roundtrips": [{"pnl": 1}]},
                {"period": "2026-W35", "roundtrips": [{"pnl": 2}]}]
    rows = pr.collect_rows(payloads)
    assert [r["period"] for r in rows] == ["2026-W34", "2026-W35"]


def test_collect_rows_handles_missing_roundtrips():
    assert pr.collect_rows([{"period": "2026-W35"}]) == []


# ─── 그룹 ────────────────────────────────────────────


def test_by_tag_counts_multi_tag_rows_in_each():
    rows = [_rt(100, tags=["정배열", "신고가돌파"])]
    grouped = pr.by_tag(rows)
    assert set(grouped) == {"정배열", "신고가돌파"}


def test_by_slot_groups_unknown_as_question():
    assert "?" in pr.by_slot([{"pnl": 1}])


# ─── 반례 탐색 ───────────────────────────────────────


def test_counterevidence_flags_single_win_concentration():
    rows = [_rt(1000, ticker="A"), _rt(50, ticker="B"), _rt(-100, ticker="C")]
    notes = pr.counterevidence(rows)["notes"]
    assert any("한 건에서 나왔다" in n for n in notes)


def test_counterevidence_flags_single_ticker_dominance():
    rows = [_rt(100, ticker="A", period=f"2026-W3{i}") for i in range(3)] + \
           [_rt(-50, ticker="B", period="2026-W34")]
    notes = pr.counterevidence(rows)["notes"]
    assert any("표본의" in n for n in notes)


def test_counterevidence_flags_single_period():
    rows = [_rt(100, ticker=f"T{i}") for i in range(5)]
    assert any("한 주차" in n for n in pr.counterevidence(rows)["notes"])


def test_counterevidence_quiet_when_spread():
    rows = [_rt(100, ticker=f"W{i}", period=f"2026-W3{i%3}") for i in range(6)] + \
           [_rt(-90, ticker=f"L{i}", period=f"2026-W3{i%3}") for i in range(6)]
    assert pr.counterevidence(rows)["notes"] == []


def test_counterevidence_empty_rows():
    assert pr.counterevidence([])["n"] == 0


# ─── 판정 ────────────────────────────────────────────


def test_hold_when_sample_too_small():
    stats = {"n": 5, "profit_factor": 3.0, "win_rate": 80.0}
    verdict, reason = pr.judge(stats, {"notes": []}, min_sample=20)
    assert verdict == pr.VERDICT_HOLD and "미달" in reason


def test_strengthen_on_good_numbers():
    stats = {"n": 30, "profit_factor": 1.6, "win_rate": 55.0}
    assert pr.judge(stats, {"notes": []})[0] == pr.VERDICT_STRENGTHEN


def test_counterevidence_downgrades_strengthen_to_hold():
    """좋아 보여도 반례가 있으면 강화하지 않는다 — 확증 편향 방어."""
    stats = {"n": 30, "profit_factor": 1.6, "win_rate": 55.0}
    verdict, reason = pr.judge(stats, {"notes": ["이익의 90%가 단 한 건에서 나왔다"]})
    assert verdict == pr.VERDICT_HOLD and "반례" in reason


def test_revise_on_weak_profit_factor():
    stats = {"n": 40, "profit_factor": 0.6, "win_rate": 30.0}
    assert pr.judge(stats, {"notes": []})[0] == pr.VERDICT_REVISE


def test_watch_in_between():
    stats = {"n": 40, "profit_factor": 1.0, "win_rate": 48.0}
    assert pr.judge(stats, {"notes": []})[0] == pr.VERDICT_WATCH


def test_high_pf_but_low_win_rate_is_not_strengthened():
    stats = {"n": 40, "profit_factor": 1.5, "win_rate": 30.0}
    assert pr.judge(stats, {"notes": []})[0] == pr.VERDICT_WATCH


def test_no_losses_cannot_conclude():
    stats = {"n": 40, "profit_factor": None, "win_rate": 100.0}
    verdict, reason = pr.judge(stats, {"notes": []})
    assert verdict == pr.VERDICT_WATCH and "손실 표본이 없어" in reason


# ─── 제안 ────────────────────────────────────────────


def test_build_proposals_covers_tags_and_slots():
    rows = _rows(3, 2, tags=["정배열"])
    targets = {p["target"] for p in pr.build_proposals(rows)}
    assert "조건 정배열" in targets and "슬롯 마이퀀트" in targets


def test_proposals_sorted_revise_first():
    rows = _rows(1, 30, tags=["나쁜조건"], win=100, loss=-500)
    verdicts = [p["verdict"] for p in pr.build_proposals(rows)]
    assert verdicts[0] == pr.VERDICT_REVISE


def test_proposal_links_principle_note():
    rows = _rows(2, 2, tags=["정배열"])
    tag_p = next(p for p in pr.build_proposals(rows) if p["kind"] == "tag")
    assert tag_p["note"] == "마이퀀트_진입조건"


def test_slot_proposal_links_first_principle():
    rows = _rows(2, 2, slot="키움")
    slot_p = next(p for p in pr.build_proposals(rows) if p["kind"] == "slot")
    assert slot_p["note"] == "모멘텀_전략_원칙"


# ─── 렌더 ────────────────────────────────────────────


def test_render_marks_draft_and_no_auto_apply():
    text = pr.render_review(pr.build_proposals(_rows(2, 2, tags=["정배열"])),
                            as_of=date(2026, 8, 27), periods=["2026-W35"])
    assert "초안이다" in text and "vault에 넣지 않는다" in text


def test_render_includes_rules_since_reminder():
    text = pr.render_review(pr.build_proposals(_rows(2, 2)), as_of=date(2026, 8, 27),
                            periods=["2026-W35"])
    assert "RULES_SINCE" in text


def test_render_empty_proposals():
    text = pr.render_review([], as_of=date(2026, 8, 27), periods=[])
    assert "제안할 것이 없다" in text


def test_render_shows_counterevidence_line():
    rows = [_rt(1000, ticker="A", tags=["정배열"])] + \
           [_rt(-50, ticker=f"L{i}", tags=["정배열"]) for i in range(3)]
    text = pr.render_review(pr.build_proposals(rows), as_of=date(2026, 8, 27),
                            periods=["2026-W35"])
    assert "반례:" in text
