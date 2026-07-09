"""
finance_bot 단위 테스트.

외부 HTTP(_fetch_ecos_raw / _fetch_fred_raw)는 monkeypatch로 가짜화.
검증 대상:
  - 지표 카탈로그 매핑 + 케이스 무관 매칭
  - 캐시 hit/miss + TTL 만료
  - 키 미설정 시 친절한 에러 메시지
  - dashboard / format
  - run() entrypoint 분기 + 알 수 없는 action
  - latest action validator (router 측은 별도)
"""
from __future__ import annotations
import time

import pytest

import finance_bot as fb


@pytest.fixture(autouse=True)
def _clear_cache():
    fb.clear_cache()
    yield
    fb.clear_cache()


# ─── 카탈로그 ────────────────────────────────────────


def test_indicators_have_required_fields():
    for k, ind in fb.INDICATORS.items():
        assert ind.key == k
        assert ind.source in ("ecos", "fred")
        assert ind.label
        assert ind.unit


# ─── 파서 ────────────────────────────────────────────


def test_parse_ecos_latest_picks_most_recent():
    payload = {
        "StatisticSearch": {
            "row": [
                {"DATA_VALUE": "100", "TIME": "202403"},
                {"DATA_VALUE": "120", "TIME": "202405"},  # 최신
                {"DATA_VALUE": "110", "TIME": "202404"},
            ]
        }
    }
    val, asof = fb._parse_ecos_latest(payload)
    assert val == 120.0
    assert asof == "202405"


def test_parse_ecos_latest_empty_raises():
    with pytest.raises(ValueError):
        fb._parse_ecos_latest({"StatisticSearch": {"row": []}})


def test_parse_ecos_latest_value_with_comma():
    payload = {"StatisticSearch": {"row": [
        {"DATA_VALUE": "1,372.50", "TIME": "20260501"}
    ]}}
    val, _ = fb._parse_ecos_latest(payload)
    assert val == pytest.approx(1372.50)


def test_parse_fred_latest_basic():
    payload = {"observations": [{"date": "2026-04-30", "value": "5.50"}]}
    val, asof = fb._parse_fred_latest(payload)
    assert val == 5.50
    assert asof == "2026-04-30"


def test_parse_fred_latest_missing_value():
    payload = {"observations": [{"date": "2026-04-30", "value": "."}]}
    with pytest.raises(ValueError):
        fb._parse_fred_latest(payload)


# ─── fetch_indicator: 가짜 fetcher ──────────────────


def test_fetch_indicator_unknown_returns_none():
    assert fb.fetch_indicator("BITCOIN") is None


def test_fetch_indicator_ecos_success(monkeypatch):
    def fake_ecos(stat, item, cycle):
        assert stat == "731Y001"
        assert item == "0000001"
        return {"StatisticSearch": {"row": [
            {"DATA_VALUE": "1372.50", "TIME": "20260501"}
        ]}}
    monkeypatch.setattr(fb, "_fetch_ecos_raw", fake_ecos)

    res = fb.fetch_indicator("USD/KRW")
    assert res is not None
    assert "error" not in res
    assert res["value"] == pytest.approx(1372.50)
    assert res["unit"] == "원"
    assert res["source"] == "ecos"
    assert res["cached"] is False


def test_fetch_indicator_fred_success(monkeypatch):
    monkeypatch.setattr(fb, "_fetch_fred_raw",
                        lambda series_id, limit=1: {"observations": [
                            {"date": "2026-04-30", "value": "21.5"}
                        ]})
    res = fb.fetch_indicator("VIX")
    assert res["value"] == 21.5
    assert res["source"] == "fred"


def test_fetch_indicator_case_insensitive(monkeypatch):
    monkeypatch.setattr(fb, "_fetch_fred_raw",
                        lambda s, limit=1: {"observations": [
                            {"date": "2026-04-30", "value": "5.5"}
                        ]})
    res = fb.fetch_indicator("vix")
    assert res is not None
    assert res["key"] == "VIX"


def test_fetch_indicator_no_api_key(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "")
    # raw fetcher의 기본 동작에 의존 — 키 검사 실패 시 RuntimeError
    res = fb.fetch_indicator("VIX")
    assert "error" in res
    assert "FRED_API_KEY" in res["error"]


