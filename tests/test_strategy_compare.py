"""test_strategy_compare.py — 전략별 성과 비교(순수) 검증 (hermetic)."""
from __future__ import annotations

from datetime import date

import pytest

import strategy_compare as sc


def _rt(pnl=1000, ret=5.0, slot="키움", reason="익절", hold=3,
        buy_at="2026-08-03 10:00", notes=None, cost=20000):
    return {"slot": slot, "ticker": "005930", "name": "삼성전자", "pnl": pnl,
            "cost": cost, "qty": 1, "ret": ret, "buy_at": buy_at,
            "sell_at": "2026-08-06 10:00", "buy_notes": notes,
            "hold_days": hold, "reason": reason}


# ─── 라벨 ────────────────────────────────────────────


def test_hold_buckets_cover_the_range():
    assert sc.hold_label(_rt(hold=0)) == "당일~1일"
    assert sc.hold_label(_rt(hold=5)) == "2~5일"
    assert sc.hold_label(_rt(hold=20)) == "6~20일"
    assert sc.hold_label(_rt(hold=21)) == "21일+"
    assert sc.hold_label(_rt(hold=None)) == "미상"


def test_era_splits_on_sell_date():
    """2026-07-23 변경은 청산 규칙이다. 효과는 그 이후에 판 거래에 나타난다."""
    since = date(2026, 7, 23)
    saved = _rt(buy_at="2026-07-20 10:00")
    saved["sell_at"] = "2026-07-24 10:00"        # 구 규칙 때 사서 새 규칙으로 잘라낸 건
    assert sc.era_label(saved, since) == "현 규칙"
    before = _rt(buy_at="2026-07-01 10:00")
    before["sell_at"] = "2026-07-22 10:00"
    assert sc.era_label(before, since) == "구 규칙"


def test_era_matches_weekly_report_basis():
    """같은 것을 두 화면이 다르게 말하면 어느 쪽도 믿을 수 없다."""
    import paper_weekly_report as pw
    since = pw.RULES_SINCE
    rts = [_rt(), _rt()]
    rts[0]["sell_at"] = (since.replace(day=since.day)).isoformat() + " 10:00"
    rts[1]["sell_at"] = "2026-01-02 10:00"
    mine = [r for r in rts if sc.era_label(r, since) == "현 규칙"]
    theirs = [r for r in rts if str(r["sell_at"])[:10] >= since.isoformat()]
    assert mine == theirs


def test_untagged_is_labeled_not_dropped():
    """조용히 빠지면 커버리지가 100%처럼 보인다."""
    assert sc.tag_labels(_rt(notes=None)) == [sc.UNTAGGED]


def test_tag_labels_use_injected_extractor():
    assert sc.tag_labels(_rt(notes="x"), extract=lambda n: ["눌림", "정배열"]) == ["눌림", "정배열"]


# ─── 버킷 ────────────────────────────────────────────


def test_bucketize_groups_by_axis():
    rts = [_rt(slot="키움"), _rt(slot="콴텍"), _rt(slot="키움")]
    b = sc.bucketize(rts, "slot")
    assert len(b["키움"]) == 2 and len(b["콴텍"]) == 1


def test_multi_label_axis_counts_in_every_bucket():
    rts = [_rt(notes="MQ[눌림,정배열]")]
    b = sc.bucketize(rts, sc.TAG_AXIS)
    assert set(b) == {"눌림", "정배열"}


def test_unknown_axis_raises():
    with pytest.raises(ValueError):
        sc.compare([_rt()], "없는축")


# ─── 비교 ────────────────────────────────────────────


def test_compare_rows_carry_edge_against_total():
    rts = [_rt(slot="키움", ret=0.10, pnl=1000), _rt(slot="콴텍", ret=0.0, pnl=0)]
    c = sc.compare(rts, "slot")
    rows = {r["label"]: r for r in c["rows"]}
    assert c["total"]["avg_ret"] == 5.0
    assert rows["키움"]["edge_ret"] == 5.0 and rows["콴텍"]["edge_ret"] == -5.0


def test_avg_ret_converts_fraction_to_percent():
    """compute_roundtrips의 ret은 비율이다. %로 쓰면서 소수를 내보내면 -14%가 -0.14%가 된다."""
    assert sc.avg_ret([{"ret": -0.1405}]) == -14.05


def test_compare_sorts_by_pnl_desc():
    rts = [_rt(slot="키움", pnl=-500), _rt(slot="콴텍", pnl=900)]
    assert [r["label"] for r in sc.compare(rts, "slot")["rows"]] == ["콴텍", "키움"]


