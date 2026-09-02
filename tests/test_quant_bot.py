"""콴텍봇 v3.22 단위 테스트 (monkeypatch 기반).

외부 HTTP는 _fetch_ecos_series_raw / _fetch_fred_series_raw에서 분리되어
patch 가능. ECOS_API_KEY/FRED_API_KEY 미설정 환경에서도 동작.
"""
from __future__ import annotations

import time

import pytest

import quant_bot as qb


# ─── compute_momentum ───────────────────────────


def _series(values: list[float], year: int | None = None) -> list[tuple[str, float]]:
    """월간 더미 시계열 — **마지막 시점이 이번 달이 되도록** 역산한다.

    v3.65에서 `snapshot()`에 신선도 검사가 붙었다. 고정 연도(2024)로 만들면
    시간이 지날수록 더미가 낡아 국면이 None이 된다 — 실제로 2026-09에
    그렇게 깨졌고, 그건 검사가 일한 것이지 테스트가 맞는 게 아니었다.
    신선도 자체는 별도 테스트에서 본다.
    """
    from datetime import datetime

    out = []
    if year is None:
        now = datetime.now()
        span = max(len(values) - 1, 0)
        y = now.year - (span + (12 - now.month)) // 12
        m = (now.month - span - 1) % 12 + 1
    else:
        y, m = year, 1
    for v in values:
        out.append((f"{y:04d}{m:02d}", float(v)))
        m += 1
        if m > 12:
            y += 1
            m = 1
    return out


def test_compute_momentum_positive():
    """최근 6개월 평균 > 직전 12개월 평균 → 양수 모멘텀."""
    # 18 기준값 + 6 신규: 직전 12개월 평균 ~9, 최근 6개월 평균 19.5
    values = list(range(1, 25))  # 1..24, 길이 24, recent_n+baseline_n=18
    series = _series([float(v) for v in values])
    m = qb.compute_momentum(series)
    assert m is not None and m > 0


def test_compute_momentum_negative():
    """감속 시계열 → 음수 모멘텀."""
    values = list(range(24, 0, -1))
    series = _series([float(v) for v in values])
    m = qb.compute_momentum(series)
    assert m is not None and m < 0


def test_compute_momentum_data_shortage_returns_none():
    """recent_n(6) + baseline_n(12) = 18 미만이면 None."""
    series = _series([100.0] * 17)
    assert qb.compute_momentum(series) is None


def test_compute_momentum_empty_series():
    assert qb.compute_momentum([]) is None


def test_compute_momentum_exact_boundary():
    """정확히 18개월이면 계산 가능."""
    series = _series([float(i) for i in range(1, 19)])
    m = qb.compute_momentum(series)
    assert m is not None


def test_compute_momentum_custom_windows():
    series = _series([float(i) for i in range(1, 11)])
    m = qb.compute_momentum(series, recent_n=3, baseline_n=6)
    assert m is not None and m > 0


# ─── compute_level ──────────────────────────────


def test_compute_level_returns_last_value():
    series = _series([100.0, 101.0, 102.5])
    assert qb.compute_level(series) == 102.5


def test_compute_level_empty_returns_none():
    assert qb.compute_level([]) is None


def test_compute_level_single():
    assert qb.compute_level([("202401", 99.9)]) == 99.9


# ─── classify_phase ─────────────────────────────


def test_classify_phase_recovery():
    """level<100, momentum>0 → Recovery."""
    assert qb.classify_phase(98.5, 0.4) == "Recovery"


def test_classify_phase_expansion():
    """level>=100, momentum>0 → Expansion."""
    assert qb.classify_phase(101.2, 0.3) == "Expansion"


def test_classify_phase_slowdown():
    """level>=100, momentum<0 → Slowdown."""
    assert qb.classify_phase(102.0, -0.2) == "Slowdown"


def test_classify_phase_contraction():
    """level<100, momentum<0 → Contraction."""
    assert qb.classify_phase(95.0, -0.5) == "Contraction"


def test_classify_phase_level_at_threshold_treated_as_above():
    """level=100 경계는 >= 처리 → Expansion."""
    assert qb.classify_phase(100.0, 0.1) == "Expansion"


def test_classify_phase_returns_none_when_inputs_missing():
    assert qb.classify_phase(None, 0.5) is None
    assert qb.classify_phase(99.0, None) is None


def test_classify_phase_custom_threshold():
    """다른 임계값으로 재분류."""
    assert qb.classify_phase(99.5, 0.1, level_threshold=99.0) == "Expansion"


# ─── compute_confidence ─────────────────────────


def test_confidence_kr_us_match_bsi_aligned():
    """한국·미국 같은 phase + BSI 가속 일치 → 1.0."""
    cons, conf = qb.compute_confidence("Expansion", "Expansion", bsi_trend=0.5)
    assert cons == "Expansion" and conf == pytest.approx(1.0, abs=0.01)


def test_confidence_kr_us_match_bsi_misaligned():
    """한국·미국 같지만 BSI 반대 → 0.8."""
    cons, conf = qb.compute_confidence("Expansion", "Expansion", bsi_trend=-0.3)
    assert cons == "Expansion" and conf == pytest.approx(0.8, abs=0.01)


def test_confidence_kr_us_disagree_picks_higher_weight():
    """한국·미국 다른 phase면 우세한 phase 선택. 동점이면 한국 우선."""
    # weights default 0.4/0.4 → 동점, primary는 한국. BSI bonus만 추가.
    cons, conf = qb.compute_confidence("Recovery", "Expansion", bsi_trend=0.5)
    # primary = "Recovery" (kr 먼저 누적), accel match → +0.2 → Recovery=0.6
    # Expansion=0.4. Top1 = Recovery.
    assert cons == "Recovery"
    assert conf == pytest.approx(0.6, abs=0.01)


def test_confidence_kr_only():
    """한국만 있음, 미국 None."""
    cons, conf = qb.compute_confidence("Recovery", None, bsi_trend=0.5)
    assert cons == "Recovery"
    assert conf == pytest.approx(0.6, abs=0.01)


def test_confidence_both_none():
    cons, conf = qb.compute_confidence(None, None, bsi_trend=0.0)
    assert cons is None and conf == 0.0


def test_confidence_no_bsi():
    cons, conf = qb.compute_confidence("Expansion", "Expansion", bsi_trend=None)
    assert cons == "Expansion" and conf == pytest.approx(0.8, abs=0.01)


# ─── ECOS parsing & fetch ──────────────────────


def test_parse_ecos_series_normal():
    payload = {"StatisticSearch": {"row": [
        {"TIME": "202403", "DATA_VALUE": "98.5"},
        {"TIME": "202401", "DATA_VALUE": "97.0"},
        {"TIME": "202402", "DATA_VALUE": "97.8"},
    ]}}
    out = qb._parse_ecos_series(payload)
    assert out == [("202401", 97.0), ("202402", 97.8), ("202403", 98.5)]