def test_fetch_indicator_caches(monkeypatch):
    calls = {"n": 0}
    def fake(series_id, limit=1):
        calls["n"] += 1
        return {"observations": [{"date": "2026-04-30", "value": "5"}]}
    monkeypatch.setattr(fb, "_fetch_fred_raw", fake)

    first = fb.fetch_indicator("VIX")
    second = fb.fetch_indicator("VIX")
    assert calls["n"] == 1
    assert first["cached"] is False
    assert second["cached"] is True


def test_cache_ttl_expires(monkeypatch):
    calls = {"n": 0}
    def fake(s, limit=1):
        calls["n"] += 1
        return {"observations": [{"date": "x", "value": "1"}]}
    monkeypatch.setattr(fb, "_fetch_fred_raw", fake)
    monkeypatch.setattr(fb, "CACHE_TTL_SEC", 0.1)

    fb.fetch_indicator("VIX")
    assert calls["n"] == 1
    time.sleep(0.15)
    fb.fetch_indicator("VIX")
    assert calls["n"] == 2  # 만료됐어야


# ─── format / dashboard ─────────────────────────────


def test_format_indicator_line_value():
    item = {"label": "VIX", "value": 21.5, "unit": "pt", "asof": "2026-04-30",
            "cached": False, "source": "fred"}
    line = fb._format_indicator_line(item)
    assert "VIX" in line and "21.5" in line and "pt" in line and "(cached)" not in line


def test_format_indicator_line_cached_flag():
    item = {"label": "VIX", "value": 21.5, "unit": "pt", "asof": "x",
            "cached": True, "source": "fred"}
    assert "(cached)" in fb._format_indicator_line(item)


def test_format_indicator_line_error():
    item = {"label": "VIX", "error": "FRED_API_KEY 미설정"}
    line = fb._format_indicator_line(item)
    assert "⚠️" in line and "FRED_API_KEY" in line


def test_format_dashboard_empty():
    assert "지표 없음" in fb.format_dashboard([])


def test_format_dashboard_has_header():
    items = [{"label": "VIX", "value": 21.5, "unit": "pt",
              "asof": "x", "cached": False, "source": "fred"}]
    out = fb.format_dashboard(items)
    assert "📊" in out
    assert "VIX" in out


def test_dashboard_aggregates(monkeypatch):
    monkeypatch.setattr(fb, "_fetch_ecos_raw",
                        lambda s, i, c: {"StatisticSearch": {"row": [
                            {"DATA_VALUE": "3.5", "TIME": "202405"}
                        ]}})
    monkeypatch.setattr(fb, "_fetch_fred_raw",
                        lambda s, limit=1: {"observations": [
                            {"date": "2026-04-30", "value": "5.5"}
                        ]})
    items = fb.dashboard()
    assert len(items) == len(fb.INDICATORS)
    keys = {i["key"] for i in items}
    assert "VIX" in keys and "USD/KRW" in keys


# ─── run() entrypoint ────────────────────────────────


def test_run_latest_success(monkeypatch):
    monkeypatch.setattr(fb, "_fetch_fred_raw",
                        lambda s, limit=1: {"observations": [
                            {"date": "2026-04-30", "value": "21.5"}
                        ]})
    msg, sources = fb.run("latest", indicator="VIX")
    assert "VIX" in msg
    assert "21.5" in msg
    assert sources == []


def test_run_latest_unknown_indicator():
    msg, _ = fb.run("latest", indicator="Bitcoin")
    assert "❌" in msg
    assert "Bitcoin" in msg


def test_run_latest_missing_indicator():
    msg, _ = fb.run("latest")
    assert "❌" in msg


def test_run_dashboard(monkeypatch):
    monkeypatch.setattr(fb, "_fetch_ecos_raw",
                        lambda s, i, c: {"StatisticSearch": {"row": [
                            {"DATA_VALUE": "1.0", "TIME": "20260501"}
                        ]}})
    monkeypatch.setattr(fb, "_fetch_fred_raw",
                        lambda s, limit=1: {"observations": [
                            {"date": "2026-04-30", "value": "1.0"}
                        ]})
    msg, _ = fb.run("dashboard")
    assert "📊" in msg


def test_run_unknown_action():
    msg, _ = fb.run("dance")
    assert "❌" in msg


