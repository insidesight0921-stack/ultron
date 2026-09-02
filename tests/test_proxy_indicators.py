"""test_proxy_indicators.py — 실시간 대리 지표(순수) 검증 (hermetic, 네트워크 없음)."""
from __future__ import annotations

import proxy_indicators as pi


def _series(n, start=2500.0, step=1.0):
    return [start + step * i for i in range(n)]


# ─── 이동평균·기울기 ─────────────────────────────────


def test_moving_average_needs_a_full_window():
    assert pi.moving_average([1, 2, 3], 5) is None
    assert pi.moving_average([1, 2, 3, 4], 4) == 2.5


def test_slope_needs_window_plus_lookback():
    """200일선의 20일 전 값을 알려면 220일이 있어야 한다."""
    assert pi.ma_slope_pct(_series(219)) is None
    assert pi.ma_slope_pct(_series(220)) is not None


def test_rising_market_gives_a_positive_slope():
    assert pi.ma_slope_pct(_series(300, step=2.0)) > 0


def test_falling_market_gives_a_negative_slope():
    assert pi.ma_slope_pct(_series(300, step=-2.0)) < 0


def test_flat_market_slope_is_zero():
    assert pi.ma_slope_pct([2500.0] * 300) == 0.0


def test_slope_is_far_slower_than_price():
    """200일선은 가격보다 훨씬 느리다 — 그래서 잡음이 아니라 추세를 본다.

    (처음엔 '급락해도 200일선은 오를 수 있다'로 썼다가 실측에서 틀린 것을 확인했다.
     20일치가 평균 안으로 들어오면 200일선도 실제로 꺾인다. 성질은 '느리다'이지
     '안 꺾인다'가 아니다.)
    """
    closes = _series(280, step=2.0) + [2000.0] * 20
    price_drop = (closes[-1] / closes[-21] - 1) * 100
    slope = pi.ma_slope_pct(closes)
    assert slope < 0
    assert abs(slope) < abs(price_drop) / 5


def test_pct_change():
    assert pi.pct_change([100, 110], 1) == 10.0
    assert pi.pct_change([100], 1) is None
    assert pi.pct_change([0, 110], 1) is None


# ─── 방향 판정 ───────────────────────────────────────


def test_slope_states():
    assert pi.slope_state(1.0) == "risk_on"
    assert pi.slope_state(-1.0) == "risk_off"
    assert pi.slope_state(0.1) == "neutral"
    assert pi.slope_state(None) == "unknown"


def test_weak_won_is_risk_off_not_risk_on():
    """환율은 부호가 뒤집힌다 — 여기서 자주 틀린다."""
    # 여기도 임계값을 박지 않는다(위 test_vix_direction_is_inverted 주석 참조).
    move = pi._param("fx.move", pi.FX_MOVE_PCT)
    assert pi.fx_state(move + 1) == "risk_off"   # 환율 상승 = 원화 약세
    assert pi.fx_state(-(move + 1)) == "risk_on"
    assert pi.fx_state(move / 2) == "neutral"
    assert pi.fx_state(None) == "unknown"


def test_foreign_flow_direction():
    assert pi.flow_state(1000) == "risk_on"
    assert pi.flow_state(-1000) == "risk_off"
    assert pi.flow_state(0) == "neutral"
    assert pi.flow_state(None) == "unknown"


def test_vix_direction_is_inverted():
    """**임계값을 박아두지 않는다.**

    2026-09-01에 이 테스트가 `vix_state(22.0) == "neutral"`로 실패했다.
    승인 원장에서 임계값이 18/28 → 15.7/20.6으로 바뀌었기 때문인데, 검사하려던
    것은 '방향이 뒤집혀 있는가'이지 22가 어느 밴드인가가 아니었다. 숫자를
    박아두면 값이 바뀔 때마다 **멀쩡한 코드가 실패**하고, 그걸 몇 번 겪으면
    숫자를 고쳐서 넘기게 된다 — 그때부터 이 테스트는 아무것도 안 지킨다.
    """
    calm, stress = pi._param("vix.calm", pi.VIX_CALM), pi._param(
        "vix.stress", pi.VIX_STRESS)
    assert calm < stress, "임계값 순서가 뒤집혔다"
    assert pi.vix_state(calm - 1) == "risk_on"       # 낮으면 위험선호
    assert pi.vix_state(stress + 1) == "risk_off"    # 높으면 위험회피
    assert pi.vix_state((calm + stress) / 2) == "neutral"
    assert pi.vix_state(None) == "unknown"