def test_parse_ecos_series_empty_payload():
    assert qb._parse_ecos_series({}) == []
    assert qb._parse_ecos_series({"StatisticSearch": {"row": []}}) == []


def test_parse_ecos_series_skips_invalid_value():
    payload = {"StatisticSearch": {"row": [
        {"TIME": "202401", "DATA_VALUE": "97.0"},
        {"TIME": "202402", "DATA_VALUE": "-"},  # 무효
        {"TIME": "202403", "DATA_VALUE": "98.5"},
    ]}}
    out = qb._parse_ecos_series(payload)
    assert len(out) == 2


def test_fetch_ecos_raw_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ECOS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ECOS_API_KEY"):
        qb._fetch_ecos_series_raw("901Y027", "I36BC", "M", 24)


def test_fetch_ecos_raw_unsupported_cycle_raises(monkeypatch):
    monkeypatch.setenv("ECOS_API_KEY", "test_key")
    with pytest.raises(ValueError, match="cycle"):
        qb._fetch_ecos_series_raw("901Y027", "I36BC", "Q", 24)


# ─── FRED parsing & fetch ──────────────────────


def test_parse_fred_series_normal():
    payload = {"observations": [
        {"date": "2024-03-01", "value": "101.5"},
        {"date": "2024-01-01", "value": "100.0"},
        {"date": "2024-02-01", "value": "100.8"},
    ]}
    out = qb._parse_fred_series(payload)
    assert out == [("2024-01-01", 100.0), ("2024-02-01", 100.8), ("2024-03-01", 101.5)]


def test_parse_fred_series_skips_dot_value():
    """FRED는 결측을 '.'로 표기."""
    payload = {"observations": [
        {"date": "2024-01-01", "value": "."},
        {"date": "2024-02-01", "value": "100.0"},
    ]}
    out = qb._parse_fred_series(payload)
    assert out == [("2024-02-01", 100.0)]


def test_parse_fred_series_empty():
    assert qb._parse_fred_series({}) == []


def test_fetch_fred_raw_missing_key_raises(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="FRED_API_KEY"):
        qb._fetch_fred_series_raw("USALOLITONOSTSAM", 24)


# ─── fetch_series (캐시 + 빈 응답 가드) ───────


def test_fetch_series_caches_normal_response(monkeypatch):
    """v3.23.1 — CLI_KR이 FRED로 이동. CLI 시리즈는 FRED 가짜 응답으로 테스트."""
    qb.clear_cache()
    calls = {"n": 0}
    def fake_raw(series_id, months):
        calls["n"] += 1
        return {"observations": [
            {"date": "2024-01-01", "value": "98.0"},
        ]}
    monkeypatch.setattr(qb, "_fetch_fred_series_raw", fake_raw)

    s1 = qb.fetch_series("CLI_KR", months=24)
    s2 = qb.fetch_series("CLI_KR", months=24)
    assert s1 == s2
    assert calls["n"] == 1, "캐시 hit으로 두 번째 호출은 fetch 안 함"


def test_fetch_series_bsi_kr_via_ecos(monkeypatch):
    """v3.23.1 — BSI_KR은 여전히 ECOS. 캐시 + 정상 응답 sanity."""
    qb.clear_cache()
    calls = {"n": 0}
    def fake_raw(stat, item, cycle, months):
        calls["n"] += 1
        # item_code는 v3.23.1에서 99988 (전산업)
        assert item == "99988", f"BSI_KR item_code 갱신 안 됨: {item}"
        return {"StatisticSearch": {"row": [
            {"TIME": "202401", "DATA_VALUE": "85.0"},
        ]}}
    monkeypatch.setattr(qb, "_fetch_ecos_series_raw", fake_raw)
    s = qb.fetch_series("BSI_KR", months=24)
    assert s == [("202401", 85.0)]


def test_fetch_series_empty_response_skips_cache(monkeypatch):
    """v3.21 패턴 — 빈 응답은 캐시하지 않고 다음 호출에서 다시 fetch.

    v3.23.1 — CLI_KR이 FRED로 이동했으니 FRED monkeypatch.
    """
    qb.clear_cache()
    calls = {"n": 0}
    def fake_raw(series_id, months):
        calls["n"] += 1
        return {"observations": []}
    monkeypatch.setattr(qb, "_fetch_fred_series_raw", fake_raw)

    s1 = qb.fetch_series("CLI_KR", months=24)
    s2 = qb.fetch_series("CLI_KR", months=24)
    assert s1 == [] and s2 == []
    assert calls["n"] == 2, "빈 응답 캐시 hit으로 fetch가 skip되면 안 됨"


def test_fetch_series_unknown_name_raises(monkeypatch):
    qb.clear_cache()
    with pytest.raises(ValueError, match="알 수 없는 시리즈"):
        qb.fetch_series("UNKNOWN", months=24)


def test_clear_cache_empties_cache(monkeypatch):
    """v3.23.1 — CLI_KR FRED 이동 반영."""
    qb.clear_cache()
    monkeypatch.setattr(qb, "_fetch_fred_series_raw",
                        lambda *a, **k: {"observations": [
                            {"date": "2024-01-01", "value": "98.0"}]})
    qb.fetch_series("CLI_KR", months=24)
    assert len(qb._SERIES_CACHE) == 1
    qb.clear_cache()
    assert len(qb._SERIES_CACHE) == 0


# ─── snapshot 통합 ───────────────────────────


def test_snapshot_all_signals_normal(monkeypatch):
    """모든 fetch 정상 → consensus_phase 결정."""
    qb.clear_cache()

    cli_kr_vals = [98.0 + i * 0.05 for i in range(24)]   # 가속 — Recovery 영역
    cli_us_vals = [101.0 + i * 0.05 for i in range(24)]  # 가속 — Expansion 영역
    bsi_vals = [50.0 + i * 0.5 for i in range(24)]       # 양수 추세

    def fake_fetch(name, months=24):
        if name == qb.ACTIVE_CLI["KR"]:
            return _series(cli_kr_vals)
        if name == qb.ACTIVE_CLI["US"]:
            return _series(cli_us_vals)
        if name == "BSI_KR":
            return _series(bsi_vals)
        return []
    monkeypatch.setattr(qb, "fetch_series", fake_fetch)

    snap = qb.snapshot()
    assert snap.phase_kr == "Recovery"
    assert snap.phase_us == "Expansion"
    assert snap.consensus_phase in ("Recovery", "Expansion")
    assert 0.0 < snap.confidence <= 1.0
    assert snap.cli_kr_level is not None
    assert snap.bsi_trend is not None and snap.bsi_trend > 0