def test_compare_no_chunks_falls_back(monkeypatch):
    monkeypatch.setattr(fb, "_fetch_fred_raw",
                        lambda s, limit=1: {"observations": [
                            {"date": "x", "value": "1"}
                        ]})
    monkeypatch.setattr(fb, "_fetch_ecos_raw",
                        lambda s, i, c: {"StatisticSearch": {"row": [
                            {"DATA_VALUE": "1", "TIME": "x"}
                        ]}})
    monkeypatch.setattr(fb, "_gather_principles", lambda k_each=2: [])
    msg, chunks = fb.compare_with_principles()
    assert "📊" in msg
    assert chunks == []
    # LLM 호출 없이 fallback 메시지
    assert "원칙 노트를 찾지 못" in msg



# ─── 병렬 dashboard (v3.8) ───────────────────────────


def test_dashboard_parallel_faster_than_serial(monkeypatch):
    """병렬은 직렬보다 명백히 빨라야 함 (mock fetcher에 sleep 0.05초씩)."""
    import time
    fb.clear_cache()

    def slow_ecos(s, i, c):
        time.sleep(0.05)
        return {"StatisticSearch": {"row": [{"DATA_VALUE": "1.0", "TIME": "20260501"}]}}
    def slow_fred(s, limit=1):
        time.sleep(0.05)
        return {"observations": [{"date": "2026-04-30", "value": "1.0"}]}

    monkeypatch.setattr(fb, "_fetch_ecos_raw", slow_ecos)
    monkeypatch.setattr(fb, "_fetch_fred_raw", slow_fred)

    fb.clear_cache()
    t0 = time.perf_counter()
    out_serial = fb.dashboard(parallel=False)
    t_serial = time.perf_counter() - t0

    fb.clear_cache()
    t0 = time.perf_counter()
    out_parallel = fb.dashboard(parallel=True)
    t_parallel = time.perf_counter() - t0

    # 병렬은 직렬의 절반 이하여야 (6개 동시면 이론상 1/6, 보수적으로 1/2)
    assert t_parallel < t_serial / 2, f"직렬 {t_serial:.2f}s vs 병렬 {t_parallel:.2f}s — 충분히 빠르지 않음"
    # 결과 순서·내용 동일
    assert [x["key"] for x in out_serial] == [x["key"] for x in out_parallel]


def test_dashboard_parallel_handles_exception(monkeypatch):
    """한 지표가 RuntimeError 던지면 그 지표만 error 표시, 나머지는 정상."""
    fb.clear_cache()
    call_count = {"n": 0}

    def picky_ecos(s, i, c):
        call_count["n"] += 1
        if "722Y001" in s:  # 기준금리만 실패
            raise RuntimeError("ECOS 죽음")
        return {"StatisticSearch": {"row": [{"DATA_VALUE": "1.0", "TIME": "20260501"}]}}

    monkeypatch.setattr(fb, "_fetch_ecos_raw", picky_ecos)
    monkeypatch.setattr(fb, "_fetch_fred_raw",
                        lambda s, limit=1: {"observations": [{"date": "x", "value": "1.0"}]})

    items = fb.dashboard(parallel=True)
    assert len(items) == len(fb.INDICATORS)
    err_items = [i for i in items if "error" in i]
    ok_items = [i for i in items if "error" not in i]
    assert len(err_items) >= 1
    assert any("기준금리" in i["label"] for i in err_items)
    assert len(ok_items) >= 4


def test_dashboard_parallel_preserves_order(monkeypatch):
    """병렬 fetch는 완료 순서가 다를 수 있지만 결과는 INDICATORS 순서 유지."""
    fb.clear_cache()
    import random

    def variable_speed_ecos(s, i, c):
        import time
        time.sleep(random.uniform(0.01, 0.05))
        return {"StatisticSearch": {"row": [{"DATA_VALUE": "1.0", "TIME": "x"}]}}
    def variable_speed_fred(s, limit=1):
        import time
        time.sleep(random.uniform(0.01, 0.05))
        return {"observations": [{"date": "x", "value": "1.0"}]}

    monkeypatch.setattr(fb, "_fetch_ecos_raw", variable_speed_ecos)
    monkeypatch.setattr(fb, "_fetch_fred_raw", variable_speed_fred)

    items = fb.dashboard(parallel=True)
    expected_order = list(fb.INDICATORS.keys())
    actual_order = [i["key"] for i in items]
    assert actual_order == expected_order
