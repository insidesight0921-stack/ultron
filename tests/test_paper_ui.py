"""
paper_ui v3.19 단위 테스트 — /api/paper/kium-scan 엔드포인트 + HTML 탭 sanity.

paper_ui는 FastAPI라 TestClient 사용 가능. lancedb 의존 없음 (web_ui와 독립).
kium_bot의 외부 호출(pykrx)만 monkeypatch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# scripts 경로 등록
ROOT = Path(__file__).resolve().parent.parent / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """매 테스트마다 fresh paper.db + TestClient."""
    # paper_db의 디폴트 경로를 tmp로 override
    import paper_db
    fresh_db = tmp_path / "paper.db"
    monkeypatch.setattr(paper_db, "DEFAULT_DB_PATH", fresh_db)
    # paper_db 함수들은 db_path 기본인자를 def 시점에 캡처하므로
    # 모듈 속성 패치만으론 부족 → _conn을 fresh_db로 리다이렉트해
    # 모든 DB 접근을 격리 (실제 data/paper.db 오염 방지)
    _orig_conn = paper_db._conn
    monkeypatch.setattr(paper_db, "_conn",
                        lambda db_path=fresh_db: _orig_conn(fresh_db))

    # paper_ui import 후 ensure_seed() 호출됨
    import importlib
    import paper_ui
    importlib.reload(paper_ui)

    from fastapi.testclient import TestClient
    return TestClient(paper_ui.app)


def _stub_kium(monkeypatch, results=None, kospi_close=None, vkospi_value=None):
    """kium_bot의 외부 호출들을 모두 monkeypatch."""
    import kium_bot

    monkeypatch.setattr(
        kium_bot, "fetch_universe",
        lambda market="KOSPI200", force_refresh=False: [
            ("005930", "삼성전자"), ("000660", "SK하이닉스"),
        ],
    )

    def fake_ohlcv(ticker, start, end):
        # 280일 단조 증가 — momentum 양수
        n = 280
        prices = [100.0 * (1.001 ** i) for i in range(n)]
        return pd.DataFrame({"종가": prices})
    monkeypatch.setattr(kium_bot, "_fetch_ohlcv_raw", fake_ohlcv)

    if kospi_close is None:
        kospi_close = pd.Series([2500.0] * 250)
    monkeypatch.setattr(kium_bot, "fetch_kospi_close",
                        lambda days=280: kospi_close)
    monkeypatch.setattr(kium_bot, "fetch_vkospi_latest",
                        lambda: vkospi_value if vkospi_value is not None else 18.0)


# ─── 헬스 + slots ────────────────────────────────────


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["service"] == "paper_ui"


def test_slots_after_seed(client):
    r = client.get("/api/paper/slots")
    assert r.status_code == 200
    slots = r.json()
    assert len(slots) == 3
    names = {s["name"] for s in slots}
    assert names == {"콴텍", "키움", "IPO"}


# ─── /api/paper/kium-scan ────────────────────────────


def test_kium_scan_default(client, monkeypatch):
    _stub_kium(monkeypatch)
    r = client.get("/api/paper/kium-scan?top_n=2")
    assert r.status_code == 200
    d = r.json()
    assert "results" in d
    assert "crash_signals" in d
    assert "weight" in d
    assert d["market"] == "KOSPI200"
    assert d["top_n"] == 2


def test_kium_scan_results_structure(client, monkeypatch):
    _stub_kium(monkeypatch)
    r = client.get("/api/paper/kium-scan?top_n=2")
    d = r.json()
    assert len(d["results"]) <= 2
    if d["results"]:
        first = d["results"][0]
        for key in ("ticker", "name", "score", "current_price",
                    "return_1m", "return_12m"):
            assert key in first


def test_kium_scan_with_crash_signals_includes_recommendation(client, monkeypatch):
    _stub_kium(monkeypatch)
    r = client.get("/api/paper/kium-scan?top_n=2&with_crash_signals=true")
    d = r.json()
    assert d["crash_signals"] is not None
    assert "recommendation" in d["crash_signals"]
    assert "hits" in d["crash_signals"]


def test_kium_scan_without_crash_signals(client, monkeypatch):
    _stub_kium(monkeypatch)
    r = client.get("/api/paper/kium-scan?top_n=2&with_crash_signals=false")
    d = r.json()
    assert d["crash_signals"] is None
    assert d["weight"] is None


def test_kium_scan_market_param(client, monkeypatch):
    _stub_kium(monkeypatch)
    r = client.get("/api/paper/kium-scan?market=KOSDAQ150&top_n=1")
    d = r.json()
    assert d["market"] == "KOSDAQ150"


def test_kium_scan_includes_weight_when_crash_on(client, monkeypatch):
    _stub_kium(monkeypatch, vkospi_value=18.0)
    r = client.get("/api/paper/kium-scan?top_n=2&with_crash_signals=true")
    d = r.json()
    assert d["weight"] is not None
    assert "equity_weight" in d["weight"]
    assert "bond_weight" in d["weight"]


def test_kium_scan_high_vkospi_reduces_equity(client, monkeypatch):
    _stub_kium(monkeypatch, vkospi_value=35.0)
    r = client.get("/api/paper/kium-scan?top_n=2&with_crash_signals=true")
    d = r.json()
    assert d["weight"]["vkospi_band"] == "high"


# ─── HTML 페이지 sanity ──────────────────────────────


def test_index_html_has_four_tabs(client):
    """v3.23: 4번째 콴텍 탭 추가."""
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'data-tab="tab-order"' in html
    assert 'data-tab="tab-chart"' in html
    assert 'data-tab="tab-signals"' in html
    assert 'data-tab="tab-quant"' in html


def test_index_html_includes_plotly_script(client):
    """v3.20: TradingView 무료 위젯이 한국 주식 미지원 → Plotly 자체 캔들차트로 교체."""
    r = client.get("/")
    html = r.text
    assert "plotly" in html.lower()
    assert "Plotly.newPlot" in html
    assert "candlestick" in html.lower()


def test_index_html_no_tradingview_remnants(client):
    """v3.20: TradingView 잔여 코드 없어야."""
    r = client.get("/")
    html = r.text
    assert "TradingView.widget" not in html
    assert "s3.tradingview.com" not in html


def test_index_html_includes_kium_scan_endpoint(client):
    r = client.get("/")
    html = r.text
    assert "/api/paper/kium-scan" in html


def test_index_html_seed_warning(client):
    """페이지에 paper trading 안내가 있어야."""
    r = client.get("/")
    html = r.text
    assert "Paper Trading" in html
    assert "실주문 안 함" in html or "가상" in html


# ─── /api/paper/chart-data (v3.20) ──────────────────


def _stub_pykrx_ohlcv(monkeypatch, n_days=200, fail=False):
    """paper_ui.api_chart_data 안의 'from pykrx import stock'을 monkeypatch."""
    import types
    fake_stock = types.SimpleNamespace()

    def get_ohlcv(start, end, ticker):
        if fail:
            raise RuntimeError("KRX down")
        idx = pd.date_range("2025-11-01", periods=n_days, freq="B")
        return pd.DataFrame({
            "시가":   [80000.0 + i * 10 for i in range(n_days)],
            "고가":   [80500.0 + i * 10 for i in range(n_days)],
            "저가":   [79500.0 + i * 10 for i in range(n_days)],
            "종가":   [80200.0 + i * 10 for i in range(n_days)],
            "거래량": [1_000_000 + i * 100 for i in range(n_days)],
        }, index=idx)
    fake_stock.get_market_ohlcv = get_ohlcv
    fake_stock.get_market_ticker_name = lambda t: "삼성전자"

    import sys
    sys.modules["pykrx"] = types.SimpleNamespace(stock=fake_stock)
    sys.modules["pykrx.stock"] = fake_stock
    return fake_stock


def test_chart_data_returns_ohlcv(client, monkeypatch):
    _stub_pykrx_ohlcv(monkeypatch, n_days=180)
    r = client.get("/api/paper/chart-data/005930")
    assert r.status_code == 200
    d = r.json()
    assert d["ticker"] == "005930"
    assert d["name"] == "삼성전자"
    assert d["n"] == 180
    for key in ("dates", "opens", "highs", "lows", "closes", "volumes"):
        assert key in d
    assert len(d["opens"]) == 180
    assert len(d["dates"]) == 180


def test_chart_data_truncates_to_days(client, monkeypatch):
    """days=30 요청 → 마지막 30일만."""
    _stub_pykrx_ohlcv(monkeypatch, n_days=200)
    r = client.get("/api/paper/chart-data/005930?days=30")
    d = r.json()
    assert d["n"] == 30


def test_chart_data_handles_pykrx_failure(client, monkeypatch):
    _stub_pykrx_ohlcv(monkeypatch, fail=True)
    r = client.get("/api/paper/chart-data/005930")
    assert r.status_code == 410


def test_chart_data_open_high_low_close_match(client, monkeypatch):
    """OHLC 값이 fake DataFrame과 일치."""
    _stub_pykrx_ohlcv(monkeypatch, n_days=10)
    r = client.get("/api/paper/chart-data/005930?days=10")
    d = r.json()
    # 첫 행은 80000, 80500, 79500, 80200
    assert d["opens"][0] == 80000.0
    assert d["highs"][0] == 80500.0
    assert d["lows"][0] == 79500.0
    assert d["closes"][0] == 80200.0


# ─── 엔드포인트 발견성 ──────────────────────────────


def test_all_paper_endpoints_exist(client):
    """기존 v3.18 엔드포인트도 정상 동작."""
    for path in (
        "/api/paper/portfolios",
        "/api/paper/slots",
        "/api/paper/positions",
        "/api/paper/trades",
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path} 실패: {r.status_code}"


def test_buy_flow_still_works(client):
    """v3.18 BC — kium-scan 추가에도 매수 정상."""
    body = {
        "slot": "콴텍", "ticker": "005930", "name": "삼성전자",
        "quantity": 1, "price": 80_000,
    }
    r = client.post("/api/paper/buy", json=body)
    assert r.status_code == 200
    assert r.json()["ok"] is True



# ═══════════════════════════════════════════════════════════════
# v3.23 — 콴텍봇 탭 + /api/paper/quant-* 엔드포인트
# ═══════════════════════════════════════════════════════════════


def _stub_quant_snapshot(monkeypatch, consensus="Expansion"):
    """quant_bot.snapshot을 가짜 PhaseSnapshot으로 monkeypatch."""
    import quant_bot
    snap = quant_bot.PhaseSnapshot(
        phase_kr="Expansion", phase_us="Expansion",
        cli_kr_level=101.5, cli_kr_momentum=0.42,
        cli_us_level=102.0, cli_us_momentum=0.31,
        bsi_trend=0.15,
        consensus_phase=consensus, confidence=0.85,
        needs_recheck=False,
    )
    monkeypatch.setattr(quant_bot, "snapshot", lambda months=24: snap)
    return snap


def _stub_quant_recommend_data(monkeypatch):
    """recommend_top_n + 데이터 fetch 일괄 monkeypatch."""
    import quant_bot
    monkeypatch.setattr(quant_bot, "fetch_fundamentals",
                        lambda market="KOSPI": {})
    monkeypatch.setattr(quant_bot, "fetch_market_caps",
                        lambda market="KOSPI": {})
    fake_recs = [
        quant_bot.StockRecommendation(
            ticker="005930", name="삼성전자", composite_score=1.2,
            raw_factors={"Momentum": 0.3},
            z_factors={"Momentum": 1.5, "Value": 0.5, "Quality": 0.8,
                       "LowVol": 0.2, "Size": -0.3},
            current_price=80000.0,
        ),
        quant_bot.StockRecommendation(
            ticker="000660", name="SK하이닉스", composite_score=0.8,
            raw_factors={"Momentum": 0.2},
            z_factors={"Momentum": 1.0, "Value": -0.3, "Quality": 1.2,
                       "LowVol": -0.1, "Size": -0.2},
            current_price=120000.0,
        ),
    ]
    monkeypatch.setattr(quant_bot, "recommend_top_n",
                        lambda **kw: fake_recs[:int(kw.get("top_n", 8))])
    return fake_recs


def test_quant_snapshot_endpoint(client, monkeypatch):
    _stub_quant_snapshot(monkeypatch)
    r = client.get("/api/paper/quant-snapshot")
    assert r.status_code == 200
    d = r.json()
    assert d["consensus_phase"] == "Expansion"
    assert d["confidence"] == 0.85
    assert "summary_text" in d
    assert "한국 CLI" in d["summary_text"]


def test_quant_snapshot_clamps_months(client, monkeypatch):
    _stub_quant_snapshot(monkeypatch)
    r = client.get("/api/paper/quant-snapshot?months=10")
    assert r.status_code == 200
    assert r.json()["months"] == 18  # clamp 18


def test_quant_recommend_endpoint(client, monkeypatch):
    _stub_quant_snapshot(monkeypatch)
    _stub_quant_recommend_data(monkeypatch)
    r = client.get("/api/paper/quant-recommend?top_n=2")
    assert r.status_code == 200
    d = r.json()
    assert d["phase"] == "Expansion"
    assert len(d["recommendations"]) == 2
    first = d["recommendations"][0]
    assert first["ticker"] == "005930"
    assert "composite_score" in first
    assert "z_factors" in first
    assert d["weights"] is not None


def test_quant_recommend_phase_override(client, monkeypatch):
    _stub_quant_snapshot(monkeypatch)
    _stub_quant_recommend_data(monkeypatch)
    r = client.get("/api/paper/quant-recommend?phase_override=Slowdown&top_n=1")
    d = r.json()
    assert d["phase"] == "Slowdown"


def test_quant_recommend_no_consensus_returns_error_field(client, monkeypatch):
    """consensus_phase=None → error 필드."""
    _stub_quant_snapshot(monkeypatch, consensus=None)
    import quant_bot
    monkeypatch.setattr(quant_bot, "fetch_fundamentals", lambda market="KOSPI": {})
    monkeypatch.setattr(quant_bot, "fetch_market_caps", lambda market="KOSPI": {})
    r = client.get("/api/paper/quant-recommend")
    d = r.json()
    assert d["phase"] is None
    assert d["recommendations"] == []
    assert "error" in d


def test_quant_recommend_invalid_phase_override_uses_consensus(client, monkeypatch):
    """phase_override가 PHASES 외면 consensus 사용."""
    _stub_quant_snapshot(monkeypatch)
    _stub_quant_recommend_data(monkeypatch)
    r = client.get("/api/paper/quant-recommend?phase_override=Sideways&top_n=1")
    d = r.json()
    assert d["phase"] == "Expansion"  # consensus


def test_index_html_has_quant_tab_content(client):
    """탭 4 콘텐츠 div + 핵심 컨트롤 요소 존재."""
    r = client.get("/")
    html = r.text
    assert 'id="tab-quant"' in html
    assert 'btn-quant-recommend' in html
    assert 'quant-phase-override' in html
    assert 'loadQuantPhase' in html  # JS 함수
    assert 'runQuantRecommend' in html


def test_index_html_quant_endpoints_referenced(client):
    """JS가 새 엔드포인트 호출."""
    r = client.get("/")
    html = r.text
    assert "/api/paper/quant-snapshot" in html
    assert "/api/paper/quant-recommend" in html


def test_health_version(client):
    """version 갱신 — v3.35 (IPO 탭 연동 버그 수정)."""
    r = client.get("/api/health")
    assert r.json()["version"] == "v3.35"


# ═══════════════════════════════════════════════════════════════
# v3.35 — IPO봇 탭 + /api/ipo/* 엔드포인트
# ═══════════════════════════════════════════════════════════════


def test_ipo_records_empty_initially(client):
    r = client.get("/api/ipo/records")
    assert r.status_code == 200
    assert r.json() == []


def test_ipo_stats_empty_initially(client):
    r = client.get("/api/ipo/stats")
    assert r.status_code == 200
    assert r.json() == []


def test_ipo_subscribe_creates_record(client):
    body = {
        "name": "테스트공모주", "sub_start": "20260601", "sub_end": "20260602",
        "listing_date": "20260610", "grade": "A+", "score": 78.5,
        "factors": {"final_price": 15000, "offer_price": 15000},
        "subscribed": True, "alloc_amount": 1_000_000,
    }
    r = client.post("/api/ipo/subscribe", json=body)
    assert r.status_code == 200
    rec = r.json()
    assert rec["name"] == "테스트공모주"
    assert rec["grade"] == "A+"
    assert rec["subscribed"] == 1
    recs = client.get("/api/ipo/records").json()
    assert len(recs) == 1
    assert recs[0]["name"] == "테스트공모주"


def test_ipo_close_computes_return_pct(client):
    client.post("/api/ipo/subscribe", json={
        "name": "상장테스트", "grade": "A",
        "factors": {"final_price": 10000, "offer_price": 10000},
        "subscribed": True,
    })
    r = client.post("/api/ipo/close", json={
        "name": "상장테스트", "listing_price": 13000,
    })
    assert r.status_code == 200
    rec = r.json()
    assert rec["listing_price"] == 13000
    assert rec["return_pct"] == 30.0   # (13000-10000)/10000*100


def test_ipo_close_unknown_name_returns_404(client):
    r = client.post("/api/ipo/close", json={
        "name": "존재하지않는종목", "listing_price": 10000,
    })
    assert r.status_code == 404


def test_ipo_stats_aggregates_by_grade(client):
    client.post("/api/ipo/subscribe", json={
        "name": "통계종목", "grade": "A",
        "factors": {"final_price": 10000, "offer_price": 10000},
        "subscribed": True,
    })
    client.post("/api/ipo/close", json={
        "name": "통계종목", "listing_price": 12000,
    })
    stats = client.get("/api/ipo/stats").json()
    assert len(stats) == 1
    assert stats[0]["grade"] == "A"
    assert stats[0]["n"] == 1
    assert stats[0]["avg_return"] == 20.0
    assert stats[0]["n_pos"] == 1


def test_ipo_scan_endpoint_mocked(client, monkeypatch):
    """/api/ipo/scan — ipo_bot.py subprocess 호출을 가짜 JSON으로 대체."""
    import subprocess
    import json as _json
    fake_items = [{
        "name": "스캔공모주", "corp_name": "스캔공모주", "grade": "A+",
        "total_score": 78.0, "sub_start": "20260601", "sub_end": "20260602",
        "listing_date": "20260610", "final_price": None,
        "band_low": 12000, "band_high": 15000, "offer_band_high": 15000,
        "competition_rate": 900, "underwriter": "미래에셋증권",
    }]

    class _FakeProc:
        returncode = 0
        stdout = _json.dumps(fake_items)
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc())
    r = client.get("/api/ipo/scan")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["total_score"] == 78.0
    assert items[0]["band_low"] == 12000


def test_index_html_has_ipo_tab(client):
    html = client.get("/").text
    assert 'data-tab="tab-ipo"' in html
    assert 'id="tab-ipo"' in html
    assert "ipoScan" in html


def test_index_html_ipo_uses_total_score(client):
    """v3.35 버그 수정 — 스캔 렌더링이 it.total_score 사용 (이전: 미정의 it.score)."""
    html = client.get("/").text
    assert "it.total_score" in html


def test_index_html_ipo_band_display(client):
    """v3.35 — 확정가 미정 시 band_low~band_high 표시 (v3.34 밴드 보완 연동)."""
    html = client.get("/").text
    assert "it.band_low" in html


def test_index_html_ipo_no_undefined_showtoast(client):
    """v3.35 버그 수정 — 정의되지 않은 showToast() 호출 제거."""
    html = client.get("/").text
    assert "showToast" not in html
