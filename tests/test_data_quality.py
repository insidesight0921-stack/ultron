"""test_data_quality.py — 성과 집계 제외(순수) 검증 (hermetic)."""
from __future__ import annotations

import json

import data_quality as dq


def _rt(ticker="010120", slot="키움", buy_at="2026-06-08 10:58:50", pnl=-1_144_023,
        ret=-0.225, name="LS ELECTRIC"):
    return {"slot": slot, "ticker": ticker, "name": name, "pnl": pnl, "ret": ret,
            "buy_at": buy_at, "sell_at": "2026-06-08 11:20:15", "cost": 5_073_000}


def _rule(ticker="010120", slot="키움", buy_at="2026-06-08 10:58:50"):
    return {"slot": slot, "ticker": ticker, "buy_at": buy_at, "kind": "stale_price",
            "reason": "진입가가 4거래일 전 종가와 일치"}


# ─── 매칭 ────────────────────────────────────────────


def test_matching_roundtrip_is_excluded():
    kept, dropped = dq.split([_rt()], [_rule()])
    assert kept == [] and len(dropped) == 1
    assert dropped[0]["excluded_kind"] == "stale_price"


def test_seconds_do_not_have_to_match():
    """기록마다 초가 흔들린다 — 분 단위까지만 본다."""
    kept, dropped = dq.split([_rt(buy_at="2026-06-08 10:58:53")], [_rule()])
    assert len(dropped) == 1


def test_different_minute_is_not_excluded():
    kept, _ = dq.split([_rt(buy_at="2026-06-08 13:01:56")], [_rule()])
    assert len(kept) == 1


def test_different_ticker_or_slot_is_not_excluded():
    kept, _ = dq.split([_rt(ticker="005930"), _rt(slot="콴텍")], [_rule()])
    assert len(kept) == 2


def test_no_rules_keeps_everything():
    kept, dropped = dq.split([_rt(), _rt(ticker="005930")], [])
    assert len(kept) == 2 and dropped == []


def test_split_does_not_mutate_input():
    rt = _rt()
    dq.split([rt], [_rule()])
    assert "excluded_reason" not in rt


# ─── 요약 ────────────────────────────────────────────


def test_summary_reports_what_was_removed():
    _, dropped = dq.split([_rt(), _rt(ticker="006800", pnl=-964_587, ret=-0.184)],
                          [_rule(), _rule(ticker="006800")])
    s = dq.summary(dropped)
    assert s["n"] == 2 and s["pnl"] == -2_108_610 and s["kinds"] == ["stale_price"]


def test_format_says_records_are_kept():
    _, dropped = dq.split([_rt()], [_rule()])
    text = dq.format_summary(dq.summary(dropped))
    assert "DB에 그대로 남아" in text and "집계에서만" in text


def test_format_empty_is_empty():
    assert dq.format_summary(dq.summary([])) == ""


# ─── 파일 경계 ───────────────────────────────────────


