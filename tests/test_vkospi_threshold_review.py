"""test_vkospi_threshold_review.py — 임계값을 사람이 승인해도 죽은 가지는 못 만든다.

이 모듈이 막으려는 실패는 이미 한 번 일어났다. `<15`는 407일 중 **0일** 걸렸고,
그건 누가 승인해서가 아니라 아무도 분포를 안 봐서였다. 그래서 여기서는
**승인 경로에도 같은 검증을 건다** — 버튼은 근거가 아니다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import vkospi_threshold_review as vtr  # noqa: E402


def _dist(n=400, lo=15.0, hi=95.0):
    """15~95를 고르게 채운 표본. 백분위가 예측 가능하다."""
    step = (hi - lo) / (n - 1)
    return [lo + step * i for i in range(n)]


# ─── 검증 ────────────────────────────────────────────


def test_a_dead_branch_is_rejected():
    """실제로 겪은 실패: 하단이 한 번도 안 걸린다."""
    out = vtr.validate(_dist(), high=60.0, low=10.0)   # 최저가 15 → 10 미만 0일
    assert out["ok"] is False
    assert "죽은 가지" in out["reason"]


def test_a_branch_that_is_always_true_is_rejected():
    out = vtr.validate(_dist(), high=16.0, low=15.5)
    assert out["ok"] is False
    assert "상수" in out["reason"] or "죽은 가지" in out["reason"]


def test_an_inverted_pair_is_rejected():
    out = vtr.validate(_dist(), high=20.0, low=60.0)
    assert out["ok"] is False
    assert "작아야" in out["reason"]


def test_too_small_a_sample_does_not_get_a_verdict():
    out = vtr.validate(_dist(n=50), high=60.0, low=25.0)
    assert out["ok"] is False
    assert "표본" in out["reason"]


def test_a_sane_pair_passes_with_its_coverage():
    out = vtr.validate(_dist(), high=79.0, low=31.0)
    assert out["ok"] is True
    assert 5 <= out["above_pct"] <= 40
    assert 5 <= out["below_pct"] <= 40


def test_a_branch_that_almost_never_fires_is_rejected():
    """드물어도 '걸리기는 한다'는 규칙이 도는 것과 다르다."""
    out = vtr.validate(_dist(), high=93.0, low=31.0)   # 상단 2%대
    assert out["ok"] is False
    assert "드물어" in out["reason"]


# ─── 재측정 판정 ─────────────────────────────────────


def test_broken_current_thresholds_are_called_broken():
    rev = vtr.review(_dist(), current_high=60.0, current_low=10.0)
    assert rev["verdict"] == "broken"
    assert rev["current"]["ok"] is False


def test_a_healthy_pair_close_to_the_distribution_is_kept():
    vals = _dist()
    rev = vtr.review(vals, current_high=79.0, current_low=31.0)
    assert rev["verdict"] == "keep"


def test_a_moved_distribution_produces_a_proposal():
    """아직 안 깨졌지만 분포가 움직였다 — 이때가 '제안'이다."""
    vals = _dist(lo=25.0, hi=110.0)
    rev = vtr.review(vals, current_high=79.0, current_low=31.0)
    assert rev["current"]["ok"] is True     # 아직 죽은 가지는 아니다
    assert rev["verdict"] == "propose"
    assert rev["suggested"]["check"]["ok"] is True
    assert rev["suggested"]["high"] > 79.0


def test_a_distribution_that_ran_away_is_broken_not_merely_proposed():
    """통째로 벗어나면 '조정 권함'이 아니라 '지금 값이 안 돈다'이다."""
    vals = _dist(lo=40.0, hi=180.0)
    rev = vtr.review(vals, current_high=79.0, current_low=31.0)
    assert rev["verdict"] == "broken"
    assert "죽은 가지" in rev["current"]["reason"]


def test_a_tiny_shift_is_not_worth_an_alert():
    """1~2% 움직임은 재측정 잡음이다 — 알리면 소음이 된다."""
    vals = _dist()
    hi = vtr.review(vals, current_high=79.0, current_low=31.0)["suggested"]["high"]
    rev = vtr.review(vals, current_high=hi * 1.02, current_low=31.0)
    assert rev["verdict"] == "keep"


# ─── 원장 ────────────────────────────────────────────


def test_the_ledger_falls_back_to_code_constants_when_empty():
    hi, lo, src = vtr.active_thresholds({"active": None},
                                        fallback_high=60.6, fallback_low=20.7)
    assert (hi, lo) == (60.6, 20.7)
    assert "코드" in src


def test_an_approved_pair_becomes_the_active_one():
    vals = _dist()
    led = vtr.approve({"active": None, "history": []},
                      high=79.0, low=31.0, values=vals, approved_at="2026-09-01")
    hi, lo, src = vtr.active_thresholds(led, fallback_high=60.6, fallback_low=20.7)
    assert (hi, lo) == (79.0, 31.0)
    assert "2026-09-01" in src


def test_approval_refuses_a_dead_branch_even_from_a_human():
    """**버튼은 근거가 아니다.**"""
    with pytest.raises(ValueError, match="죽은 가지"):
        vtr.approve({"active": None, "history": []},
                    high=60.0, low=10.0, values=_dist())


def test_the_history_is_append_only():
    vals = _dist()
    led = vtr.approve({"active": None, "history": []},
                      high=79.0, low=31.0, values=vals, approved_at="2026-09-01")
    led2 = vtr.approve(led, high=80.0, low=32.0, values=vals,
                       approved_at="2026-12-01")
    assert len(led2["history"]) == 2
    assert led2["history"][0]["high"] == 79.0        # 옛 항목이 남아 있다
    assert led2["active"]["previous"]["high"] == 79.0


def test_an_entry_records_enough_to_reproduce_the_decision():
    """몇 달 뒤 '그때 왜 이 값이었나'에 답할 수 있어야 한다."""
    led = vtr.approve({"active": None, "history": []},
                      high=79.0, low=31.0, values=_dist(), approved_at="2026-09-01")
    e = led["active"]
    assert e["n"] >= 120
    assert e["above_pct"] and e["below_pct"]
    assert e["describe"]["median"]


def test_a_corrupt_ledger_does_not_silently_become_empty(tmp_path):
    """조용히 빈 원장이 되면 승인 이력이 사라지고 코드 상수로 되돌아간다."""
    p = tmp_path / "led.json"
    p.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        vtr.load_ledger(p)


def test_a_reversed_ledger_pair_falls_back_instead_of_running(tmp_path):
    hi, lo, src = vtr.active_thresholds(
        {"active": {"high": 20.0, "low": 60.0, "approved_at": "2026-09-01"}},
        fallback_high=60.6, fallback_low=20.7)
    assert (hi, lo) == (60.6, 20.7)
    assert "이상" in src


def test_saving_and_loading_round_trips(tmp_path):
    p = tmp_path / "led.json"
    led = vtr.approve({"active": None, "history": []},
                      high=79.0, low=31.0, values=_dist(), approved_at="2026-09-01")
    vtr.save_ledger(p, led)
    back = vtr.load_ledger(p)
    assert back["active"]["high"] == 79.0
    assert len(back["history"]) == 1


# ─── 주기·재알림 ─────────────────────────────────────


def test_an_empty_ledger_is_due_immediately():
    assert vtr.due_for_review({}, today="2026-09-01") is True


def test_it_is_not_due_again_the_next_day():
    led = {"active": {"approved_at": "2026-09-01"}}
    assert vtr.due_for_review(led, today="2026-09-02") is False


def test_it_is_due_after_a_quarter():
    led = {"active": {"approved_at": "2026-09-01"}}
    assert vtr.due_for_review(led, today="2026-12-01") is True


def test_a_pending_proposal_is_pushed_once_then_renotified():
    """한 번 밀어놓고 잊지 않는다 — 2026-07 리밸런싱이 그렇게 사라졌다."""
    assert vtr.needs_push({"pushes": []}, today="2026-09-01") is True
    p = {"pushes": ["2026-09-01"]}
    assert vtr.needs_push(p, today="2026-09-03") is False
    assert vtr.needs_push(p, today="2026-09-09") is True


def test_renotification_stops_so_it_does_not_become_noise():
    p = {"pushes": ["2026-09-01", "2026-09-08", "2026-09-15"]}
    assert vtr.needs_push(p, today="2026-10-01") is False


def test_no_pending_means_nothing_to_push():
    assert vtr.needs_push(None, today="2026-09-01") is False


# ─── 문구 ────────────────────────────────────────────


def test_the_message_names_why_the_current_value_is_broken():
    rev = vtr.review(_dist(), current_high=60.0, current_low=10.0)
    msg = vtr.format_proposal(rev, current_high=60.0, current_low=10.0)
    assert "죽은 가지" in msg


def test_a_rejected_proposal_says_approval_will_not_apply():
    """제안값이 검증을 못 넘으면 그 사실이 문구에 있어야 한다."""
    vals = _dist()
    rev = vtr.review(vals, current_high=60.0, current_low=10.0)
    rev["suggested"] = {"high": 90.0, "low": 12.0,
                        "check": vtr.validate(vals, 90.0, 12.0)}
    msg = vtr.format_proposal(rev, current_high=60.0, current_low=10.0)
    assert "승인해도 반영되지 않습니다" in msg