# ─── 지표 항목 ───────────────────────────────────────


def test_indicator_marks_availability():
    assert pi.indicator("x", 1.0, "risk_on")["available"] is True
    assert pi.indicator("x", None, "unknown")["available"] is False


# ─── 집계 ────────────────────────────────────────────


def _items(*states):
    return [pi.indicator(f"i{n}", None if s == "unknown" else 1.0, s)
            for n, s in enumerate(states)]


def test_summary_counts_directions():
    s = pi.summarize(_items("risk_on", "risk_on", "risk_off"))
    assert s["counts"]["risk_on"] == 2 and s["lean"] == "위험선호 우세"


def test_summary_reports_a_tie_as_mixed():
    assert pi.summarize(_items("risk_on", "risk_off"))["lean"] == "혼조"


def test_summary_without_any_data_refuses_to_lean():
    s = pi.summarize(_items("unknown", "unknown"))
    assert s["lean"] == "판정 불가" and s["n_available"] == 0


def test_summary_lists_missing_indicators():
    items = [pi.indicator("환율", None, "unknown"), pi.indicator("VIX", 20.0, "neutral")]
    assert pi.summarize(items)["missing"] == ["환율"]


def test_summary_does_not_produce_a_single_blended_number():
    """단위도 신뢰도도 다른 값을 하나로 합치면 어디서 왔는지 알 수 없게 된다."""
    s = pi.summarize(_items("risk_on", "risk_off", "neutral"))
    assert "score" not in s and "weighted" not in s


# ─── 표시 ────────────────────────────────────────────


def test_format_shows_missing_and_says_it_does_not_fill_them():
    snap = {"indicators": [pi.indicator("환율", None, "unknown"),
                           pi.indicator("VIX", 20.0, "neutral", unit="pt")],
            "summary": pi.summarize([pi.indicator("환율", None, "unknown"),
                                     pi.indicator("VIX", 20.0, "neutral")])}
    text = pi.format_snapshot(snap)
    assert "미확보: 환율" in text and "만들어 채우지 않습니다" in text


def test_format_shows_a_dash_for_missing_values():
    snap = {"indicators": [pi.indicator("환율", None, "unknown", unit="원")],
            "summary": pi.summarize([pi.indicator("환율", None, "unknown")])}
    assert "환율: —" in pi.format_snapshot(snap)


def test_format_carries_the_note_about_thresholds_being_hypotheses():
    snap = {"indicators": [], "summary": pi.summarize([])}
    assert "가설" in pi.format_snapshot(snap)


def test_format_marks_vix_as_a_stand_in():
    item = pi.indicator("VIX", 20.0, "neutral", unit="pt",
                        note="VKOSPI 대용. 같은 지표가 아님")
    text = pi.format_snapshot({"indicators": [item], "summary": pi.summarize([item])})
    assert "같은 지표가 아님" in text


# ─── 자격 정보·실패 처리 (2026-08-28) ────────────────
#
# 첫 실행에서 4개 중 1개만 나왔다. ECOS·FRED·KRX 셋 다 ".env에 키가 있는데"
# 미설정으로 실패했다 — launchd·CLI 프로세스가 .env를 읽지 않았기 때문이다.
# 그 위에 float(None) TypeError가 겹쳐 진짜 원인이 가려졌다.

import sys
import types