def test_small_sample_is_flagged():
    c = sc.compare([_rt()], "slot")
    assert c["rows"][0]["small"] is True


def test_large_sample_is_not_flagged():
    c = sc.compare([_rt() for _ in range(sc.MIN_SAMPLE_N)], "slot")
    assert c["rows"][0]["small"] is False


def test_compare_keeps_sufficient_statistics():
    """승률·손익비는 주차를 합산할 수 없다. 원자료가 함께 남아야 한다."""
    c = sc.compare([_rt(pnl=100), _rt(pnl=-50)], "slot")
    r = c["rows"][0]
    assert {"n", "wins", "losses", "sum_win", "sum_loss", "cost"} <= set(r)
    assert r["sum_win"] == 100 and r["sum_loss"] == 50


def test_compare_empty_is_safe():
    c = sc.compare([], "slot")
    assert c["rows"] == [] and c["total"]["n"] == 0 and c["total"]["avg_ret"] is None


def test_share_n_sums_to_100_on_single_label_axis():
    rts = [_rt(slot="키움"), _rt(slot="키움"), _rt(slot="콴텍")]
    c = sc.compare(rts, "slot")
    assert round(sum(r["share_n"] for r in c["rows"])) == 100


def test_avg_ret_ignores_missing_ret():
    rts = [_rt(ret=0.10), _rt(ret=None)]
    assert sc.avg_ret(rts) == 10.0
    assert sc.avg_ret([_rt(ret=None)]) is None


# ─── 귀속 커버리지 ───────────────────────────────────


def test_attribution_reports_zero_coverage_plainly():
    att = sc.attribution([_rt() for _ in range(78)])
    assert att["total"] == 78 and att["tagged"] == 0
    assert att["coverage"] == 0.0 and att["usable"] is False


def test_attribution_usable_only_above_min_sample():
    tagged = [_rt(notes="MQ[눌림]") for _ in range(sc.MIN_SAMPLE_N)]
    assert sc.attribution(tagged)["usable"] is True
    assert sc.attribution(tagged[:-1])["usable"] is False


def test_attribution_empty_has_no_coverage():
    assert sc.attribution([])["coverage"] is None


# ─── 요약 ────────────────────────────────────────────


def test_overview_has_every_axis():
    ov = sc.overview([_rt()])
    assert set(ov["axes"]) == set(sc.AXES)


def test_format_warns_when_attribution_is_missing():
    text = sc.format_overview(sc.overview([_rt() for _ in range(10)]))
    assert "진입 조건별 비교는 아직 불가능" in text


def test_format_omits_warning_when_tagged():
    rts = [_rt(notes="MQ[눌림]") for _ in range(10)]
    assert "아직 불가능" not in sc.format_overview(sc.overview(rts))


def test_format_reports_empty_state():
    assert "아직 없습니다" in sc.format_overview(sc.overview([]))


def test_every_axis_carries_a_caveat():
    """숫자만 띄우면 반드시 인과로 읽힌다 — 오독 지점을 데이터에 함께 넣는다."""
    for axis in sc.AXES:
        assert sc.compare([_rt()], axis)["caveat"].strip()


def test_hold_caveat_names_the_exit_rule_confound():
    assert "인과로 읽지" in sc.compare([_rt()], "hold")["caveat"]


# ─── 데이터 품질 ─────────────────────────────────────


def test_outliers_flag_impossible_returns():
    """+247%는 스윙 매매 성적이 아니라 입력 오류다."""
    rts = [_rt(ret=2.477, pnl=1_984_615), _rt(ret=0.15)]
    odd = sc.outliers(rts)
    assert len(odd) == 1 and odd[0]["ret"] == 247.7


def test_outliers_are_not_removed_from_totals():
    """조용히 빼면 나중에 왜 숫자가 달라졌는지 아무도 모른다."""
    rts = [_rt(ret=2.477, pnl=1_984_615), _rt(ret=0.15, pnl=100)]
    ov = sc.overview(rts)
    assert ov["n"] == 2
    assert ov["axes"]["slot"]["total"]["pnl"] == 1_984_715
    assert ov["outlier_pnl"] == 1_984_615


def test_outliers_ignore_missing_return():
    assert sc.outliers([_rt(ret=None)]) == []


def test_format_warns_about_outliers():
    text = sc.format_overview(sc.overview([_rt(ret=2.477, pnl=1_984_615)]))
    assert "이상치" in text and "그대로 포함" in text


def test_format_has_no_outlier_warning_when_clean():
    assert "이상치" not in sc.format_overview(sc.overview([_rt(ret=0.05)]))