def test_snapshot_partial_fetch_failure_graceful(monkeypatch):
    """일부 fetch 실패해도 나머지로 graceful degrade."""
    qb.clear_cache()

    def fake_fetch(name, months=24):
        if name == qb.ACTIVE_CLI["KR"]:
            raise RuntimeError("ECOS down")
        if name == qb.ACTIVE_CLI["US"]:
            return _series([101.0 + i * 0.05 for i in range(24)])
        return []
    monkeypatch.setattr(qb, "fetch_series", fake_fetch)

    snap = qb.snapshot()
    assert snap.phase_kr is None
    assert snap.phase_us == "Expansion"
    assert snap.consensus_phase == "Expansion"


def test_snapshot_all_fetch_failure(monkeypatch):
    """모든 fetch 실패 → consensus None, 모든 신호 None."""
    qb.clear_cache()
    monkeypatch.setattr(qb, "fetch_series",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("all down")))

    snap = qb.snapshot()
    assert snap.consensus_phase is None
    assert snap.confidence == 0.0
    assert snap.phase_kr is None and snap.phase_us is None


def test_snapshot_low_confidence_triggers_recheck(monkeypatch):
    """신호 1개만 + BSI 없으면 confidence < 0.6 → needs_recheck True."""
    qb.clear_cache()
    def fake_fetch(name, months=24):
        if name == qb.ACTIVE_CLI["KR"]:
            return _series([98.0 + i * 0.05 for i in range(24)])
        return []  # us, bsi 없음
    monkeypatch.setattr(qb, "fetch_series", fake_fetch)

    snap = qb.snapshot()
    assert snap.consensus_phase == "Recovery"
    assert snap.confidence < 0.6
    assert snap.needs_recheck is True


# ─── format_snapshot ─────────────────────────


def test_format_snapshot_normal():
    snap = qb.PhaseSnapshot(
        phase_kr="Recovery", phase_us="Expansion",
        cli_kr_level=98.5, cli_kr_momentum=0.42,
        cli_us_level=101.2, cli_us_momentum=0.31,
        bsi_trend=0.15,
        consensus_phase="Expansion", confidence=0.8,
        needs_recheck=False,
    )
    text = qb.format_snapshot(snap)
    assert "콴텍봇 거시 국면 스냅샷" in text
    assert "Expansion" in text
    assert "확신도 80%" in text
    assert "한국 CLI" in text
    assert "98.50" in text
    assert "v3.22" in text


def test_format_snapshot_no_data():
    snap = qb.PhaseSnapshot(
        phase_kr=None, phase_us=None,
        cli_kr_level=None, cli_kr_momentum=None,
        cli_us_level=None, cli_us_momentum=None,
        bsi_trend=None,
        consensus_phase=None, confidence=0.0,
        needs_recheck=False,
    )
    text = qb.format_snapshot(snap)
    assert "데이터 부족" in text or "판단 불가" in text


def test_format_snapshot_low_confidence_warning():
    snap = qb.PhaseSnapshot(
        phase_kr="Recovery", phase_us=None,
        cli_kr_level=98.5, cli_kr_momentum=0.42,
        cli_us_level=None, cli_us_momentum=None,
        bsi_trend=None,
        consensus_phase="Recovery", confidence=0.4,
        needs_recheck=True,
    )
    text = qb.format_snapshot(snap)
    assert "재진단" in text


# ─── run() entrypoint ───────────────────────


def test_run_phase_action_returns_text(monkeypatch):
    qb.clear_cache()
    def fake_fetch(name, months=24):
        if name == qb.ACTIVE_CLI["KR"]:
            return _series([98.0 + i * 0.05 for i in range(24)])
        if name == qb.ACTIVE_CLI["US"]:
            return _series([101.0 + i * 0.05 for i in range(24)])
        return []
    monkeypatch.setattr(qb, "fetch_series", fake_fetch)

    text, sources = qb.run("phase")
    assert "콴텍봇" in text
    assert sources == []


def test_run_unknown_action_returns_error():
    text, sources = qb.run("unknown_action")
    assert "알 수 없는" in text or "지원" in text
    assert sources == []


def test_run_clamps_months_too_low(monkeypatch):
    """months < 18이면 18로 클램프."""
    qb.clear_cache()
    captured = {"months": None}
    def fake_fetch(name, months=24):
        captured["months"] = months
        return []
    monkeypatch.setattr(qb, "fetch_series", fake_fetch)
    qb.run("phase", months=10)
    assert captured["months"] == 18


def test_run_clamps_months_too_high(monkeypatch):
    """months > 60이면 60으로 클램프."""
    qb.clear_cache()
    captured = {"months": None}
    def fake_fetch(name, months=24):
        captured["months"] = months
        return []
    monkeypatch.setattr(qb, "fetch_series", fake_fetch)
    qb.run("phase", months=120)
    assert captured["months"] == 60


