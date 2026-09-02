"""test_cli_migration.py — 죽은 시리즈를 갈아 끼우기 전에 같은 뜻인지 잰다.

2026-09-01: 국면 판정에 쓰던 OECD CLI(Normalised)가 2024-01에서 멈췄고,
같은 CLI의 Amplitude adjusted 계열은 살아 있다. **값이 비슷해 보이는 것과
같은 뜻인 것은 다르다** — 오늘 교과서 임계값 다섯 개를 재면서 배운 것이다.

이 파일이 지키는 것: 합격선은 **검증 전에** 정해져 있고, 결과를 보고
낮출 수 없다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cli_migration as cm  # noqa: E402


def _months(n, start_y=2000):
    out = []
    y, m = start_y, 1
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


# ─── 정렬 ────────────────────────────────────────────


def test_only_overlapping_months_are_compared():
    """겹치지 않는 달을 채워 넣으면 일치율이 조용히 부풀려진다."""
    a = [("2024-01", 100.0), ("2024-02", 101.0)]
    b = [("2024-02", 100.5), ("2024-03", 99.0)]
    months, va, vb = cm._aligned(a, b)
    assert months == ["2024-02"]


def test_a_thin_overlap_refuses_to_judge():
    a = [(m, 100.0) for m in _months(12)]
    out = cm.compare_levels(a, a)
    assert out["usable"] is False and "24개월" in out["reason"]


# ─── 기준선 판정 ─────────────────────────────────────


def test_identical_series_agree_perfectly():
    s = [(m, 100.0 + (i % 7) - 3) for i, m in enumerate(_months(60))]
    out = cm.compare_levels(s, s)
    assert out["agree_pct"] == 100.0
    assert out["kappa"] == 1.0


def test_opposite_series_have_negative_kappa():
    ms = _months(60)
    a = [(m, 101.0 if i % 2 else 99.0) for i, m in enumerate(ms)]
    b = [(m, 99.0 if i % 2 else 101.0) for i, m in enumerate(ms)]
    out = cm.compare_levels(a, b)
    assert out["kappa"] < 0


def test_agreement_comes_with_its_chance_baseline():
    """일치율은 혼자서는 뜻이 없다 — VIX·VKOSPI에서 배운 것과 같다."""
    ms = _months(60)
    a = [(m, 101.0) for m in ms]          # 늘 위
    b = [(m, 101.0) for m in ms]
    out = cm.compare_levels(a, b)
    assert out["agree_pct"] == 100.0
    assert out["expected_pct"] == 100.0   # 우연 기대도 100 — 정보가 없다
    assert out["kappa"] is None or out["kappa"] == 0


# ─── 교차 시점 ───────────────────────────────────────


def test_crossings_are_found():
    s = [("2024-01", 99.0), ("2024-02", 101.0), ("2024-03", 101.5),
         ("2024-04", 98.0)]
    assert cm.crossings(s) == ["2024-02", "2024-04"]


def test_a_flat_series_has_no_crossings():
    assert cm.crossings([(m, 101.0) for m in _months(24)]) == []


def test_shifted_crossings_are_matched_within_tolerance():
    ms = _months(60)
    a = [(m, 99.0 if i < 30 else 101.0) for i, m in enumerate(ms)]
    b = [(m, 99.0 if i < 31 else 101.0) for i, m in enumerate(ms)]   # 1개월 밀림
    out = cm.compare_crossings(a, b)
    assert out["matched"] == 1
    assert out["mean_gap"] <= cm.CROSS_TOLERANCE_MONTHS


def test_far_apart_crossings_are_not_matched():
    ms = _months(60)
    a = [(m, 99.0 if i < 10 else 101.0) for i, m in enumerate(ms)]
    b = [(m, 99.0 if i < 40 else 101.0) for i, m in enumerate(ms)]   # 30개월 밀림
    out = cm.compare_crossings(a, b)
    assert out["matched"] == 0


# ─── 판정 ────────────────────────────────────────────


def test_a_strong_match_passes():
    ms = _months(120)
    a = [(m, 100.0 + (i % 20) - 10) for i, m in enumerate(ms)]
    out = cm.verdict(cm.compare_levels(a, a), cm.compare_crossings(a, a))
    assert out["ok"] is True


def test_a_weak_kappa_is_refused_with_the_reason():
    ms = _months(120)
    a = [(m, 101.0 if i % 2 else 99.0) for i, m in enumerate(ms)]
    b = [(m, 101.0 if i % 3 else 99.0) for i, m in enumerate(ms)]
    v = cm.verdict(cm.compare_levels(a, b), cm.compare_crossings(a, b))
    assert v["ok"] is False
    assert "임계값을 새 계열의 분포에서 다시" in v["reason"]


def test_the_pass_mark_is_fixed_before_the_test():
    """**결과를 보고 기준을 낮추면 그건 검증이 아니라 승인이다.**"""
    assert cm.KAPPA_PASS == 0.6
    assert cm.CROSS_TOLERANCE_MONTHS == 2


def test_the_report_states_the_pass_mark_was_preset():
    ms = _months(60)
    a = [(m, 100.0 + (i % 10) - 5) for i, m in enumerate(ms)]
    msg = cm.format_report(cm.compare_levels(a, a), cm.compare_crossings(a, a),
                           old_name="CLI_KR", new_name="CLI_KR_AA")
    assert "검증 전에 정했습니다" in msg
    assert "승인입니다" in msg


# ─── 모멘텀 부호 (4분면의 나머지 축) ────────────────
#
# level만 대조하고 교체하면 4분면 중 절반만 검증한 것이다.
# classify_phase는 `level >= 100`과 `momentum >= 0` 둘을 쓴다.


def test_identical_series_have_identical_momentum_signs():
    s = [(m, 100.0 + i * 0.3) for i, m in enumerate(_months(60))]
    out = cm.compare_momentum(s, s)
    assert out["usable"] is True
    assert out["agree_pct"] == 100.0


def test_a_scaled_series_keeps_the_same_momentum_sign():
    """진폭이 달라도 **부호**는 같아야 한다 — 분류에 쓰이는 건 부호뿐이다."""
    ms = _months(60)
    a = [(m, 100.0 + i * 0.2) for i, m in enumerate(ms)]
    b = [(m, 100.0 + i * 0.8) for i, m in enumerate(ms)]   # 진폭 4배
    out = cm.compare_momentum(a, b)
    assert out["agree_pct"] == 100.0


def test_an_inverted_series_flips_the_momentum_sign():
    ms = _months(60)
    a = [(m, 100.0 + i * 0.3) for i, m in enumerate(ms)]
    b = [(m, 100.0 - i * 0.3) for i, m in enumerate(ms)]
    out = cm.compare_momentum(a, b)
    assert out["agree_pct"] == 0.0


def test_a_thin_overlap_yields_no_momentum_verdict():
    s = [(m, 100.0) for m in _months(20)]
    assert cm.compare_momentum(s, s)["usable"] is False


def test_the_verdict_refuses_when_momentum_signs_diverge():
    """level이 완벽해도 모멘텀이 갈리면 교체하면 안 된다."""
    ms = _months(120)
    level_ok = cm.compare_levels([(m, 101.0 if i % 20 < 10 else 99.0)
                                  for i, m in enumerate(ms)],
                                 [(m, 101.0 if i % 20 < 10 else 99.0)
                                  for i, m in enumerate(ms)])
    cross_ok = {"usable": True, "match_pct": 100.0}
    bad_momentum = {"usable": True, "kappa": 0.1, "agree_pct": 55.0,
                    "n": 100, "agree": 55, "expected_pct": 50.0}
    v = cm.verdict(level_ok, cross_ok, bad_momentum)
    assert v["ok"] is False
    assert "모멘텀 부호가 갈린다" in v["reason"]


def test_the_verdict_passes_when_all_three_agree():
    ms = _months(120)
    s = [(m, 100.0 + (i % 20) - 10) for i, m in enumerate(ms)]
    v = cm.verdict(cm.compare_levels(s, s), cm.compare_crossings(s, s),
                   cm.compare_momentum(s, s))
    assert v["ok"] is True
    assert "모멘텀 부호" in v["reason"]
