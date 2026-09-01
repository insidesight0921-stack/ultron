"""test_param_store.py — 표본 없이는 못 바꾼다. 승인해도 검증을 통과해야 한다.

2026-09-01 하루에 교과서 상수 다섯 개를 재봤고 결말이 전부 달랐다. 바꿔야 할
값과 두어야 할 값을 가려낸 것은 **분포였지 판단이 아니었다.** 이 창구는 그
규율을 코드로 만든 것이고, 이 파일은 규율이 장식이 되지 않게 고정한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import param_store as ps  # noqa: E402
from param_store import Param  # noqa: E402


def _band(sample):
    return Param(key="t.band", label="테스트 밴드", module="m", attr="A",
                 kind="band_high", sample=lambda: sample)


def _rare(sample):
    return Param(key="t.rare", label="테스트 긴급", module="m", attr="A",
                 kind="rare_high", sample=lambda: sample)


def _two(sample):
    return Param(key="t.two", label="테스트 양쪽", module="m", attr="A",
                 kind="band_high", sample=lambda: sample, two_sided=True)


UNIFORM = [float(i) / 2 for i in range(400)]        # 0~199.5 균등


# ─── 표본이 없으면 못 바꾼다 ─────────────────────────


def test_no_sample_means_no_change():
    """**이게 이 모듈의 존재 이유다.** 표본 없이 통과시키면 도장이 된다."""
    out = ps.validate(_band([]), 50.0, [])
    assert out["ok"] is False and "표본" in out["reason"]


def test_a_thin_sample_is_refused_too():
    out = ps.validate(_band(UNIFORM[:50]), 50.0, UNIFORM[:50])
    assert out["ok"] is False and "표본" in out["reason"]


# ─── 밴드형 ──────────────────────────────────────────


def test_a_threshold_that_never_fires_is_refused():
    out = ps.validate(_band(UNIFORM), 500.0, UNIFORM)
    assert out["ok"] is False and "죽은 가지" in out["reason"]


def test_a_threshold_that_always_fires_is_refused():
    out = ps.validate(_band(UNIFORM), -10.0, UNIFORM)
    assert out["ok"] is False
    assert "상수" in out["reason"] or "상시" in out["reason"]


def test_a_threshold_that_fires_half_the_time_is_refused():
    """계획서 원안이 정확히 이 상태였다 — VKOSPI>30이 48.6%."""
    out = ps.validate(_band(UNIFORM), 100.0, UNIFORM)
    assert out["ok"] is False and "상시 조치" in out["reason"]


def test_a_sane_threshold_passes_with_its_coverage():
    out = ps.validate(_band(UNIFORM), 160.0, UNIFORM)
    assert out["ok"] is True
    assert 5 <= out["coverage"] <= 40


# ─── 드문 사건형은 '진입'으로 센다 ───────────────────


def test_entries_are_counted_not_days():
    """경보는 실측 평균 13.7일 이어진다 — 일수로 세면 판단이 달라진다."""
    # 200일 중 100일이 연속으로 임계 위 = 해당 50%지만 진입은 1회.
    series = [0.0] * 100 + [100.0] * 100
    assert ps.coverage_pct(series, 50.0, above=True) == 50.0
    assert ps.entries_per_year(series, 50.0, above=True) == pytest.approx(1.3, abs=0.1)


def test_a_too_frequent_emergency_is_refused():
    series = [0.0, 100.0] * 100      # 매일 들락날락 = 진입 100회
    out = ps.validate(_rare(series), 50.0, series)
    assert out["ok"] is False and "잦다" in out["reason"]


def test_an_emergency_that_never_enters_is_refused():
    series = [0.0] * 200
    out = ps.validate(_rare(series), 50.0, series)
    assert out["ok"] is False and "죽은 가지" in out["reason"]


def test_a_rare_emergency_passes():
    series = [0.0] * 190 + [100.0] * 10
    out = ps.validate(_rare(series), 50.0, series)
    assert out["ok"] is True and out["per_year"] <= 6


# ─── ±T는 양쪽을 따로 센다 ───────────────────────────


def test_a_two_sided_threshold_is_not_double_counted():
    """절대값으로 한 번에 세면 두 쪽이 합산되어 두 배로 걸리는 것처럼 보인다 —
    2026-09-01에 환율 ±2%가 44.6%로 나와 검증에 걸릴 뻔했다."""
    signed = [float(i) - 200 for i in range(400)]     # -200~199 균등
    out = ps.validate(_two(signed), 150.0, signed)
    assert out["ok"] is True
    assert out["up"] < 20 and out["down"] < 20
    assert out["coverage"] > out["up"]                # 합산값은 참고로만


def test_a_two_sided_threshold_with_one_dead_side_is_refused():
    only_positive = [float(i) for i in range(400)]
    out = ps.validate(_two(only_positive), 100.0, only_positive)
    assert out["ok"] is False and "하단" in out["reason"]


# ─── 원장 ────────────────────────────────────────────


def test_the_ledger_falls_back_to_the_code_default_when_empty():
    v, src = ps.active_value({"active": {}}, _band(UNIFORM), 77.8)
    assert v == 77.8 and "코드" in src


def test_an_approved_value_becomes_active():
    led = ps.approve({"active": {}, "history": []}, _band(UNIFORM), 160.0, UNIFORM,
                     approved_at="2026-09-01")
    v, src = ps.active_value(led, _band(UNIFORM), 77.8)
    assert v == 160.0 and "2026-09-01" in src


def test_approval_refuses_an_invalid_value_even_from_a_human():
    """**버튼은 근거가 아니다.**"""
    with pytest.raises(ValueError, match="죽은 가지"):
        ps.approve({"active": {}, "history": []}, _band(UNIFORM), 500.0, UNIFORM)


def test_the_history_is_append_only():
    led = ps.approve({"active": {}, "history": []}, _band(UNIFORM), 160.0, UNIFORM,
                     approved_at="2026-09-01")
    led2 = ps.approve(led, _band(UNIFORM), 170.0, UNIFORM, approved_at="2026-12-01")
    assert len(led2["history"]) == 2
    assert led2["active"]["t.band"]["previous"] == 160.0


def test_a_corrupt_ledger_does_not_silently_become_empty(tmp_path):
    p = tmp_path / "params.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        ps.load(p)


def test_saving_and_loading_round_trips(tmp_path):
    p = tmp_path / "params.json"
    led = ps.approve({"active": {}, "history": []}, _band(UNIFORM), 160.0, UNIFORM,
                     approved_at="2026-09-01")
    ps.save(p, led)
    assert ps.load(p)["active"]["t.band"]["value"] == 160.0


# ─── 표시 ────────────────────────────────────────────


def test_describe_says_when_it_cannot_be_changed():
    msg = ps.describe(_band([]), 50.0, "코드 초기값", [])
    assert "바꿀 수 없다" in msg
