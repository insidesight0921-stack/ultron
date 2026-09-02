"""test_phase_history.py — 과거 국면을 재구성하되 **미래를 보지 않는다**.

탭 C(국면 판단 정확도)는 "국면 캐시가 1개월치라 측정 대상이 없음"으로 막혀
있었다. 그런데 국면은 순수 함수로 계산되고 원자료는 과거 조회가 된다 —
캐시가 쌓이길 기다릴 이유가 없었다(2026-09-01).

이 파일이 지키는 것은 하나다: **t월 판정에 t월 이후 값이 섞이면 안 된다.**
섞이면 정확도가 가짜로 올라가고, 그 숫자는 검증이 아니라 자기 확인이 된다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import phase_history as ph  # noqa: E402


def _series(n, start=95.0, step=0.5, prefix="20"):
    """n개월 월간 시계열. 라벨은 YYYY-MM."""
    out = []
    y, m = 2018, 1
    for i in range(n):
        out.append((f"{y}-{m:02d}", start + i * step))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


# ─── 미래 미참조 (핵심) ──────────────────────────────


def test_changing_the_future_does_not_change_past_verdicts():
    """**음성 대조.** 미래만 바꾼 두 판에서 과거 판정이 같아야 한다."""
    base = _series(30)
    up = base[:20] + [(d, 200.0) for d, _ in base[20:]]
    down = base[:20] + [(d, 10.0) for d, _ in base[20:]]
    h_up = {r["month"]: r["consensus"] for r in ph.rebuild(up, up, up)}
    h_down = {r["month"]: r["consensus"] for r in ph.rebuild(down, down, down)}
    shared = [m for m in h_up if m <= base[19][0]]
    assert shared, "비교할 과거 구간이 없다"
    for m in shared:
        assert h_up[m] == h_down[m], f"{m}에서 미래가 과거를 바꿨다"


def test_a_verdict_uses_only_months_up_to_itself():
    """phase_at은 길이로 자른다 — 그 뒤 값이 결과에 영향을 주면 안 된다."""
    s = _series(24)
    a = ph.phase_at(s, s, s, upto=20)
    tampered = s[:20] + [(d, 999.0) for d, _ in s[20:]]
    b = ph.phase_at(tampered, tampered, tampered, upto=20)
    assert a == b


def test_other_series_are_cut_by_month_not_by_length():
    """길이로 자르면 발표 시차가 있는 계열에서 달이 밀려 미래가 섞인다."""
    kr = _series(24)
    us = _series(30)                      # 더 길다
    hist = ph.rebuild(kr, us, kr)
    assert hist
    last_month = hist[-1]["month"]
    assert last_month == kr[-1][0]        # 기준은 항상 CLI_KR


# ─── 최소 표본 ───────────────────────────────────────


def test_a_short_series_yields_no_history():
    """모멘텀은 최근 6 + 직전 12개월이 필요하다 — 그 전엔 국면이 없다."""
    assert ph.rebuild(_series(17), _series(17), _series(17)) == []


def test_the_first_verdict_appears_at_the_minimum_length():
    hist = ph.rebuild(_series(18), _series(18), _series(18))
    assert len(hist) == 1
    assert hist[0]["month"] == _series(18)[-1][0]


def test_the_history_length_follows_the_series():
    hist = ph.rebuild(_series(40), _series(40), _series(40))
    assert len(hist) == 40 - ph.MIN_MONTHS + 1


# ─── 전환·요약 ───────────────────────────────────────


def test_transitions_are_found():
    hist = [{"month": "2024-01", "consensus": "Expansion"},
            {"month": "2024-02", "consensus": "Expansion"},
            {"month": "2024-03", "consensus": "Slowdown"},
            {"month": "2024-04", "consensus": "Slowdown"},
            {"month": "2024-05", "consensus": "Recovery"}]
    flips = ph.transitions(hist)
    assert [f["month"] for f in flips] == ["2024-03", "2024-05"]
    assert flips[0]["from"] == "Expansion" and flips[0]["to"] == "Slowdown"


def test_missing_verdicts_do_not_count_as_transitions():
    """None은 '판정 안 함'이지 새 국면이 아니다."""
    hist = [{"month": "2024-01", "consensus": "Expansion"},
            {"month": "2024-02", "consensus": None},
            {"month": "2024-03", "consensus": "Expansion"}]
    assert ph.transitions(hist) == []


def test_a_single_phase_history_is_not_usable():
    """**한 국면뿐이면 정확도를 잴 수 없다** — 나우캐스팅에서 배운 것과 같다."""
    hist = [{"month": f"2024-{m:02d}", "consensus": "Expansion"}
            for m in range(1, 13)] * 3
    d = ph.describe(hist)
    assert d["distinct"] == 1
    assert d["usable"] is False


def test_a_varied_long_history_is_usable():
    hist = ([{"month": f"2023-{m:02d}", "consensus": "Expansion"} for m in range(1, 13)]
            + [{"month": f"2024-{m:02d}", "consensus": "Slowdown"} for m in range(1, 13)])
    d = ph.describe(hist)
    assert d["distinct"] == 2 and d["n"] >= ph.MIN_HISTORY
    assert d["usable"] is True


# ─── 보고 문구 ───────────────────────────────────────


def test_the_report_says_to_collect_more_rather_than_wait():
    hist = [{"month": f"2024-{m:02d}", "consensus": "Expansion"} for m in range(1, 13)]
    msg = ph.format_report(hist)
    assert "기다릴 필요 없습니다" in msg


def test_the_report_states_the_no_lookahead_property():
    hist = [{"month": "2024-01", "consensus": "Expansion"}]
    msg = ph.format_report(hist)
    assert "미래 미참조" in msg


def test_the_report_does_not_claim_the_bot_actually_judged_that_way():
    """'그때 봇이 그렇게 판단했다'와 '지금 규칙으로 그때를 보면 이렇다'는 다르다."""
    msg = ph.format_report([{"month": "2024-01", "consensus": "Expansion"}])
    assert "지금 규칙으로" in msg


def test_an_empty_history_explains_what_is_missing():
    msg = ph.format_report([])
    assert "18" in msg or str(ph.MIN_MONTHS) in msg


# ─── 저장은 병합 ─────────────────────────────────────


def test_saving_merges_instead_of_overwriting(tmp_path):
    """어제 캐시 덮어쓰기 사고와 같은 유형을 여기서도 막는다."""
    p = tmp_path / "phase.json"
    ph.save([{"month": "2024-01", "consensus": "Expansion"}], path=p)
    ph.save([{"month": "2024-02", "consensus": "Slowdown"}], path=p)
    import json
    rows = json.loads(p.read_text(encoding="utf-8"))
    assert [r["month"] for r in rows] == ["2024-01", "2024-02"]


def test_saving_the_same_month_takes_the_newer_row(tmp_path):
    p = tmp_path / "phase.json"
    ph.save([{"month": "2024-01", "consensus": "Expansion"}], path=p)
    ph.save([{"month": "2024-01", "consensus": "Slowdown"}], path=p)
    import json
    rows = json.loads(p.read_text(encoding="utf-8"))
    assert len(rows) == 1 and rows[0]["consensus"] == "Slowdown"