def test_load_missing_or_broken_returns_empty(tmp_path):
    """제외를 못 해도 집계 자체는 돌아야 한다."""
    assert dq.load_rules(tmp_path / "none.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert dq.load_rules(bad) == []


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "state" / "excluded_trades.json"
    dq.save_rules([_rule()], p)
    assert dq.load_rules(p)[0]["ticker"] == "010120"
    assert list(p.parent.glob(".tmp_*")) == []
    assert "rules" in json.loads(p.read_text(encoding="utf-8"))


def test_load_accepts_bare_list(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps([_rule()]), encoding="utf-8")
    assert len(dq.load_rules(p)) == 1


def test_apply_uses_the_rules_file(tmp_path):
    p = tmp_path / "r.json"
    dq.save_rules([_rule()], p)
    kept, dropped = dq.apply([_rt(), _rt(ticker="005930")], p)
    assert len(kept) == 1 and len(dropped) == 1


# ─── 라벨 전용 규칙 ──────────────────────────────────


def test_label_only_rule_keeps_the_row_in_totals():
    """모든 의심 기록을 빼면 표본이 사라진다 — 뺄 것과 표시만 할 것을 나눈다."""
    rule = dict(_rule(), exclude=False, kind="fill_assumption",
                reason="장외 시각 진입 — 전 거래일 종가 사용")
    kept, dropped = dq.split([_rt()], [rule])
    assert dropped == [] and len(kept) == 1
    assert kept[0]["flag_kind"] == "fill_assumption"
    assert "excluded_kind" not in kept[0]


def test_flagged_lists_label_only_rows():
    rule = dict(_rule(), exclude=False, kind="fill_assumption")
    kept, _ = dq.split([_rt(), _rt(ticker="005930")], [rule])
    assert [r["ticker"] for r in dq.flagged(kept)] == ["010120"]


def test_is_exclusion_defaults_to_true():
    assert dq.is_exclusion({}) is True
    assert dq.is_exclusion({"exclude": False}) is False
    assert dq.is_exclusion({"exclude": True}) is True


def test_excluded_rows_carry_both_labels():
    _, dropped = dq.split([_rt()], [_rule()])
    assert dropped[0]["flag_kind"] == "stale_price"
    assert dropped[0]["excluded_kind"] == "stale_price"


# ─── 체결가가 그날 범위 밖 (2026-10-26) ────


def _fill_rt(buy_at="2026-07-28 09:17:18", sell_at="2026-07-28 09:17:40",
        buy_price=614921.0, sell_price=529205.0, ticker="011070", slot="키움", pnl=-600000):
    return {"slot": slot, "ticker": ticker, "name": "LG이노텍", "buy_at": buy_at,
            "sell_at": sell_at, "buy_price": buy_price, "sell_price": sell_price, "pnl": pnl}


def _bars(table):
    return lambda ticker, day: table.get((ticker, day))


BARS = _bars({("011070", "20260728"): (504000.0, 575000.0),
              ("011070", "20260901"): (500000.0, 520000.0)})


def test_a_fill_above_the_days_high_is_out_of_range():
    """실측 사례: 07-28 LG이노텍 매수가 614,921 · 그날 고가 575,000."""
    got = dq.fill_out_of_range(_fill_rt(), BARS)
    assert len(got) == 1 and got[0].startswith("매수가 614,921원")


def test_slippage_inside_the_tolerance_is_not_flagged():
    rt = _fill_rt(buy_price=575000 * 1.004, sell_price=530000.0)
    assert dq.fill_out_of_range(rt, BARS) == []


def test_unknown_bars_are_not_called_normal():
    """음성 대조: 일봉이 없으면 「정상」이 아니라 「검사 불가」(None)다."""
    assert dq.fill_out_of_range(_fill_rt(ticker="999999"), BARS) is None


def test_gains_and_losses_are_judged_alike():
    """수익 건도 같은 기준으로 걸린다 — 손실만 골라 빼면 데이터를 고르는 것이 된다."""
    win = _fill_rt(buy_price=480000.0, sell_price=560000.0, pnl=+800000)
    assert dq.fill_out_of_range(win, BARS)


def test_rules_exclude_before_the_safeguard_and_only_label_after():
    before = _fill_rt()
    after = _fill_rt(buy_at="2026-09-01 10:00:00", sell_at="2026-09-01 20:12:00",
                buy_price=510000.0, sell_price=600000.0)
    rules = dq.fill_rules([before, after], BARS, detected_at="2026-10-26")
    assert [r["exclude"] for r in rules] == [True, False]
    assert all(r["kind"] == dq.FILL_KIND for r in rules)


def test_split_sells_of_one_buy_make_one_rule():
    rules = dq.fill_rules([_fill_rt(), _fill_rt(sell_at="2026-07-28 09:27:40")], BARS,
                          detected_at="2026-10-26")
    assert len(rules) == 1


def test_merge_replaces_a_label_only_rule_and_keeps_the_old_judgement():
    old = {"slot": "키움", "ticker": "011070", "buy_at": "2026-07-28 09:17:18",
           "kind": "fill_assumption", "exclude": False, "reason": "전 거래일 종가"}
    new = dq.fill_rules([_fill_rt()], BARS, detected_at="2026-10-26")
    merged, changes = dq.merge_rules([old], new)
    assert len(merged) == 1 and merged[0]["kind"] == dq.FILL_KIND and merged[0]["exclude"]
    assert merged[0]["supersedes"]["kind"] == "fill_assumption"
    assert changes and changes[0].startswith("교체")


def test_merge_leaves_an_existing_exclusion_alone_and_is_idempotent():
    stale = {"slot": "키움", "ticker": "011070", "buy_at": "2026-07-28 09:17:18",
             "kind": "stale_price", "exclude": True, "reason": "4거래일 전 종가"}
    new = dq.fill_rules([_fill_rt()], BARS, detected_at="2026-10-26")
    merged, changes = dq.merge_rules([stale], new)
    assert merged == [stale] and changes == []
    once, _ = dq.merge_rules([], new)
    twice, changes2 = dq.merge_rules(once, new)
    assert twice == once and changes2 == []


def test_the_new_rule_actually_removes_the_roundtrip_from_totals():
    rules, _ = dq.merge_rules([], dq.fill_rules([_fill_rt()], BARS, detected_at="2026-10-26"))
    kept, dropped = dq.split([_fill_rt()], rules)
    assert kept == [] and dropped[0]["excluded_kind"] == dq.FILL_KIND