def test_run_handles_snapshot_exception(monkeypatch):
    monkeypatch.setattr(qb, "snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    text, sources = qb.run("phase")
    assert "실패" in text or "boom" in text
    assert sources == []

# ─── 라우터 통합 (v3.22) ─────────────────────


def test_router_known_tools_includes_quant_bot():
    import router
    assert "quant_bot" in router.KNOWN_TOOLS


def test_router_validate_quant_args_phase_minimal():
    import router
    out = router._validate_quant_args({"action": "phase"})
    assert out == {"action": "phase"}


def test_router_validate_quant_args_with_months():
    import router
    out = router._validate_quant_args({"action": "phase", "months": 36})
    assert out == {"action": "phase", "months": 36}


def test_router_validate_quant_args_clamps_months():
    """months가 18 미만이면 18, 60 초과면 60으로 자동 클램프."""
    import router
    assert router._validate_quant_args({"action": "phase", "months": 10})["months"] == 18
    assert router._validate_quant_args({"action": "phase", "months": 100})["months"] == 60


def test_router_validate_quant_args_rejects_unknown_action():
    import router
    assert router._validate_quant_args({"action": "rebalance"}) is None
    assert router._validate_quant_args({"action": ""}) is None



# ═══════════════════════════════════════════════════════════════
# v3.23 — 종목 팩터 스코어링 + 추천
# ═══════════════════════════════════════════════════════════════


import pandas as pd  # noqa: E402


# ─── _normalize_factor_name ─────────────────


def test_normalize_factor_name_english():
    assert qb._normalize_factor_name("Momentum") == "Momentum"
    assert qb._normalize_factor_name("Quality (ROE)") == "Quality"
    assert qb._normalize_factor_name("Low Vol") == "LowVol"


def test_normalize_factor_name_korean():
    assert qb._normalize_factor_name("모멘텀") == "Momentum"
    assert qb._normalize_factor_name("저변동성") == "LowVol"
    assert qb._normalize_factor_name("소형주") == "Size"
    assert qb._normalize_factor_name("가치 (PBR)") == "Value"


def test_normalize_factor_name_unknown():
    assert qb._normalize_factor_name("Growth") is None
    assert qb._normalize_factor_name("") is None


# ─── parse_phase_weights_from_wiki ─────────


def test_parse_phase_weights_from_real_wiki():
    """실제 wiki 노트 파싱 — 4 phase 모두 + 합 1.0."""
    weights = qb.parse_phase_weights_from_wiki()
    for phase in qb.PHASES:
        assert phase in weights, f"{phase} 누락"
        s = sum(weights[phase].values())
        assert abs(s - 1.0) < 1e-6, f"{phase} 합 {s} != 1.0"


def test_parse_phase_weights_falls_back_when_missing(tmp_path):
    """노트 부재 시 fallback 사용."""
    nonexistent = tmp_path / "missing.md"
    weights = qb.parse_phase_weights_from_wiki(nonexistent)
    for phase in qb.PHASES:
        assert phase in weights
        s = sum(weights[phase].values())
        assert abs(s - 1.0) < 1e-6


def test_parse_phase_weights_partial_section(tmp_path):
    """일부 phase만 정의된 노트 → 나머지는 fallback에서."""
    note = tmp_path / "partial.md"
    note.write_text(
        "#### Recovery — test\n\n| 팩터 | 가중 |\n|---|---|\n"
        "| Momentum | 0.5 |\n| Value | 0.5 |\n",
        encoding="utf-8",
    )
    weights = qb.parse_phase_weights_from_wiki(note)
    # Recovery는 파일에서, 나머지는 fallback
    assert "Recovery" in weights
    assert weights["Recovery"]["Momentum"] == pytest.approx(0.5)
    assert weights["Recovery"]["Value"] == pytest.approx(0.5)
    assert "Expansion" in weights  # fallback 보충


def test_parse_phase_weights_fallback_when_no_sections(tmp_path):
    """헤딩이 없으면 전부 fallback."""
    note = tmp_path / "no_heading.md"
    note.write_text("# 그냥 텍스트\n팩터 가중치 없음", encoding="utf-8")
    weights = qb.parse_phase_weights_from_wiki(note)
    for phase in qb.PHASES:
        assert phase in weights


# ─── z_score_normalize ────────────────────


def test_z_score_normal():
    out = qb.z_score_normalize([1, 2, 3, 4, 5])
    assert out[0] < 0 < out[-1]
    assert abs(out[2]) < 1e-9  # 중앙값 근사 0


def test_z_score_preserves_none():
    out = qb.z_score_normalize([None, 1.0, 2.0, None])
    assert out[0] is None and out[3] is None
    assert out[1] < 0 < out[2]


def test_z_score_zero_std():
    """모든 값 같으면 std=0 → 모두 0.0."""
    out = qb.z_score_normalize([5.0, 5.0, 5.0])
    assert out == [0.0, 0.0, 0.0]


def test_z_score_all_none():
    """전부 None → 그대로."""
    out = qb.z_score_normalize([None, None])
    assert out == [None, None]


def test_z_score_single_value():
    out = qb.z_score_normalize([42.0])
    assert out == [0.0]


# ─── combine_factor_scores ────────────────


def test_combine_basic():
    zs = {"Momentum": 1.0, "Value": -0.5, "Quality": 0.0,
          "LowVol": 0.5, "Size": 0.5}
    ws = {"Momentum": 0.4, "Value": 0.3, "Quality": 0.0,
          "LowVol": 0.2, "Size": 0.1}
    # 합산: 1.0*0.4 - 0.5*0.3 + 0 + 0.5*0.2 + 0.5*0.1 = 0.4-0.15+0.1+0.05=0.4
    # 정규화: 사용된 가중 합 0.4+0.3+0.2+0.1 = 1.0 → 0.4/1.0 = 0.4
    out = qb.combine_factor_scores(zs, ws)
    assert out == pytest.approx(0.4, abs=1e-6)


def test_combine_skips_none_factors():
    zs = {"Momentum": 1.0, "Value": None, "Quality": 0.5,
          "LowVol": None, "Size": None}
    ws = {"Momentum": 0.5, "Value": 0.3, "Quality": 0.2,
          "LowVol": 0.0, "Size": 0.0}
    # used = (1.0, 0.5), (0.5, 0.2). total = 0.7. (0.5+0.1)/0.7 = 0.857
    out = qb.combine_factor_scores(zs, ws)
    assert out == pytest.approx(0.857, abs=0.01)


def test_combine_all_none_returns_none():
    zs = {f: None for f in qb.SCORING_FACTORS}
    ws = {f: 0.2 for f in qb.SCORING_FACTORS}
    assert qb.combine_factor_scores(zs, ws) is None


def test_combine_zero_weights_returns_none():
    zs = {f: 1.0 for f in qb.SCORING_FACTORS}
    ws = {f: 0.0 for f in qb.SCORING_FACTORS}
    assert qb.combine_factor_scores(zs, ws) is None


# ─── compute_factor_scores ────────────────


def _fake_close(n=280, factor=1.001):
    return pd.Series([100.0 * (factor ** i) for i in range(n)])


def test_compute_factors_full():
    close = _fake_close()
    fundamental = {"BPS": 50000, "PER": 12.0, "PBR": 1.2,
                   "EPS": 4000, "DPS": 1500, "DIV": 2.5}
    out = qb.compute_factor_scores(close, fundamental, market_cap=1e15)
    assert out["Momentum"] is not None and out["Momentum"] > 0
    assert out["Value"] == pytest.approx(1.0/1.2, abs=1e-6)
    assert out["Quality"] == pytest.approx(4000/50000, abs=1e-6)
    assert out["LowVol"] is not None and out["LowVol"] < 0
    assert out["Size"] is not None and out["Size"] < 0


def test_compute_factors_missing_fundamental():
    close = _fake_close()
    out = qb.compute_factor_scores(close, fundamental=None, market_cap=None)
    assert out["Momentum"] is not None
    assert out["LowVol"] is not None
    assert out["Value"] is None
    assert out["Quality"] is None
    assert out["Size"] is None


def test_compute_factors_zero_pbr_skips_value():
    close = _fake_close()
    f = {"PBR": 0, "BPS": 50000, "EPS": 4000}
    out = qb.compute_factor_scores(close, f, market_cap=1e12)
    assert out["Value"] is None
    assert out["Quality"] is not None  # BPS>0, EPS>0


def test_compute_factors_short_close_returns_none_for_momentum():
    """OHLCV 부족 시 momentum/lowvol None."""
    close = _fake_close(n=50)
    out = qb.compute_factor_scores(close)
    assert out["Momentum"] is None
    assert out["LowVol"] is None  # vol_window=63 미달


# ─── recommend_top_n 통합 ────────────────


def _fake_universe():
    return [
        ("005930", "삼성전자"),
        ("000660", "SK하이닉스"),
        ("035420", "NAVER"),
        ("207940", "삼성바이오로직스"),
    ]


def _fake_fundamentals():
    return {
        "005930": {"BPS": 50000, "PER": 12.0, "PBR": 1.2, "EPS": 4000, "DPS": 0, "DIV": 0},
        "000660": {"BPS": 80000, "PER": 8.0,  "PBR": 0.9, "EPS": 9000, "DPS": 0, "DIV": 0},
        "035420": {"BPS": 100000, "PER": 25.0, "PBR": 2.5, "EPS": 4000, "DPS": 0, "DIV": 0},
        "207940": {"BPS": 30000, "PER": 60.0, "PBR": 6.0, "EPS": 500, "DPS": 0, "DIV": 0},
    }


def _fake_caps():
    return {
        "005930": 4e15, "000660": 8e14, "035420": 3e13, "207940": 5e13,
    }


def test_fundamental_and_cap_adapters_delegate_to_collector(monkeypatch):
    seen = []

    def fake_fundamentals(date, market="KOSPI"):
        seen.append(("fundamental", date, market))
        return _fake_fundamentals()

    def fake_caps(date, market="KOSPI"):
        seen.append(("cap", date, market))
        return _fake_caps()

    monkeypatch.setattr(qb, "fetch_fundamental_data", fake_fundamentals)
    monkeypatch.setattr(qb, "fetch_market_cap_data", fake_caps)
    assert qb.fetch_fundamentals("KOSDAQ") == _fake_fundamentals()
    assert qb.fetch_market_caps("KOSDAQ") == _fake_caps()
    assert [item[0] for item in seen] == ["fundamental", "cap"]
    assert all(item[1].isdigit() and len(item[1]) == 8 for item in seen)
    assert all(item[2] == "KOSDAQ" for item in seen)


def test_fundamental_and_cap_adapters_fail_closed(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(qb, "fetch_fundamental_data", boom)
    monkeypatch.setattr(qb, "fetch_market_cap_data", boom)
    assert qb.fetch_fundamentals() == {}
    assert qb.fetch_market_caps() == {}


def test_fundamental_and_cap_adapters_use_data_api_first(monkeypatch):
    class FakeClient:
        def latest_factors(self, market):
            return {
                "market": market,
                "as_of": "20260821",
                "fundamentals": _fake_fundamentals(),
                "market_caps": _fake_caps(),
            }

    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(qb, "_DATA_API_CLIENT", FakeClient())
    monkeypatch.setattr(
        qb,
        "fetch_fundamental_data",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("collector called")),
    )
    monkeypatch.setattr(
        qb,
        "fetch_market_cap_data",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("collector called")),
    )
    assert qb.fetch_fundamentals("KOSPI") == _fake_fundamentals()
    assert qb.fetch_market_caps("KOSPI") == _fake_caps()