def _fake_finance(payload):
    mod = types.ModuleType("finance_bot")
    mod.fetch_indicator = lambda name: payload
    return mod


def test_finance_value_reads_the_asof_key(monkeypatch):
    """키 이름은 `asof`다. `date`/`as_of`를 찾다가 기준일이 늘 비어 있었다."""
    monkeypatch.setitem(sys.modules, "finance_bot",
                        _fake_finance({"value": 1385.5, "asof": "2026-08-27"}))
    assert pi._finance_value("USD/KRW") == (1385.5, "2026-08-27")


def test_finance_value_survives_an_error_payload(monkeypatch):
    """fetch_indicator는 실패 시 값 없이 {'error': ...}를 준다 — 이걸 성공으로 보고
    float()에 넣어 TypeError를 냈다. 원인(키 미로딩)이 형변환 오류에 가려졌다."""
    monkeypatch.setitem(sys.modules, "finance_bot",
                        _fake_finance({"key": "fx", "error": "ECOS_API_KEY 미설정"}))
    assert pi._finance_value("USD/KRW") == (None, None)


def test_finance_value_treats_a_null_value_as_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "finance_bot",
                        _fake_finance({"value": None, "asof": "2026-08-27"}))
    assert pi._finance_value("VIX") == (None, None)


def test_finance_value_does_not_raise_on_a_broken_payload(monkeypatch):
    monkeypatch.setitem(sys.modules, "finance_bot", _fake_finance({"value": "숫자아님"}))
    assert pi._finance_value("VIX") == (None, None)


def test_required_env_lists_every_credential_the_snapshot_uses():
    """키를 하나도 안 넣어도 200일선(로컬 캐시)은 나온다 — 1/4을 보고 '도는구나'
    하고 넘어가지 않도록, 필요한 키를 코드에 명시해 둔다."""
    assert set(pi.REQUIRED_ENV) == {"ECOS_API_KEY", "FRED_API_KEY", "KRX_ID", "KRX_PW"}


def test_format_separates_a_missing_credential_from_a_failed_fetch():
    """'수집 실패'와 '키가 없어 시도조차 못 함'은 고쳐야 할 곳이 다르다."""
    item = pi.indicator("VIX", None, "unknown", unit="pt")
    text = pi.format_snapshot({"indicators": [item],
                               "summary": pi.summarize([item]),
                               "missing_env": ["FRED_API_KEY"]})
    assert "자격 정보 미설정: FRED_API_KEY" in text
    assert "시도 자체를 못 한 것" in text


def test_format_stays_quiet_when_every_credential_is_set():
    item = pi.indicator("VIX", 20.0, "neutral", unit="pt")
    text = pi.format_snapshot({"indicators": [item],
                               "summary": pi.summarize([item]), "missing_env": []})
    assert "자격 정보 미설정" not in text


# ─── 안 도는 가지를 실제로 열었는가 (v3.64) ─────────
#
# `fx_change = None`이 코드에 상수로 박혀 있어 `fx_state`는 줄곧 `unknown`
# 이었다. 임계값이 안 걸린 게 아니라 **계산 자체를 한 적이 없었다.**
# 지표 목록에는 이름이 올라 있어 밖에서는 4개를 보는 것처럼 보였다.


def test_the_change_is_computed_not_hardcoded_to_none():
    """상수 None이 남아 있으면 이 항은 영원히 unknown이다."""
    import ast
    import inspect

    import proxy_indicators as pi

    src = inspect.getsource(pi.snapshot)
    tree = ast.parse(src.lstrip())
    hardcoded = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "fx_change" for t in n.targets)
        and isinstance(n.value, ast.Constant) and n.value.value is None
    ]
    assert not hardcoded, "fx_change가 다시 상수 None으로 돌아갔다"


def test_a_real_change_produces_a_real_state():
    import proxy_indicators as pi

    closes = [1300.0] * 20 + [1400.0]          # +7.7%
    ch = pi.fx_change_pct(closes)
    assert ch is not None and ch > 2.0
    assert pi.fx_state(ch) == "risk_off"       # 원화 약세 → 위험회피