def _stub_ohlcv(monkeypatch):
    """4종목 다른 추세를 전용 collector adapter에 주입."""
    factors = {"005930": 1.002, "000660": 1.001, "035420": 0.999, "207940": 1.003}
    def fake(ticker, start, end):
        f = factors.get(ticker, 1.001)
        return {
            "ticker": ticker,
            "as_of": end,
            "series": {"close": [100.0 * (f ** i) for i in range(280)]},
        }
    monkeypatch.setattr(qb, "_collect_ohlcv", fake)


def test_recommend_returns_top_n(monkeypatch):
    _stub_ohlcv(monkeypatch)
    recs = qb.recommend_top_n(
        phase="Recovery", universe=_fake_universe(), top_n=3,
        fundamentals=_fake_fundamentals(), market_caps=_fake_caps(),
    )
    assert len(recs) <= 3
    assert all(isinstance(r, qb.StockRecommendation) for r in recs)
    # 정렬: 점수 내림차순
    scores = [r.composite_score for r in recs]
    assert scores == sorted(scores, reverse=True)


def test_recommend_phase_weight_changes_ranking(monkeypatch):
    """Recovery vs Slowdown — 가중 다르면 순위도 다를 수 있다."""
    _stub_ohlcv(monkeypatch)
    rec_recovery = qb.recommend_top_n(
        phase="Recovery", universe=_fake_universe(), top_n=4,
        fundamentals=_fake_fundamentals(), market_caps=_fake_caps(),
    )
    rec_slowdown = qb.recommend_top_n(
        phase="Slowdown", universe=_fake_universe(), top_n=4,
        fundamentals=_fake_fundamentals(), market_caps=_fake_caps(),
    )
    # 정확한 순위 비교는 brittle하지만, 점수는 다른 phase에서 다르게 계산됨
    rec_scores = {r.ticker: r.composite_score for r in rec_recovery}
    sl_scores = {r.ticker: r.composite_score for r in rec_slowdown}
    common = set(rec_scores) & set(sl_scores)
    assert any(abs(rec_scores[t] - sl_scores[t]) > 1e-6 for t in common),         "Recovery와 Slowdown 점수가 모두 같음 — 가중 차이가 안 반영"


def test_recommend_unknown_phase_returns_empty():
    recs = qb.recommend_top_n(phase="Sideways", universe=_fake_universe())
    assert recs == []


def test_recommend_empty_universe_returns_empty():
    recs = qb.recommend_top_n(phase="Recovery", universe=[])
    assert recs == []


def test_recommend_default_top_n_per_phase(monkeypatch):
    _stub_ohlcv(monkeypatch)
    # universe가 4종목이라 결과 ≤ 4. 디폴트 top_n: Recovery=8 → 모든 4종목 반환.
    recs = qb.recommend_top_n(
        phase="Recovery", universe=_fake_universe(),
        fundamentals=_fake_fundamentals(), market_caps=_fake_caps(),
    )
    assert len(recs) == 4  # universe 전체 (top_n=8 디폴트지만 4 종목뿐)
    # Contraction은 디폴트 4
    recs2 = qb.recommend_top_n(
        phase="Contraction", universe=_fake_universe(),
        fundamentals=_fake_fundamentals(), market_caps=_fake_caps(),
    )
    assert len(recs2) <= 4


def test_recommend_zero_weights_returns_empty(monkeypatch):
    _stub_ohlcv(monkeypatch)
    bad = {"Recovery": {f: 0.0 for f in qb.SCORING_FACTORS}}
    recs = qb.recommend_top_n(
        phase="Recovery", universe=_fake_universe(), weights=bad,
        fundamentals=_fake_fundamentals(), market_caps=_fake_caps(),
    )
    assert recs == []


# ─── format_recommendations ───────────────


def test_format_recommendations_empty():
    text = qb.format_recommendations("Recovery", [])
    assert "결과 없음" in text


def test_format_recommendations_with_data():
    rec = qb.StockRecommendation(
        ticker="005930", name="삼성전자", composite_score=0.5,
        raw_factors={"Momentum": 0.2}, z_factors={"Momentum": 1.5, "Value": -0.3},
        current_price=80000.0,
    )
    text = qb.format_recommendations(
        "Expansion", [rec], confidence=0.9,
        weights={"Momentum": 0.4, "Value": 0.1, "Quality": 0.2, "LowVol": 0.0, "Size": 0.1},
    )
    assert "Expansion" in text
    assert "삼성전자" in text
    assert "+0.50" in text
    assert "확신도 90%" in text
    assert "v3.23" in text


def test_format_recommendations_contraction_bond_hint():
    text = qb.format_recommendations("Contraction", [])
    assert "채권 ETF" in text or "Contraction" in text


def test_format_recommendations_with_snapshot_text():
    text = qb.format_recommendations(
        "Recovery", [],
        snapshot_text="📊 콴텍봇 거시 국면 스냅샷",
    )
    assert "거시 국면 스냅샷" in text
    assert "Recovery" in text


def test_format_recommendations_recheck_warning():
    rec = qb.StockRecommendation(
        ticker="005930", name="삼성전자", composite_score=0.5,
        raw_factors={}, z_factors={}, current_price=80000,
    )
    text = qb.format_recommendations(
        "Recovery", [rec], confidence=0.4, needs_recheck=True,
    )
    assert "재진단" in text


# ─── run("recommend") 통합 ─────────────


def test_run_recommend_action(monkeypatch):
    """run('recommend') 정상 흐름 — snapshot + recommend 통합 출력."""
    qb.clear_cache()

    def fake_fetch(name, months=24):
        if name == qb.ACTIVE_CLI["KR"]:
            return _series([98.0 + i * 0.05 for i in range(24)])
        if name == qb.ACTIVE_CLI["US"]:
            return _series([101.0 + i * 0.05 for i in range(24)])
        return []
    monkeypatch.setattr(qb, "fetch_series", fake_fetch)

    monkeypatch.setattr(qb, "fetch_fundamentals", lambda market="KOSPI": _fake_fundamentals())
    monkeypatch.setattr(qb, "fetch_market_caps", lambda market="KOSPI": _fake_caps())

    import kium_bot
    monkeypatch.setattr(kium_bot, "fetch_universe",
                        lambda market="KOSPI200", force_refresh=False: _fake_universe())
    _stub_ohlcv(monkeypatch)

    text, sources = qb.run("recommend", top_n=2)
    assert "콴텍봇" in text
    assert "추천" in text or "Top" in text
    assert sources == []


def test_run_recommend_no_phase_returns_error_message(monkeypatch):
    """consensus_phase 미확정 시 추천 불가 메시지."""
    qb.clear_cache()
    monkeypatch.setattr(qb, "fetch_series", lambda *a, **k: [])
    text, _ = qb.run("recommend")
    assert "거시 국면" in text or "미확정" in text or "추천 불가" in text


def test_run_recommend_phase_override(monkeypatch):
    """phase_override 사용 — consensus 무시."""
    qb.clear_cache()
    monkeypatch.setattr(qb, "fetch_series", lambda *a, **k: [])  # consensus 미확정
    monkeypatch.setattr(qb, "fetch_fundamentals", lambda market="KOSPI": _fake_fundamentals())
    monkeypatch.setattr(qb, "fetch_market_caps", lambda market="KOSPI": _fake_caps())
    import kium_bot
    monkeypatch.setattr(kium_bot, "fetch_universe",
                        lambda market="KOSPI200", force_refresh=False: _fake_universe())
    _stub_ohlcv(monkeypatch)

    text, _ = qb.run("recommend", phase_override="Expansion", top_n=2)
    assert "Expansion" in text


# ─── 라우터 통합 (v3.23 recommend) ────────


def test_router_quant_recommend_minimal():
    import router
    out = router._validate_quant_args({"action": "recommend"})
    assert out == {"action": "recommend"}


def test_router_quant_recommend_full_args():
    import router
    out = router._validate_quant_args({
        "action": "recommend", "top_n": 5,
        "market": "KOSDAQ150", "phase_override": "Recovery",
    })
    assert out["action"] == "recommend"
    assert out["top_n"] == 5
    assert out["market"] == "KOSDAQ150"
    assert out["phase_override"] == "Recovery"


def test_router_quant_recommend_top_n_clamp():
    import router
    out = router._validate_quant_args({"action": "recommend", "top_n": 999})
    assert out["top_n"] == 30
    out2 = router._validate_quant_args({"action": "recommend", "top_n": 0})
    assert "top_n" not in out2  # 1 미만은 디폴트


def test_router_quant_recommend_phase_override_case_insensitive():
    import router
    out = router._validate_quant_args({
        "action": "recommend", "phase_override": "expansion",
    })
    assert out["phase_override"] == "Expansion"


def test_router_quant_recommend_unknown_market_dropped():
    import router
    out = router._validate_quant_args({
        "action": "recommend", "market": "NASDAQ",
    })
    assert "market" not in out


def test_router_system_prompt_includes_recommend():
    import router
    sp = router._build_system_prompt()
    assert "recommend" in sp
    assert "phase_override" in sp



# ─── v3.23.1 카탈로그 핫픽스 sanity ─────────


def test_v3231_cli_kr_moved_to_fred():
    """v3.23.1 — CLI_KR이 ECOS에서 FRED로 이동했는지 확인."""
    assert "CLI_KR" not in qb.ECOS_SERIES, "CLI_KR이 ECOS에서 제거되어야 함"
    assert "CLI_KR" in qb.FRED_SERIES, "CLI_KR이 FRED로 이동되어야 함"
    assert qb.FRED_SERIES["CLI_KR"]["series_id"] == "KORLOLITONOSTSAM"


def test_v3231_bsi_kr_item_code_updated():
    """v3.23.1 — BSI_KR item_code AX1AAA → 99988 (전산업)."""
    assert qb.ECOS_SERIES["BSI_KR"]["item_code"] == "99988"


def test_v3231_ecos_series_only_bsi():
    """v3.23.1 — ECOS는 BSI_KR만 남음."""
    assert set(qb.ECOS_SERIES.keys()) == {"BSI_KR"}