def test_a_short_series_gives_none_not_zero():
    """0은 '변화 없음'이라는 사실이다 — 모르는 것과 다르다."""
    import proxy_indicators as pi

    assert pi.fx_change_pct([1300.0, 1310.0]) is None
    assert pi.fx_change_pct([]) is None


def test_a_flat_series_gives_zero_not_none():
    """반대로, 진짜 변화가 없는 것은 0으로 말해야 한다."""
    import proxy_indicators as pi

    assert pi.fx_change_pct([1300.0] * 25) == 0.0


def test_a_stale_cache_is_not_used_silently(tmp_path, monkeypatch):
    """낡은 환율로 '원화 강세'라고 말하면 없는 사실이 생긴다."""
    import json

    import price_sanity as ps
    import proxy_indicators as pi

    root = tmp_path / "fx"
    root.mkdir(parents=True)
    (root / "usdkrw_20200101.json").write_text(json.dumps({
        "series": {"date": ["20200101"], "close": [1200.0]}}), encoding="utf-8")
    monkeypatch.setattr(ps, "_cache_root", lambda: tmp_path)
    closes, as_of = pi._fx_series(refresh=False)
    # 갱신을 끄면 캐시를 그대로 준다(호출부가 신선도를 판단할 수 있게).
    assert as_of == "20200101"
    # 갱신을 켜면 낡은 것으로 판정하고 네트워크를 시도한다 — 실패해도
    # 낡은 값을 돌려주지 않는다.
    monkeypatch.setattr(pi, "FX_STALE_DAYS", 1)
    import quant_bot as qb

    def _boom(*a, **k):
        raise RuntimeError("네트워크 없음")

    monkeypatch.setattr(qb, "_fetch_ecos_series_raw", _boom)
    closes2, _ = pi._fx_series(refresh=True)
    assert closes2 == []


# ─── 검증 상태를 지표 옆에 붙인다 (2026-09-02) ──────────

def _snap(names=("코스피 200일선 기울기", "외국인 순매수(5일)")):
    return {"summary": {"lean": "혼조", "n_available": len(names),
                        "n_total": len(names), "missing": [], "counts": {}},
            "indicators": [{"name": n, "value": 1.0, "unit": "%",
                            "state": "risk_on", "as_of": "20260902",
                            "note": "", "available": True} for n in names]}


def test_a_disproven_indicator_is_marked_on_screen():
    """**화면이 지표를 그냥 보여주면 사람은 신호로 읽는다.**

    2026-09-02 실측: 외국인 순매수 2,162건 적중 50.0% vs 기준선 54.7%.
    """
    notes = {"코스피 200일선 기울기": "**예측력 없음**(표본 1490건에서 기준선 미달)",
             "외국인 순매수(5일)": "**예측력 없음**(표본 2162건에서 기준선 미달)"}
    out = pi.format_snapshot(_snap(), notes=notes)
    assert "예측력이 없다고 측정된 지표" in out
    assert out.count("예측력 없음") >= 2


def test_an_unmeasured_indicator_says_so_rather_than_looking_verified():
    out = pi.format_snapshot(_snap(), notes={"코스피 200일선 기울기": "미측정",
                                             "외국인 순매수(5일)": "미측정"})
    assert "[미측정]" in out
    assert "예측력이 없다고 측정된 지표" not in out


def test_the_footer_explains_what_the_bracket_means():
    out = pi.format_snapshot(_snap(), notes={})
    assert "검증 상태" in out and "기준선" in out


def test_a_missing_ledger_does_not_break_the_screen(tmp_path):
    """원장이 없어도 화면은 떠야 한다 — 없으면 미측정이라고 말한다."""
    got = pi.verification_notes(["외국인 순매수(5일)"], ledger=tmp_path / "없음.json")
    assert got == {"외국인 순매수(5일)": "미측정"}