def test_v3231_fred_series_both_cli():
    """v3.23.1 — FRED에 한미 CLI 둘 다. v3.65에서 진폭조정 후보가 추가됐다."""
    assert {"CLI_KR", "CLI_US"} <= set(qb.FRED_SERIES.keys())


def test_the_active_cli_is_named_explicitly():
    """**어느 시리즈로 판정하는지가 코드에 드러나야 한다.**

    2026-09-01: 쓰던 시리즈가 2024-01에서 죽었는데 그 사실을 아무도 몰랐다.
    교체 후보를 붙일 때 '지금 쓰는 것'을 이름으로 고정한다.
    """
    assert set(qb.ACTIVE_CLI) == {"KR", "US"}
    for key in qb.ACTIVE_CLI.values():
        assert key in qb.FRED_SERIES


def test_the_active_series_is_the_one_that_is_still_updated():
    """2026-09-01 교체: Normalised 계열은 2024-01에서 멈췄고 Amplitude
    adjusted는 갱신 중이다. 검증(kappa +0.975/+0.990) 통과 후 교체했다.

    **되돌아가면 다시 32개월 낡은 값으로 판정하게 된다.**
    """
    assert "CLI_KR_AA" in qb.FRED_SERIES and "CLI_US_AA" in qb.FRED_SERIES
    assert qb.ACTIVE_CLI["KR"] == "CLI_KR_AA"
    assert qb.ACTIVE_CLI["US"] == "CLI_US_AA"
    for key in qb.ACTIVE_CLI.values():
        assert qb.FRED_SERIES[key]["series_id"].endswith("AASTSAM"), (
            f"{key}가 멈춘 Normalised 계열로 되돌아갔다")



# ═══════════════════════════════════════════════════════════════
# v3.23.2 — OHLCV 디스크 캐시 + 진행 로그
# ═══════════════════════════════════════════════════════════════


def test_save_load_ohlcv_cache_roundtrip(tmp_path, monkeypatch):
    """ticker × end_date 캐시 hit → close DataFrame 복원."""
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    ser = pd.Series([100.0, 101.5, 102.0])
    qb._save_ohlcv_cache("005930", "20260513", ser)
    loaded = qb._load_ohlcv_cache("005930", "20260513")
    assert loaded is not None
    assert len(loaded) == 3
    assert float(loaded["종가"].iloc[0]) == 100.0
    assert float(loaded["종가"].iloc[-1]) == 102.0


def test_save_ohlcv_cache_skips_empty(tmp_path, monkeypatch):
    """v3.21 패턴 — 빈 응답은 캐시 안 함."""
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    qb._save_ohlcv_cache("000000", "20260513", [])
    qb._save_ohlcv_cache("000000", "20260513", None)
    assert not (tmp_path / "000000_20260513.json").exists()


def test_load_ohlcv_cache_miss_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    assert qb._load_ohlcv_cache("999999", "20260513") is None


def test_load_ohlcv_cache_uses_data_api_when_enabled(tmp_path, monkeypatch):
    class FakeClient:
        def latest_ohlcv(self, ticker):
            return {
                "ticker": ticker,
                "as_of": "20260513",
                "series": {"close": [100.0, 101.0]},
            }

    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(qb, "_DATA_API_CLIENT", FakeClient())
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    loaded = qb._load_ohlcv_cache("005930", "20260513")
    assert loaded["종가"].tolist() == [100.0, 101.0]


def test_load_ohlcv_cache_api_date_mismatch_falls_back(tmp_path, monkeypatch):
    class FakeClient:
        def latest_ohlcv(self, ticker):
            return {
                "ticker": ticker,
                "as_of": "20260512",
                "series": {"close": [999.0]},
            }

    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(qb, "_DATA_API_CLIENT", FakeClient())
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    qb._save_ohlcv_cache("005930", "20260513", pd.Series([100.0, 101.0]))
    loaded = qb._load_ohlcv_cache("005930", "20260513")
    assert loaded["종가"].tolist() == [100.0, 101.0]


def test_load_ohlcv_cache_api_failure_falls_back(tmp_path, monkeypatch):
    class FailingClient:
        def latest_ohlcv(self, ticker):
            raise qb.DataAPIError("down")

    monkeypatch.setenv("AI_AGENT_DATA_API_ENABLED", "1")
    monkeypatch.setattr(qb, "_DATA_API_CLIENT", FailingClient())
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    qb._save_ohlcv_cache("005930", "20260513", pd.Series([100.0]))
    loaded = qb._load_ohlcv_cache("005930", "20260513")
    assert loaded["종가"].tolist() == [100.0]


def test_load_ohlcv_cache_ttl_expired(tmp_path, monkeypatch):
    """파일 mtime이 TTL 초과면 None."""
    import os
    import time
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    monkeypatch.setattr(qb, "OHLCV_TTL_SEC", 1)  # 1초 TTL
    qb._save_ohlcv_cache("005930", "20260513", pd.Series([100.0]))
    p = tmp_path / "005930_20260513.json"
    assert p.exists()
    # mtime을 2초 전으로 강제
    old = time.time() - 5
    os.utime(p, (old, old))
    assert qb._load_ohlcv_cache("005930", "20260513") is None


def test_fetch_ohlcv_cached_first_call_fetches(tmp_path, monkeypatch):
    """첫 호출은 fetch → 캐시 저장."""
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    calls = {"n": 0}
    def fake_collect(ticker, start, end):
        calls["n"] += 1
        close = [100.0 + i for i in range(50)]
        qb._save_ohlcv_cache(ticker, end, close)
        return {"ticker": ticker, "as_of": end, "series": {"close": close}}
    monkeypatch.setattr(qb, "_collect_ohlcv", fake_collect)

    df1 = qb.fetch_ohlcv_cached("005930", "20260101", "20260513")
    assert df1 is not None and len(df1) == 50
    assert calls["n"] == 1
    # 캐시 파일 생성됨
    assert (tmp_path / "005930_20260513.json").exists()


def test_fetch_ohlcv_cached_second_call_hits_cache(tmp_path, monkeypatch):
    """두 번째 호출은 디스크 캐시 → fetch 호출 안 함."""
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    calls = {"n": 0}
    def fake_collect(ticker, start, end):
        calls["n"] += 1
        close = [100.0 + i for i in range(50)]
        qb._save_ohlcv_cache(ticker, end, close)
        return {"ticker": ticker, "as_of": end, "series": {"close": close}}
    monkeypatch.setattr(qb, "_collect_ohlcv", fake_collect)

    qb.fetch_ohlcv_cached("005930", "20260101", "20260513")
    qb.fetch_ohlcv_cached("005930", "20260101", "20260513")
    assert calls["n"] == 1, "두 번째 호출은 캐시 hit으로 fetch skip"


def test_fetch_ohlcv_cached_empty_response_not_cached(tmp_path, monkeypatch):
    """v3.21 패턴 — 빈 df 응답은 캐시 안 함. 다음 호출 다시 fetch."""
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    calls = {"n": 0}
    def fake_collect(ticker, start, end):
        calls["n"] += 1
        raise RuntimeError("empty response")
    monkeypatch.setattr(qb, "_collect_ohlcv", fake_collect)

    qb.fetch_ohlcv_cached("000000", "20260101", "20260513")
    qb.fetch_ohlcv_cached("000000", "20260101", "20260513")
    assert calls["n"] == 2, "빈 응답 캐시 hit 발생하면 안 됨"


def test_fetch_ohlcv_cached_fetch_exception_not_cached(tmp_path, monkeypatch):
    """fetch 예외는 캐시 안 함."""
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    calls = {"n": 0}
    def fake_collect(ticker, start, end):
        calls["n"] += 1
        raise RuntimeError("KRX down")
    monkeypatch.setattr(qb, "_collect_ohlcv", fake_collect)

    df1 = qb.fetch_ohlcv_cached("000000", "20260101", "20260513")
    df2 = qb.fetch_ohlcv_cached("000000", "20260101", "20260513")
    assert df1 is None and df2 is None
    assert calls["n"] == 2


def test_recommend_uses_ohlcv_cache(tmp_path, monkeypatch):
    """recommend_top_n이 캐시를 거치는지 — 두 번째 호출 빠른 path."""
    monkeypatch.setattr(qb, "_OHLCV_CACHE_DIR", tmp_path)
    fetch_calls = {"n": 0}
    def fake_collect(ticker, start, end):
        fetch_calls["n"] += 1
        close = [100.0 * (1.001 ** i) for i in range(280)]
        qb._save_ohlcv_cache(ticker, end, close)
        return {"ticker": ticker, "as_of": end, "series": {"close": close}}
    monkeypatch.setattr(qb, "_collect_ohlcv", fake_collect)

    universe = [("005930", "삼성전자"), ("000660", "SK하이닉스")]
    funds = _fake_fundamentals()
    caps = _fake_caps()

    # 첫 호출 — 2종목 모두 fetch
    qb.recommend_top_n(
        phase="Recovery", universe=universe, top_n=2,
        fundamentals=funds, market_caps=caps,
    )
    assert fetch_calls["n"] == 2

    # 두 번째 호출 — 캐시 hit, fetch 안 함
    qb.recommend_top_n(
        phase="Recovery", universe=universe, top_n=2,
        fundamentals=funds, market_caps=caps,
    )
    assert fetch_calls["n"] == 2, "두 번째 호출에서 fetch 호출되면 안 됨 (캐시)"


# ─── 원자료 신선도 (v3.65, 2026-09-01 사고) ─────────
#
# **콴텍봇이 2년 8개월 낡은 데이터로 "현재 국면"을 냈다.**
# FRED KORLOLITONOSTSAM의 마지막 관측치가 2024-01인데 `compute_level`은
# `series[-1]`을 그냥 썼고 PhaseSnapshot에 기준일 필드조차 없었다.
# 화면에는 2026-09 국면이 "Expansion"으로 확신 있게 떴다.


def test_months_behind_reads_various_label_shapes():
    """ECOS는 YYYYMM, FRED는 YYYY-MM-DD로 온다."""
    assert qb.months_behind("2024-01-01", today="202609") == 32
    assert qb.months_behind("202401", today="202609") == 32
    assert qb.months_behind("2026-09", today="202609") == 0


def test_an_unreadable_label_is_not_treated_as_fresh():
    assert qb.months_behind("알수없음", today="202609") is None
    f = qb.freshness([("알수없음", 1.0)], "X", today="202609")
    assert f["usable"] is False


def test_an_empty_series_is_not_usable():
    f = qb.freshness([], "CLI_KR", today="202609")
    assert f["usable"] is False and f["as_of"] is None


def test_a_fresh_series_passes_without_a_note():
    f = qb.freshness([("2026-08-01", 100.0)], "CLI_KR", today="202609")
    assert f["usable"] is True and f["note"] == ""


def test_a_slightly_late_series_warns_but_is_used():
    """OECD CLI는 정상적으로 1~2개월 지연된다 — 그것까지 막으면 못 쓴다."""
    f = qb.freshness([("2026-05-01", 100.0)], "CLI_KR", today="202609")
    assert f["usable"] is True and "확인 필요" in f["note"]


def test_a_long_dead_series_is_dropped_not_used():
    """이게 실제로 일어난 일이다 — 2024-01 값으로 2026-09를 판정했다."""
    f = qb.freshness([("2024-01-01", 100.2)], "CLI_KR", today="202609")
    assert f["usable"] is False
    assert "32개월" in f["note"]


def test_a_stale_series_yields_no_phase(monkeypatch):
    """**모르는 것을 'Expansion'이라고 말하지 않는다.**"""
    dead = [(f"2023-{m:02d}", 100.0 + m * 0.3) for m in range(1, 13)] + \
           [(f"2024-{m:02d}", 103.0 + m * 0.3) for m in range(1, 7)]

    monkeypatch.setattr(qb, "fetch_series", lambda name, months=24: dead)
    snap = qb.snapshot()
    assert snap.phase_kr is None
    assert snap.consensus_phase is None
    assert snap.stale is True


def test_the_snapshot_carries_each_series_as_of(monkeypatch):
    """기준일 필드가 없어서 낡은 값이 현재로 나갔다 — 이제 들고 다닌다."""
    monkeypatch.setattr(qb, "fetch_series",
                        lambda name, months=24: [("2024-01-01", 100.2)])
    snap = qb.snapshot()
    assert snap.freshness
    names = {f["name"] for f in snap.freshness}
    assert names == {"CLI_KR", "CLI_US", "BSI_KR"}
    assert all(f["as_of"] == "2024-01-01" for f in snap.freshness)


def test_a_fresh_snapshot_is_not_marked_stale(monkeypatch):
    fresh = _series([95.0 + i * 0.4 for i in range(24)])
    monkeypatch.setattr(qb, "fetch_series", lambda name, months=24: fresh)
    snap = qb.snapshot()
    assert snap.stale is False


def test_the_freshness_label_names_the_series_actually_used(monkeypatch):
    """교체 후 화면이 쓰지도 않는 이름으로 기준일을 보고하면 안 된다."""
    monkeypatch.setattr(qb, "fetch_series",
                        lambda name, months=24: [("2024-01-01", 100.0)])
    snap = qb.snapshot()
    names = {f["name"] for f in snap.freshness}
    assert qb.ACTIVE_CLI["KR"] in names
    assert qb.ACTIVE_CLI["US"] in names
