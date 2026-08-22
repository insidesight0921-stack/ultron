#!/usr/bin/env python3
"""
Paper Trading UI — 5단계 검증 사이트 골격 (v3.18 MVP).

브라우저 http://localhost:8080.

기능 (MVP):
- 슬롯별 자본·포지션 대시보드
- 종목 검색 + 현재가 조회 (pykrx)
- 수동 매수/매도 폼
- 거래 내역 (최근 100건)

미구현 (v3.19+):
- TradingView 차트 위젯
- 키움봇 모멘텀 신호 탭
- 검증 대시보드 (성과·MDD·샤프)

사용:
    python paper_ui.py                   # 기본: localhost:8080
    # 원격 접속은 인증을 적용한 뒤 Tailscale IPv4를 명시적으로 지정한다.
    python paper_ui.py --host <Tailscale IPv4>
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

HOME = Path.home()
PROJECT = HOME / "울트론" / "ai-agent"

# .env 로드 — ECOS/FRED/DART API 키 등
try:
    from dotenv import load_dotenv as _ldenv
    _ldenv(PROJECT / ".env")
except Exception:
    pass

sys.path.insert(0, str(PROJECT / "scripts"))
from storage_paths import PATHS  # noqa: E402
import paper_db as pdb  # noqa: E402
from kium_bot import run as kium_run  # noqa: E402  # v3.19 신호 탭
import quant_bot as qb  # noqa: E402  # v3.23 콴텍 탭

log = logging.getLogger("paper_ui")


# 앱 시작 시 시드
pdb.ensure_seed()

# v3.47 — 나만의 퀀트 슬롯 보장(초기 자본 1,000만 · 필요시 DB에서 조정)
MYQUANT_SLOT = "마이퀀트"
try:
    pdb.ensure_slot(MYQUANT_SLOT)
except Exception:
    log.exception("마이퀀트 슬롯 생성 실패")


app = FastAPI(title="Paper Trading UI")



# ─── IPO봇 API (v3.29) ────────────────────────────────────────────────────────

@app.get("/api/ipo/scan")
async def api_ipo_scan():
    """ipo_bot scan 실행 → JSON 반환."""
    import subprocess, json as _json, sys
    try:
        result = subprocess.run(
            [sys.executable, str(PROJECT / "scripts" / "ipo_bot.py"), "scan", "--json"],
            capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT)
        )
        if result.returncode == 0 and result.stdout.strip():
            return JSONResponse(_json.loads(result.stdout))
        return JSONResponse({"error": result.stderr[:500] or "scan 실패"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/ipo/records")
async def api_ipo_records():
    return JSONResponse(pdb.ipo_list())


@app.post("/api/ipo/subscribe")
async def api_ipo_subscribe(req: Request):
    """페이퍼 청약 등록/토글."""
    body = await req.json()
    result = pdb.ipo_upsert(
        name=body["name"],
        sub_start=body.get("sub_start"),
        sub_end=body.get("sub_end"),
        listing_date=body.get("listing_date"),
        grade=body.get("grade"),
        score=body.get("score"),
        factors=body.get("factors"),
        subscribed=body.get("subscribed", True),
        alloc_amount=body.get("alloc_amount"),
    )
    return JSONResponse(result)


@app.post("/api/ipo/close")
async def api_ipo_close(req: Request):
    """상장 결과 입력 — listing_price 기록 + 수익률 계산."""
    body = await req.json()
    result = pdb.ipo_close(
        name=body["name"],
        listing_price=float(body["listing_price"]),
    )
    if result is None:
        return JSONResponse({"error": "종목을 찾을 수 없습니다"}, status_code=404)
    return JSONResponse(result)


@app.get("/api/ipo/stats")
async def api_ipo_stats():
    return JSONResponse(pdb.ipo_stats())


@app.get("/api/paper/portfolios")
async def api_portfolios():
    return JSONResponse(pdb.list_portfolios())


@app.get("/api/paper/slots")
async def api_slots():
    slots = pdb.list_slots()
    # 슬롯별 요약 보강
    out = []
    for s in slots:
        summary = pdb.slot_summary(s["id"]) or {}
        out.append({
            **s,
            "n_positions": summary.get("n_positions", 0),
            "n_trades": summary.get("n_trades", 0),
        })
    return JSONResponse(out)


@app.get("/api/paper/positions")
async def api_positions(slot: str | None = None):
    return JSONResponse(pdb.list_positions(slot=slot))


@app.get("/api/paper/trades")
async def api_trades(slot: str | None = None, limit: int = 100):
    return JSONResponse(pdb.list_trades(slot=slot, limit=limit))


@app.get("/api/paper/quote/{ticker}")
async def api_quote(ticker: str):
    """pykrx 현재가 (최근 영업일 종가). 외부 호출 실패 시 410."""
    try:
        from pykrx import stock
        from datetime import datetime, timedelta
        end = datetime.now()
        start = end - timedelta(days=10)
        df = stock.get_market_ohlcv(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), str(ticker).strip()
        )
        if df is None or len(df) == 0:
            return JSONResponse({"error": "데이터 없음"}, status_code=404)
        for col in ("종가", "Close", "close"):
            if col in df.columns:
                price = float(df[col].iloc[-1])
                # 종목명도 같이
                try:
                    name = stock.get_market_ticker_name(str(ticker).strip())
                except Exception:
                    name = str(ticker)
                return JSONResponse({
                    "ticker": str(ticker), "name": name, "price": price,
                })
        return JSONResponse({"error": "종가 컬럼 없음"}, status_code=500)
    except Exception as e:
        log.warning(f"quote 실패 {ticker}: {e}")
        return JSONResponse({"error": str(e)}, status_code=410)


@app.get("/api/paper/chart-data/{ticker}")
async def api_chart_data(ticker: str, days: int = 180):
    """v3.20: pykrx OHLCV → JSON. Plotly 캔들차트 데이터.

    응답: {ticker, name, dates, opens, highs, lows, closes, volumes}
    days 기본 180 (~6개월). 실패 시 410.
    """
    try:
        from pykrx import stock
        from datetime import datetime, timedelta
        end = datetime.now()
        start = end - timedelta(days=int(days * 1.6) + 14)  # 비영업일 여유
        df = stock.get_market_ohlcv(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
            str(ticker).strip()
        )
        if df is None or len(df) == 0:
            return JSONResponse({"error": "데이터 없음"}, status_code=404)

        # 컬럼 — 한글/영어 모두 지원
        col_map = {}
        for src_col, dst_col in [("시가", "open"), ("고가", "high"),
                                  ("저가", "low"), ("종가", "close"),
                                  ("거래량", "volume"),
                                  ("Open", "open"), ("High", "high"),
                                  ("Low", "low"), ("Close", "close"),
                                  ("Volume", "volume")]:
            if src_col in df.columns and dst_col not in col_map:
                col_map[dst_col] = src_col
        if not all(k in col_map for k in ("open", "high", "low", "close")):
            return JSONResponse(
                {"error": f"OHLC 컬럼 없음 — {list(df.columns)}"},
                status_code=500,
            )

        # 최근 days 개로 잘라내기
        df = df.tail(int(days))

        try:
            name = stock.get_market_ticker_name(str(ticker).strip())
        except Exception:
            name = str(ticker)

        return JSONResponse({
            "ticker": str(ticker), "name": name,
            "dates": [d.strftime("%Y-%m-%d") for d in df.index],
            "opens": [float(x) for x in df[col_map["open"]]],
            "highs": [float(x) for x in df[col_map["high"]]],
            "lows": [float(x) for x in df[col_map["low"]]],
            "closes": [float(x) for x in df[col_map["close"]]],
            "volumes": (
                [float(x) for x in df[col_map["volume"]]]
                if "volume" in col_map else []
            ),
            "n": len(df),
        })
    except Exception as e:
        log.warning(f"chart-data 실패 {ticker}: {e}")
        return JSONResponse({"error": str(e)}, status_code=410)


@app.post("/api/paper/buy")
async def api_buy(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    slot = body.get("slot")
    ticker = (body.get("ticker") or "").strip()
    name = body.get("name")
    quantity = body.get("quantity")
    price = body.get("price")
    notes = body.get("notes")

    if not slot or not ticker or quantity is None or price is None:
        return JSONResponse(
            {"error": "필수 인자: slot, ticker, quantity, price"},
            status_code=400,
        )

    try:
        out = pdb.record_buy(
            slot=slot, ticker=ticker, name=name,
            quantity=int(quantity), price=float(price),
            notes=notes,
        )
        return JSONResponse({"ok": True, **out})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        log.exception("buy 실패")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/paper/sell")
async def api_sell(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    slot = body.get("slot")
    ticker = (body.get("ticker") or "").strip()
    quantity = body.get("quantity")
    price = body.get("price")
    notes = body.get("notes")

    if not slot or not ticker or quantity is None or price is None:
        return JSONResponse(
            {"error": "필수 인자: slot, ticker, quantity, price"},
            status_code=400,
        )

    try:
        out = pdb.record_sell(
            slot=slot, ticker=ticker,
            quantity=int(quantity), price=float(price),
            notes=notes,
        )
        return JSONResponse({"ok": True, **out})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        log.exception("sell 실패")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/paper/quant-snapshot")
async def api_quant_snapshot(months: int = 24):
    """v3.23: 콴텍봇 거시 국면 스냅샷 (LLM 무호출, 결정론).

    응답: {phase_kr, phase_us, cli_kr_level, ..., consensus_phase, confidence,
           needs_recheck, summary_text}
    """
    try:
        if months < 18:
            months = 18
        elif months > 60:
            months = 60
        snap = qb.snapshot(months=months)
        return JSONResponse({
            "phase_kr": snap.phase_kr,
            "phase_us": snap.phase_us,
            "cli_kr_level": snap.cli_kr_level,
            "cli_kr_momentum": snap.cli_kr_momentum,
            "cli_us_level": snap.cli_us_level,
            "cli_us_momentum": snap.cli_us_momentum,
            "bsi_trend": snap.bsi_trend,
            "consensus_phase": snap.consensus_phase,
            "confidence": snap.confidence,
            "needs_recheck": snap.needs_recheck,
            "summary_text": qb.format_snapshot(snap),
            "months": months,
        })
    except Exception as e:
        log.exception("quant-snapshot 실패")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/paper/quant-recommend")
async def api_quant_recommend(
    top_n: int | None = None,
    market: str = "KOSPI200",
    phase_override: str | None = None,
    months: int = 24,
):
    """v3.23: 콴텍봇 phase별 Top N 종목 추천. 결정론 z-score 가중합.

    응답: {phase, confidence, weights, recommendations: [...], snapshot: {...}}
    """
    try:
        if months < 18:
            months = 18
        elif months > 60:
            months = 60
        snap = qb.snapshot(months=months)
        phase = (
            phase_override if phase_override in qb.PHASES
            else snap.consensus_phase
        )
        if not phase:
            return JSONResponse({
                "phase": None,
                "confidence": snap.confidence,
                "needs_recheck": snap.needs_recheck,
                "weights": None,
                "recommendations": [],
                "error": "거시 국면 미확정 — ECOS/FRED 키 또는 네트워크 점검",
                "snapshot": {
                    "phase_kr": snap.phase_kr, "phase_us": snap.phase_us,
                    "summary_text": qb.format_snapshot(snap),
                },
            })
        weights_all = qb.parse_phase_weights_from_wiki()
        recs = qb.recommend_top_n(
            phase=phase, market=market, top_n=top_n, weights=weights_all,
        )
        return JSONResponse({
            "phase": phase,
            "confidence": snap.confidence,
            "needs_recheck": snap.needs_recheck,
            "weights": weights_all.get(phase),
            "recommendations": [
                {
                    "ticker": r.ticker, "name": r.name,
                    "composite_score": r.composite_score,
                    "current_price": r.current_price,
                    "z_factors": r.z_factors,
                    "raw_factors": r.raw_factors,
                }
                for r in recs
            ],
            "snapshot": {
                "phase_kr": snap.phase_kr, "phase_us": snap.phase_us,
                "summary_text": qb.format_snapshot(snap),
            },
            "market": market,
            "phase_override": phase_override,
        })
    except Exception as e:
        log.exception("quant-recommend 실패")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/paper/kium-scan")
async def api_kium_scan(top_n: int = 10, market: str = "KOSPI200",
                       with_crash_signals: bool = True):
    """v3.19: 키움봇 모멘텀 스캔 + 크래시 감지를 paper_ui에서 직접 호출.

    응답: {"results": [...], "crash_signals": {...}, "weight": {...}}
    또는 raw 텍스트 출력의 구조화 버전 (UI가 표·경고·비중 분리 렌더링).
    """
    try:
        from kium_bot import scan_universe, fetch_kospi_close, \
            fetch_vkospi_latest, detect_crash_signals, \
            compute_weight_recommendation
        results = scan_universe(market=market, top_n=int(top_n))
        crash = None
        weight = None
        if with_crash_signals:
            try:
                kospi_close = fetch_kospi_close()
                crash = detect_crash_signals(
                    kospi_close=kospi_close, top_results=results
                )
                vkospi_v = fetch_vkospi_latest()
                weight = compute_weight_recommendation(
                    vkospi=vkospi_v, kospi_close=kospi_close
                )
            except Exception as e:
                log.warning(f"crash_signals 계산 실패 — 결과만: {e}")
        return JSONResponse({
            "results": results,
            "crash_signals": crash,
            "weight": weight,
            "market": market,
            "top_n": top_n,
        })
    except Exception as e:
        log.exception("kium-scan 실패")
        return JSONResponse(
            {"error": str(e)}, status_code=500
        )




# ─── 나만의 퀀트 (v3.47) ─────────────────────────────


@app.get("/api/paper/myquant-scan")
async def api_myquant_scan(market: str = "1028", refresh: bool = False):
    """entry_backtest 진입 조건으로 현재 유니버스 스캔(당일 캐시, 수 분 소요 가능).

    응답: {"items": [{ticker,name,price,matched,features}], "slot": "마이퀀트"}
    """
    try:
        import entry_backtest as eb
        if refresh:
            from datetime import datetime
            from pathlib import Path as _P
            cpath = (PATHS.private_state_dir /
                     f"myquant_scan_{datetime.now().strftime('%Y%m%d')}.json")
            cpath.unlink(missing_ok=True)
        items = eb.scan_current(market_code=market)
        return JSONResponse({"items": items, "slot": MYQUANT_SLOT,
                             "conditions": list(eb.SCAN_CONDITIONS)})
    except Exception as e:
        log.exception("myquant-scan 실패")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/paper/myquant-tags")
async def api_myquant_tags():
    """마이퀀트 태그별 실현 성과 (buy notes의 MQ[...] 기준)."""
    try:
        import trade_analytics as ta
        rts = ta.compute_roundtrips(pdb.list_trades(limit=100000))
        return JSONResponse({"tags": ta.tag_performance(rts),
                             "text": ta.format_tag_performance(rts)})
    except Exception as e:
        log.exception("myquant-tags 실패")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/performance")
async def api_performance():
    """v3.31: 슬롯별 실현 성과 통계 (승률·수익률·MDD·샤프)."""
    try:
        return JSONResponse(pdb.performance_stats())
    except Exception as e:
        log.exception("performance 조회 실패")
        return JSONResponse({"error": str(e)}, status_code=500)

# ─── HTML 페이지 ────────────────────────────────────


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📑 Paper Trading</title>
<style>
  :root {
    --bg: #0f1419; --fg: #e6e6e6; --muted: #888;
    --card: #1a2028; --border: #2a3038;
    --accent: #4ea1ff; --buy: #00c853; --sell: #ff5252;
    --warn: #ffb300;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Apple SD Gothic Neo', system-ui, sans-serif;
         background: var(--bg); color: var(--fg); margin: 0; padding: 1rem; }
  h1, h2, h3 { margin: 0.5rem 0; }
  .container { max-width: 1100px; margin: 0 auto; }
  .grid { display: grid; gap: 1rem; }
  .grid-3 { grid-template-columns: repeat(3, 1fr); }
  .grid-2 { grid-template-columns: repeat(2, 1fr); }
  @media (max-width: 760px) { .grid-3, .grid-2 { grid-template-columns: 1fr; } }
  .card { background: var(--card); border: 1px solid var(--border);
          border-radius: 8px; padding: 1rem; }
  .slot-card { display: flex; justify-content: space-between; align-items: baseline; }
  .slot-name { font-weight: bold; }
  .slot-capital { color: var(--accent); font-size: 1.2rem; }
  .muted { color: var(--muted); font-size: 0.9rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
  th, td { padding: 0.4rem 0.6rem; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: normal; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  form { display: grid; gap: 0.5rem; }
  form .row { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; }
  input, select, button {
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 4px;
    padding: 0.5rem; font-size: 0.95rem;
  }
  button { cursor: pointer; font-weight: bold; }
  .btn-buy { background: var(--buy); color: white; border: none; }
  .btn-sell { background: var(--sell); color: white; border: none; }
  .btn-quote { background: var(--accent); color: white; border: none; }
  .btn-scan { background: var(--accent); color: white; border: none; padding: 0.6rem 1rem; }
  .toast { position: fixed; bottom: 1rem; right: 1rem;
           background: var(--card); border: 1px solid var(--border);
           padding: 0.5rem 1rem; border-radius: 4px; opacity: 0; transition: opacity 0.3s; }
  .toast.show { opacity: 1; }
  .toast.error { border-color: var(--sell); }
  .badge-buy { color: var(--buy); }
  .badge-sell { color: var(--sell); }
  /* 탭 */
  .tabs { display: flex; gap: 0.5rem; border-bottom: 1px solid var(--border);
          margin: 1rem 0 0; }
  .tab { background: none; border: none; color: var(--muted);
         padding: 0.6rem 1rem; cursor: pointer; font-size: 0.95rem;
         border-bottom: 2px solid transparent; }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-content { display: none; padding-top: 1rem; }
  .tab-content.active { display: block; }
  /* 차트 */
  #tv-chart-container { width: 100%; height: 540px;
                        background: var(--card); border: 1px solid var(--border);
                        border-radius: 8px; }
  /* 신호 */
  .crash-warn { background: var(--warn); color: #000;
                padding: 0.5rem 1rem; border-radius: 4px; font-weight: bold; }
  .crash-ok { background: var(--buy); color: white;
              padding: 0.5rem 1rem; border-radius: 4px; }
  .crash-mid { background: var(--accent); color: white;
               padding: 0.5rem 1rem; border-radius: 4px; }
  .signal-row td { cursor: pointer; }
  .signal-row:hover td { background: var(--border); }
  .weight-line { color: var(--accent); font-size: 1rem; margin: 0.4rem 0; }
</style>
</head>
<body>
<div class="container">
  <h1>📑 Paper Trading</h1>
  <p class="muted">실주문 안 함 · 가상 매매 · 6개월 검증 후 KIS 입문</p>

  <h2>슬롯</h2>
  <div id="slots" class="grid grid-3"></div>

  <div class="tabs">
    <button class="tab active" data-tab="tab-order">📝 주문</button>
    <button class="tab" data-tab="tab-chart">📈 차트</button>
    <button class="tab" data-tab="tab-signals">📊 키움봇 신호</button>
    <button class="tab" data-tab="tab-quant">🌐 콴텍봇</button>
    <button class="tab" data-tab="tab-ipo">🏷️ IPO봇</button>
    <button class="tab" data-tab="tab-myquant">🧪 나만의 퀀트</button>
    <button class="tab" data-tab="tab-perf">📊 성과</button>
  </div>

  <!-- 탭 1: 주문 -->
  <div id="tab-order" class="tab-content active">
    <div class="card">
      <form id="order-form">
        <div class="row">
          <select name="slot" id="slot-select" required></select>
          <div style="display: flex; gap: 0.5rem;">
            <input type="text" name="ticker" id="ticker" placeholder="종목 (005930)" required style="flex:1">
            <button type="button" class="btn-quote" id="btn-quote">현재가</button>
          </div>
        </div>
        <input type="text" name="name" id="name" placeholder="종목명 (자동 채움)">
        <div class="row">
          <input type="number" name="quantity" id="quantity" placeholder="수량" min="1" required>
          <input type="number" name="price" id="price" placeholder="단가" min="1" step="any" required>
        </div>
        <input type="text" name="notes" id="notes" placeholder="메모 (선택)">
        <div class="row">
          <button type="button" class="btn-buy" id="btn-buy">🟢 매수</button>
          <button type="button" class="btn-sell" id="btn-sell">🔴 매도</button>
        </div>
      </form>
    </div>

    <h3 style="margin-top: 1.5rem;">보유 포지션</h3>
    <div id="positions" class="card"><table><thead>
      <tr><th>슬롯</th><th>종목</th><th class="num">수량</th><th class="num">평균가</th></tr>
    </thead><tbody></tbody></table></div>

    <h3 style="margin-top: 1.5rem;">거래 내역 (최근 100건)</h3>
    <div id="trades" class="card"><table><thead>
      <tr><th>시각</th><th>슬롯</th><th>종목</th><th>방향</th>
          <th class="num">수량</th><th class="num">단가</th><th class="num">수수료</th></tr>
    </thead><tbody></tbody></table></div>
  </div>

  <!-- 탭 2: 차트 -->
  <div id="tab-chart" class="tab-content">
    <div class="card">
      <div style="display: flex; gap: 0.5rem; margin-bottom: 0.8rem;">
        <input type="text" id="chart-ticker" placeholder="티커 입력 (예: 005930) 또는 신호 탭에서 클릭"
               style="flex:1">
        <button type="button" class="btn-quote" id="btn-chart-load">차트 보기</button>
      </div>
      <div id="tv-chart-container"></div>
      <p class="muted" style="margin-top: 0.6rem;">
        Plotly 캔들차트 · pykrx 일봉 (최근 ~6개월). 영업일 기준, 종가 기준 ~15분 지연.
      </p>
    </div>
  </div>

  <!-- 탭 3: 키움 신호 -->
  <div id="tab-signals" class="tab-content">
    <div class="card">
      <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
        <select id="scan-market">
          <option value="KOSPI200">KOSPI 200</option>
          <option value="KOSDAQ150">KOSDAQ 150</option>
          <option value="KOSPI200+KOSDAQ150">KOSPI200+KOSDAQ150</option>
        </select>
        <input type="number" id="scan-topn" value="10" min="1" max="50" style="width: 80px">
        <label class="muted"><input type="checkbox" id="scan-crash" checked> 크래시 감지</label>
        <button type="button" class="btn-scan" id="btn-scan">🔍 스캔 실행</button>
        <span id="scan-status" class="muted"></span>
      </div>
      <div id="scan-summary" style="margin-top: 1rem;"></div>
      <table style="margin-top: 1rem;">
        <thead><tr>
          <th>#</th><th>종목</th><th class="num">점수</th>
          <th class="num">최근1M</th><th class="num">최근12M</th><th class="num">현재가</th>
        </tr></thead>
        <tbody id="scan-tbody">
          <tr><td colspan="6" class="muted">스캔 버튼 클릭 → KOSPI 200 종목 12-1 모멘텀 Top N (수십 초 소요)</td></tr>
        </tbody>
      </table>
      <p class="muted" style="margin-top: 0.6rem;">
        ⚠️ 생존편향 주의 — 현재 상장 종목 기준 (1~2%p 과대 추정 가능). 종목 클릭 시 매수 폼에 자동 채움.
      </p>
    </div>
  </div>

  <!-- 탭 4: 콴텍봇 (v3.23) -->
  <div id="tab-quant" class="tab-content">
    <div class="card">
      <div id="quant-phase-card" style="margin-bottom: 1rem;">
        <p class="muted">⏳ 거시 국면 로딩 중...</p>
      </div>
      <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
        <select id="quant-market">
          <option value="KOSPI200">KOSPI 200</option>
          <option value="KOSDAQ150">KOSDAQ 150</option>
          <option value="KOSPI200+KOSDAQ150">KOSPI200+KOSDAQ150</option>
        </select>
        <select id="quant-phase-override">
          <option value="">자동 (거시 국면 따라)</option>
          <option value="Recovery">Recovery (강제)</option>
          <option value="Expansion">Expansion (강제)</option>
          <option value="Slowdown">Slowdown (강제)</option>
          <option value="Contraction">Contraction (강제)</option>
        </select>
        <input type="number" id="quant-topn" placeholder="Top N (기본=phase별)" min="1" max="30" style="width: 130px">
        <button type="button" class="btn-scan" id="btn-quant-recommend">📈 종목 추천</button>
        <span id="quant-status" class="muted"></span>
      </div>
      <div id="quant-summary" style="margin-top: 1rem;"></div>
      <table style="margin-top: 1rem;">
        <thead><tr>
          <th>#</th><th>종목</th><th class="num">합산 점수</th>
          <th class="num">Mom</th><th class="num">Val</th><th class="num">Qua</th>
          <th class="num">Low</th><th class="num">Siz</th><th class="num">현재가</th>
        </tr></thead>
        <tbody id="quant-tbody">
          <tr><td colspan="9" class="muted">📈 종목 추천 버튼 클릭 → 5팩터 z-score 가중합 Top N (~수십 초)</td></tr>
        </tbody>
      </table>
      <p class="muted" style="margin-top: 0.6rem;">
        ⚠️ 결정론 z-score 가중합 · 가중치는 wiki/투자/퀀트/국면별_팩터_가중.md (사용자 직접 조정 가능). 
        매월 첫 영업일 09:30 텔레그램 자동 푸시. 종목 클릭 → 콴텍 슬롯 매수 폼 자동 채움.
      </p>
    </div>
  </div>

  <!-- 탭 5: IPO봇 (v3.29) -->
  <div id="tab-ipo" class="tab-content">
    <div class="card">
      <div style="display:flex;align-items:center;gap:1rem;margin-bottom:0.75rem;">
        <h3 style="margin:0;">📋 현재 스캔 결과</h3>
        <button onclick="ipoScan()" style="padding:0.3rem 0.8rem;font-size:0.85rem;">🔄 스캔</button>
        <span id="ipo-scan-status" style="font-size:0.8rem;color:var(--muted);"></span>
      </div>
      <table id="ipo-scan-table">
        <thead><tr>
          <th>등급</th><th>종목명</th><th>청약기간</th><th>상장일</th>
          <th class="num">공모가</th><th class="num">시총</th>
          <th class="num">점수</th><th>경쟁률</th><th>주관</th><th>청약</th>
        </tr></thead>
        <tbody id="ipo-scan-body"><tr><td colspan="10" style="text-align:center;color:var(--muted);">스캔 버튼을 눌러 데이터를 불러오세요</td></tr></tbody>
      </table>
    </div>

    <div class="card" style="margin-top:1rem;">
      <h3 style="margin-bottom:0.75rem;">📒 페이퍼 청약 기록</h3>
      <table id="ipo-records-table">
        <thead><tr>
          <th>등급</th><th>종목명</th><th>청약마감</th><th>상장일</th>
          <th class="num">점수</th><th>청약여부</th>
          <th class="num">공모가</th><th class="num">상장가</th>
          <th class="num">수익률</th><th>결과입력</th>
        </tr></thead>
        <tbody id="ipo-records-body"><tr><td colspan="10" style="text-align:center;color:var(--muted);">기록 없음</td></tr></tbody>
      </table>
    </div>

    <div class="card" style="margin-top:1rem;">
      <h3 style="margin-bottom:0.75rem;">📊 등급별 예측력 통계</h3>
      <table id="ipo-stats-table">
        <thead><tr>
          <th>등급</th><th class="num">건수</th><th class="num">평균수익률</th>
          <th class="num">최소</th><th class="num">최대</th><th class="num">양(+)건수</th>
        </tr></thead>
        <tbody id="ipo-stats-body"><tr><td colspan="6" style="text-align:center;color:var(--muted);">상장 완료 데이터 없음</td></tr></tbody>
      </table>
    </div>

    <!-- 상장가 입력 모달 -->
    <div id="ipo-close-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:999;align-items:center;justify-content:center;">
      <div style="background:var(--card-bg);border:1px solid var(--border);border-radius:8px;padding:1.5rem;width:320px;">
        <h4 style="margin:0 0 1rem;">📈 상장 결과 입력</h4>
        <p id="ipo-close-name" style="margin:0 0 0.5rem;font-weight:600;"></p>
        <label style="font-size:0.85rem;">상장가(원)</label>
        <input type="number" id="ipo-close-price" style="width:100%;margin:0.4rem 0 1rem;padding:0.4rem;" placeholder="예: 45000">
        <div style="display:flex;gap:0.5rem;justify-content:flex-end;">
          <button onclick="document.getElementById('ipo-close-modal').style.display='none'">취소</button>
          <button onclick="ipoCloseSubmit()" style="background:var(--accent);color:#fff;">저장</button>
        </div>
      </div>
    </div>
  </div>

</div>

  <!-- 탭 6.5: 나만의 퀀트 (v3.47) -->
  <div id="tab-myquant" class="tab-content">
    <div class="card">
      <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
        <button type="button" class="btn-scan" id="btn-myquant-scan">🔍 조건 스캔 (KOSPI200)</button>
        <label class="muted"><input type="checkbox" id="myquant-refresh"> 캐시 무시(재스캔)</label>
        <span id="myquant-status" class="muted"></span>
      </div>
      <p class="muted" style="margin-top: 0.4rem;">
        진입 조건(추세위+눌림 / 정배열 / 신고가돌파 / 과낙폭반등) 충족 종목.
        매수하면 선택 조건이 <b>MQ[태그]</b>로 기록돼 태그별 성과가 자동 축적됩니다.
        슬롯: 마이퀀트 · 청산은 공용 자동 손절/트레일링 적용.
      </p>
      <table style="margin-top: 1rem;">
        <thead><tr>
          <th>종목</th><th class="num">현재가</th><th>충족 조건</th>
          <th class="num">ma20</th><th class="num">dd20</th><th class="num">5일</th><th></th>
        </tr></thead>
        <tbody id="myquant-tbody">
          <tr><td colspan="7" class="muted">스캔 실행 → 조건 충족 종목 (당일 캐시, 첫 실행은 수 분)</td></tr>
        </tbody>
      </table>
    </div>
    <div class="card" style="margin-top: 1rem;">
      <h3 style="margin-top:0">🏷️ 태그별 실현 성과</h3>
      <pre id="myquant-tags" class="muted" style="white-space: pre-wrap;">아직 마이퀀트 거래가 없습니다.</pre>
      <p class="muted">※ 통계 관찰이며 투자 권유 아님. 표본 5건 미만(†)은 참고용.</p>
    </div>
  </div>

  <!-- 탭 6: 성과 통계 (v3.31) -->
  <div id="tab-perf" class="tab-content">
    <div class="card" style="margin-bottom:1rem;">
      <div style="display:flex;gap:0.5rem;align-items:center;">
        <button type="button" class="btn-scan" id="btn-perf-refresh">🔄 새로고침</button>
        <span id="perf-status" class="muted"></span>
      </div>
    </div>
    <div id="perf-grid" class="grid grid-3" style="margin-bottom:1.5rem;"></div>
    <div class="card">
      <h3 style="margin-top:0;">완결 거래 성과 상세</h3>
      <table>
        <thead><tr>
          <th>슬롯</th><th class="num">완결 거래</th><th class="num">승률</th>
          <th class="num">실현 손익</th><th class="num">수익률</th>
          <th class="num">MDD</th><th class="num">샤프</th>
          <th class="num">보유 종목</th><th class="num">미실현 매입액</th>
        </tr></thead>
        <tbody id="perf-tbody">
          <tr><td colspan="9" class="muted">새로고침 버튼 클릭</td></tr>
        </tbody>
      </table>
    </div>
    <p class="muted" style="margin-top:0.8rem;">
      ※ 승률·손익은 FIFO 매칭 완결 거래 기준. 샤프는 거래 단위 연환산(252회/년 가정).
    </p>
  </div>

<div id="toast" class="toast"></div>

<!-- Plotly (자체 캔들차트 렌더링 — TradingView 무료 위젯이 한국 주식 미지원) -->
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<script>
function $(id) { return document.getElementById(id); }
function toast(msg, isError) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show" + (isError ? " error" : "");
  setTimeout(() => { t.className = "toast"; }, 2500);
}
function fmt(n) { return Number(n).toLocaleString("ko-KR"); }

// ─── 탭 전환 ──────────────────────────────────────
document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    $(btn.dataset.tab).classList.add("active");
  });
});

async function loadSlots() {
  const r = await fetch("/api/paper/slots");
  const slots = await r.json();
  const sel = $("slot-select");
  sel.innerHTML = "";
  $("slots").innerHTML = "";
  for (const s of slots) {
    const opt = document.createElement("option");
    opt.value = s.name; opt.textContent = s.name;
    sel.appendChild(opt);
    const card = document.createElement("div");
    card.className = "card slot-card";
    card.innerHTML = `
      <div>
        <div class="slot-name">${s.name} (${(s.allocation_pct*100).toFixed(0)}%)</div>
        <div class="muted">포지션 ${s.n_positions} · 거래 ${s.n_trades}</div>
      </div>
      <div class="slot-capital">${fmt(Math.round(s.current_capital))}원</div>`;
    $("slots").appendChild(card);
  }
}

async function loadPositions() {
  const r = await fetch("/api/paper/positions");
  const positions = await r.json();
  const tbody = $("positions").querySelector("tbody");
  tbody.innerHTML = positions.length === 0
    ? "<tr><td colspan='4' class='muted'>(보유 포지션 없음)</td></tr>"
    : positions.map(p => `<tr>
        <td>${p.slot_name}</td><td>${p.name || ''} (${p.ticker})</td>
        <td class="num">${fmt(p.quantity)}</td>
        <td class="num">${fmt(Math.round(p.avg_price))}원</td>
      </tr>`).join("");
}

async function loadTrades() {
  const r = await fetch("/api/paper/trades?limit=100");
  const trades = await r.json();
  const tbody = $("trades").querySelector("tbody");
  tbody.innerHTML = trades.length === 0
    ? "<tr><td colspan='7' class='muted'>(거래 내역 없음)</td></tr>"
    : trades.map(t => `<tr>
        <td class="muted">${t.executed_at}</td>
        <td>${t.slot_name}</td>
        <td>${t.name || ''} (${t.ticker})</td>
        <td class="badge-${t.side}">${t.side === 'buy' ? '🟢 매수' : '🔴 매도'}</td>
        <td class="num">${fmt(t.quantity)}</td>
        <td class="num">${fmt(Math.round(t.price))}원</td>
        <td class="num">${fmt(Math.round(t.fees))}원</td>
      </tr>`).join("");
}

async function reload() {
  await Promise.all([loadSlots(), loadPositions(), loadTrades()]);
}

// ─── 현재가 + 매매 ─────────────────────────────────
$("btn-quote").addEventListener("click", async () => {
  const ticker = $("ticker").value.trim();
  if (!ticker) return toast("종목 입력 필요", true);
  try {
    const r = await fetch(`/api/paper/quote/${encodeURIComponent(ticker)}`);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || "조회 실패");
    }
    const d = await r.json();
    $("name").value = d.name || "";
    $("price").value = Math.round(d.price);
    toast(`${d.name} 현재가 ${fmt(Math.round(d.price))}원`);
  } catch (e) { toast("현재가 실패: " + e.message, true); }
});

async function submitOrder(side) {
  const body = {
    slot: $("slot-select").value,
    ticker: $("ticker").value.trim(),
    name: $("name").value,
    quantity: Number($("quantity").value),
    price: Number($("price").value),
    notes: $("notes").value || null,
  };
  if (!body.ticker || !body.quantity || !body.price)
    return toast("종목·수량·단가 필요", true);
  const r = await fetch(`/api/paper/${side}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = await r.json();
  if (d.ok) {
    toast(`${side === 'buy' ? '🟢 매수' : '🔴 매도'} 완료`);
    $("ticker").value = ""; $("name").value = "";
    $("quantity").value = ""; $("price").value = ""; $("notes").value = "";
    reload();
  } else {
    toast(`실패: ${d.error}`, true);
  }
}
$("btn-buy").addEventListener("click", () => submitOrder("buy"));
$("btn-sell").addEventListener("click", () => submitOrder("sell"));

// ─── Plotly 캔들차트 (v3.20 — TradingView 무료 위젯이 한국 주식 미지원) ─
async function loadChart(ticker) {
  const t = (ticker || "").trim();
  if (!t) return;
  const container = $("tv-chart-container");
  container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:540px;color:#888">⏳ 차트 로딩 중...</div>';
  try {
    const r = await fetch(`/api/paper/chart-data/${encodeURIComponent(t)}`);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || "조회 실패");
    }
    const d = await r.json();
    container.innerHTML = "";  // 로딩 메시지 제거
    const trace = {
      type: "candlestick",
      x: d.dates,
      open: d.opens, high: d.highs, low: d.lows, close: d.closes,
      increasing: { line: { color: "#00c853" }, fillcolor: "#00c853" },
      decreasing: { line: { color: "#ff5252" }, fillcolor: "#ff5252" },
      name: d.name,
    };
    const layout = {
      title: { text: `${d.name} (${d.ticker}) · ${d.n}일`, font: { color: "#e6e6e6" } },
      paper_bgcolor: "#1a2028", plot_bgcolor: "#1a2028",
      font: { color: "#e6e6e6" },
      xaxis: {
        gridcolor: "#2a3038", color: "#888",
        rangeslider: { visible: false },
        type: "category",  // 비영업일 갭 제거
      },
      yaxis: {
        gridcolor: "#2a3038", color: "#888",
        tickformat: ",d", side: "right",
      },
      margin: { t: 50, l: 20, r: 70, b: 40 },
      showlegend: false,
    };
    const config = { responsive: true, displaylogo: false,
                     modeBarButtonsToRemove: ["lasso2d", "select2d"] };
    Plotly.newPlot(container, [trace], layout, config);
  } catch (e) {
    container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:540px;color:#ff5252">❌ 차트 로딩 실패: ${e.message}</div>`;
  }
}
$("btn-chart-load").addEventListener("click", () => {
  loadChart($("chart-ticker").value);
});
$("chart-ticker").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); loadChart($("chart-ticker").value); }
});

// ─── 키움 신호 스캔 ───────────────────────────────
async function runScan() {
  const market = $("scan-market").value;
  const top_n = Number($("scan-topn").value) || 10;
  const with_crash = $("scan-crash").checked;
  $("scan-status").textContent = "⏳ 스캔 중... (수십 초 소요)";
  $("btn-scan").disabled = true;
  try {
    const url = `/api/paper/kium-scan?top_n=${top_n}&market=${encodeURIComponent(market)}&with_crash_signals=${with_crash}`;
    const r = await fetch(url);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || "스캔 실패");
    }
    const d = await r.json();
    renderScan(d);
    $("scan-status").textContent = `✅ ${d.results?.length || 0}건 (${market})`;
  } catch (e) {
    $("scan-status").textContent = "";
    toast("스캔 실패: " + e.message, true);
  } finally {
    $("btn-scan").disabled = false;
  }
}

function renderScan(d) {
  const summary = $("scan-summary");
  summary.innerHTML = "";
  if (d.crash_signals) {
    const c = d.crash_signals;
    let cls = c.hits >= 2 ? "crash-warn" : (c.hits === 1 ? "crash-mid" : "crash-ok");
    const div = document.createElement("div");
    div.className = cls;
    div.textContent = c.recommendation;
    summary.appendChild(div);
    const detail = document.createElement("div");
    detail.className = "muted";
    detail.style.marginTop = "0.4rem";
    const v = c.vol_spike, p = c.market_panic, r = c.momentum_reversal;
    const vF = v.hit ? "🚨" : "✓";
    const pF = p.hit ? "🚨" : "✓";
    const rF = r.hit ? "🚨" : "✓";
    detail.innerHTML =
      `${vF} 변동성 (21d/63d) ${v.ratio !== null ? v.ratio.toFixed(2) : "N/A"} · ` +
      `${pF} KOSPI 20d ${p.return_window !== null ? (p.return_window*100).toFixed(1)+"%" : "N/A"} · ` +
      `${rF} 모멘텀 1M평균 ${r.avg_return_1m !== null ? (r.avg_return_1m*100).toFixed(1)+"%" : "N/A"}`;
    summary.appendChild(detail);
  }
  if (d.weight) {
    const w = d.weight;
    const wd = document.createElement("div");
    wd.className = "weight-line";
    wd.textContent = `📐 권장 비중: 주식 ${(w.equity_weight*100).toFixed(0)}% / 채권 ${(w.bond_weight*100).toFixed(0)}%`;
    summary.appendChild(wd);
    const wr = document.createElement("div");
    wr.className = "muted";
    wr.textContent = w.reason || "";
    summary.appendChild(wr);
  }
  const tbody = $("scan-tbody");
  if (!d.results || d.results.length === 0) {
    tbody.innerHTML = "<tr><td colspan='6' class='muted'>(결과 없음)</td></tr>";
    return;
  }
  tbody.innerHTML = d.results.map((r, i) => `
    <tr class="signal-row" data-ticker="${r.ticker}" data-name="${r.name}" data-price="${Math.round(r.current_price)}">
      <td>${i+1}</td>
      <td>${r.name} (${r.ticker})</td>
      <td class="num">${(r.score*100).toFixed(1)}%</td>
      <td class="num">${r.return_1m !== null ? (r.return_1m*100).toFixed(1)+"%" : "N/A"}</td>
      <td class="num">${r.return_12m !== null ? (r.return_12m*100).toFixed(1)+"%" : "N/A"}</td>
      <td class="num">${fmt(Math.round(r.current_price))}원</td>
    </tr>
  `).join("");
  // 클릭 → 매수 폼 자동 채움 + 주문 탭으로
  document.querySelectorAll(".signal-row").forEach(row => {
    row.addEventListener("click", () => {
      $("ticker").value = row.dataset.ticker;
      $("name").value = row.dataset.name;
      $("price").value = row.dataset.price;
      // 주문 탭으로
      document.querySelector(".tab[data-tab='tab-order']").click();
      toast(`${row.dataset.name} 매수 폼 채움`);
    });
  });
}
$("btn-scan").addEventListener("click", runScan);

// ─── 콴텍봇 (v3.23) ─────────────────────────────
async function loadQuantPhase() {
  const card = $("quant-phase-card");
  card.innerHTML = '<p class="muted">⏳ 거시 국면 로딩 중...</p>';
  try {
    const r = await fetch("/api/paper/quant-snapshot");
    if (!r.ok) throw new Error("snapshot HTTP " + r.status);
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    const emoji = {Recovery:"🌱", Expansion:"🚀", Slowdown:"🌧️", Contraction:"🧊"}[d.consensus_phase] || "❓";
    const phase = d.consensus_phase || "(미확정)";
    const conf = (d.confidence * 100).toFixed(0) + "%";
    let cls = "crash-ok";
    if (d.needs_recheck) cls = "crash-mid";
    if (!d.consensus_phase) cls = "crash-warn";
    card.innerHTML = `
      <div class="${cls}">${emoji} 통합 국면 <b>${phase}</b> · 확신도 ${conf}${d.needs_recheck ? " ⚠️ 2주 재진단" : ""}</div>
      <div class="muted" style="margin-top: 0.4rem;">
        한국 CLI ${d.cli_kr_level !== null ? d.cli_kr_level.toFixed(2) : "N/A"}
        (${d.cli_kr_momentum !== null ? (d.cli_kr_momentum >= 0 ? "+" : "") + d.cli_kr_momentum.toFixed(3) : "N/A"})
        · 미국 CLI ${d.cli_us_level !== null ? d.cli_us_level.toFixed(2) : "N/A"}
        (${d.cli_us_momentum !== null ? (d.cli_us_momentum >= 0 ? "+" : "") + d.cli_us_momentum.toFixed(3) : "N/A"})
        · BSI 추세 ${d.bsi_trend !== null ? (d.bsi_trend >= 0 ? "+" : "") + d.bsi_trend.toFixed(3) : "N/A"}
      </div>`;
  } catch (e) {
    card.innerHTML = `<div class="crash-warn">❌ 거시 국면 실패: ${e.message}</div>`;
  }
}

async function runQuantRecommend() {
  const market = $("quant-market").value;
  const phase_override = $("quant-phase-override").value;
  const top_n = $("quant-topn").value;
  const params = new URLSearchParams();
  params.set("market", market);
  if (phase_override) params.set("phase_override", phase_override);
  if (top_n) params.set("top_n", top_n);
  $("quant-status").textContent = "⏳ 추천 중... (수십 초)";
  $("btn-quant-recommend").disabled = true;
  try {
    const r = await fetch(`/api/paper/quant-recommend?${params}`);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || "추천 실패");
    }
    const d = await r.json();
    renderQuantRecommend(d);
    $("quant-status").textContent = `✅ ${d.recommendations?.length || 0}건 (${d.phase || "?"})`;
  } catch (e) {
    $("quant-status").textContent = "";
    toast("추천 실패: " + e.message, true);
  } finally {
    $("btn-quant-recommend").disabled = false;
  }
}

function renderQuantRecommend(d) {
  const summary = $("quant-summary");
  summary.innerHTML = "";
  const emoji = {Recovery:"🌱", Expansion:"🚀", Slowdown:"🌧️", Contraction:"🧊"}[d.phase] || "";
  if (d.phase) {
    const div = document.createElement("div");
    div.className = "weight-line";
    div.textContent = `${emoji} ${d.phase} (확신도 ${(d.confidence*100).toFixed(0)}%)`;
    summary.appendChild(div);
    if (d.weights) {
      const wd = document.createElement("div");
      wd.className = "muted";
      const factors = ["Momentum","Value","Quality","LowVol","Size"];
      const parts = factors.filter(f => (d.weights[f]||0) > 0)
        .map(f => `${f}=${d.weights[f].toFixed(2)}`);
      wd.textContent = "가중: " + parts.join(" · ");
      summary.appendChild(wd);
    }
    if (d.needs_recheck) {
      const w = document.createElement("div");
      w.className = "crash-mid";
      w.style.marginTop = "0.4rem";
      w.textContent = "⚠️ 확신도 60% 미만 — 2주 재진단 권고. 직전 국면 가중 유지.";
      summary.appendChild(w);
    }
  }
  if (d.error) {
    const e = document.createElement("div");
    e.className = "crash-warn";
    e.textContent = "❌ " + d.error;
    summary.appendChild(e);
  }
  const tbody = $("quant-tbody");
  if (!d.recommendations || d.recommendations.length === 0) {
    tbody.innerHTML = "<tr><td colspan='9' class='muted'>(결과 없음)</td></tr>";
    return;
  }
  tbody.innerHTML = d.recommendations.map((r, i) => {
    const z = r.z_factors || {};
    const fz = (k) => z[k] !== null && z[k] !== undefined ? (z[k] >= 0 ? "+" : "") + z[k].toFixed(1) : "N/A";
    const cur = r.current_price ? fmt(Math.round(r.current_price)) + "원" : "N/A";
    return `<tr class="signal-row" data-ticker="${r.ticker}" data-name="${r.name}" data-price="${Math.round(r.current_price||0)}" data-slot="콴텍">
      <td>${i+1}</td>
      <td>${r.name} (${r.ticker})</td>
      <td class="num">${(r.composite_score >= 0 ? "+" : "") + r.composite_score.toFixed(2)}</td>
      <td class="num">${fz("Momentum")}</td>
      <td class="num">${fz("Value")}</td>
      <td class="num">${fz("Quality")}</td>
      <td class="num">${fz("LowVol")}</td>
      <td class="num">${fz("Size")}</td>
      <td class="num">${cur}</td>
    </tr>`;
  }).join("");
  // 클릭 → 콴텍 슬롯 매수 폼 자동 채움 + 주문 탭
  document.querySelectorAll("#quant-tbody .signal-row").forEach(row => {
    row.addEventListener("click", () => {
      $("ticker").value = row.dataset.ticker;
      $("name").value = row.dataset.name;
      if (row.dataset.price && row.dataset.price !== "0") $("price").value = row.dataset.price;
      // 슬롯 자동 콴텍으로
      const sel = $("slot-select");
      for (const opt of sel.options) {
        if (opt.value.indexOf("콴텍") !== -1) { sel.value = opt.value; break; }
      }
      document.querySelector(".tab[data-tab='tab-order']").click();
      toast(`${row.dataset.name} 콴텍 슬롯 매수 폼 채움`);
    });
  });
}

$("btn-quant-recommend").addEventListener("click", runQuantRecommend);

// 콴텍 탭 첫 클릭 시 phase 자동 로드 (한 번만)
let quantPhaseLoaded = false;
document.querySelector(".tab[data-tab='tab-quant']").addEventListener("click", () => {
  if (!quantPhaseLoaded) {
    quantPhaseLoaded = true;
    loadQuantPhase();
  }
});

reload();

// ─── IPO봇 탭 (v3.29) ────────────────────────────────────────────────────────
let _ipoCloseTarget = null;

function gradeColor(grade) {
  return {
    "A++":"#f59e0b", "A+":"#10b981", "A":"#22c55e",
    "B":"#3b82f6", "C":"#6b7280", "?":"#9ca3af"
  }[grade] || "#9ca3af";
}

function fmtPct(v) {
  if (v == null) return "-";
  const s = v > 0 ? "+" : "";
  const col = v > 0 ? "#10b981" : v < 0 ? "#ef4444" : "inherit";
  return `<span style="color:${col};font-weight:600">${s}${v.toFixed(1)}%</span>`;
}

function fmtNum(v, unit="") {
  if (v == null || v === "") return "-";
  if (unit === "억") return (v/1e8).toFixed(0) + "억";
  return Number(v).toLocaleString() + unit;
}

async function ipoScan() {
  const status = document.getElementById("ipo-scan-status");
  const tbody = document.getElementById("ipo-scan-body");
  status.textContent = "⏳ 스캔 중...";
  tbody.innerHTML = '<tr><td colspan="10" style="text-align:center">스캔 중...</td></tr>';
  try {
    const r = await fetch("/api/ipo/scan");
    if (!r.ok) throw new Error(await r.text());
    const items = await r.json();
    if (items.error) throw new Error(items.error);
    renderIpoScan(items);
    status.textContent = `✅ ${items.length}건 (${new Date().toLocaleTimeString()})`;
    // 기록 탭도 갱신
    loadIpoRecords();
  } catch(e) {
    status.textContent = "❌ " + e.message;
    tbody.innerHTML = `<tr><td colspan="10" style="color:#ef4444">${e.message}</td></tr>`;
  }
}

function renderIpoScan(items) {
  const tbody = document.getElementById("ipo-scan-body");
  if (!items || items.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted)">공모주 없음</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(it => {
    const g = it.grade || "?";
    const gc = gradeColor(g);
    const score = it.total_score != null ? it.total_score.toFixed(1) : "-";
    const subPeriod = [it.sub_start, it.sub_end].filter(Boolean)
                      .map(d => d.slice(4,6)+"/"+d.slice(6,8)).join("~") || "-";
    const listDate = it.listing_date ? it.listing_date.slice(4,6)+"/"+it.listing_date.slice(6,8) : "-";
    let price = "-";
    if (it.final_price) price = fmtNum(it.final_price, "원");
    else if (it.band_low && it.band_high) price = fmtNum(it.band_low) + "~" + fmtNum(it.band_high, "원");
    else if (it.offer_band_high) price = "~" + fmtNum(it.offer_band_high, "원");
    const mktcap = it.market_cap_100m ? fmtNum(it.market_cap_100m, "억") : "-";
    const rate = it.competition_rate ? it.competition_rate + ":1" : "-";
    const uw = it.underwriter || "-";
    return `<tr>
      <td><span style="color:${gc};font-weight:700">${g}</span></td>
      <td style="font-weight:600">${it.name}</td>
      <td>${subPeriod}</td>
      <td>${listDate}</td>
      <td class="num">${price}</td>
      <td class="num">${mktcap}</td>
      <td class="num">${score}</td>
      <td class="num">${rate}</td>
      <td>${uw}</td>
      <td><button onclick='ipoSubscribe(${JSON.stringify(it)})'
           style="padding:0.2rem 0.6rem;font-size:0.8rem;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer">
           📋 청약등록</button></td>
    </tr>`;
  }).join("");
}

async function ipoSubscribe(item) {
  const alloc = prompt(`[${item.name}] 배정 예상 금액(원, 0이면 생략)?`, "0");
  if (alloc === null) return;
  const body = {
    name: item.name,
    sub_start: item.sub_start || null,
    sub_end: item.sub_end || null,
    listing_date: item.listing_date || null,
    grade: item.grade || null,
    score: item.total_score != null ? item.total_score : null,
    factors: {
      competition_rate: item.competition_rate,
      final_price: item.final_price,
      offer_price: item.final_price || item.offer_band_high,
      lockup_ratio: item.lockup_ratio,
    },
    subscribed: true,
    alloc_amount: parseFloat(alloc) || null,
  };
  const r = await fetch("/api/ipo/subscribe", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if (r.ok) { toast(`✅ ${item.name} 청약 등록`); loadIpoRecords(); }
  else toast("❌ 등록 실패", true);
}

async function loadIpoRecords() {
  const r = await fetch("/api/ipo/records");
  const records = await r.json();
  const tbody = document.getElementById("ipo-records-body");
  if (!records || records.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted)">기록 없음</td></tr>';
    loadIpoStats();
    return;
  }
  tbody.innerHTML = records.map(rec => {
    const g = rec.grade || "?";
    const gc = gradeColor(g);
    const subEnd = rec.sub_end ? rec.sub_end.slice(4,6)+"/"+rec.sub_end.slice(6,8) : "-";
    const ld = rec.listing_date ? rec.listing_date.slice(4,6)+"/"+rec.listing_date.slice(6,8) : "-";
    const score = rec.score != null ? rec.score.toFixed(1) : "-";
    const subBadge = rec.subscribed
      ? '<span style="color:#10b981;font-weight:600">✅ 청약</span>'
      : '<span style="color:var(--muted)">-</span>';
    let offerPrice = "-";
    try { const f = JSON.parse(rec.factors || "{}"); offerPrice = f.offer_price ? fmtNum(f.offer_price,"원") : "-"; } catch(e){}
    const listPrice = rec.listing_price ? fmtNum(rec.listing_price,"원") : "-";
    const retPct = fmtPct(rec.return_pct);
    const closeBtn = rec.listing_price == null && rec.subscribed
      ? `<button onclick="ipoCloseModal('${rec.name}')"
           style="padding:0.2rem 0.6rem;font-size:0.75rem;border:1px solid var(--border);border-radius:4px;cursor:pointer">
           📥 결과입력</button>`
      : (rec.listing_price != null ? "✔" : "-");
    return `<tr>
      <td><span style="color:${gc};font-weight:700">${g}</span></td>
      <td style="font-weight:600">${rec.name}</td>
      <td>${subEnd}</td><td>${ld}</td>
      <td class="num">${score}</td>
      <td>${subBadge}</td>
      <td class="num">${offerPrice}</td>
      <td class="num">${listPrice}</td>
      <td class="num">${retPct}</td>
      <td>${closeBtn}</td>
    </tr>`;
  }).join("");
  loadIpoStats();
}

function ipoCloseModal(name) {
  _ipoCloseTarget = name;
  document.getElementById("ipo-close-name").textContent = name;
  document.getElementById("ipo-close-price").value = "";
  const modal = document.getElementById("ipo-close-modal");
  modal.style.display = "flex";
}

async function ipoCloseSubmit() {
  const price = parseFloat(document.getElementById("ipo-close-price").value);
  if (!price || price <= 0) { alert("상장가를 입력하세요"); return; }
  const r = await fetch("/api/ipo/close", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name: _ipoCloseTarget, listing_price: price})
  });
  document.getElementById("ipo-close-modal").style.display = "none";
  if (r.ok) { toast("✅ 결과 저장"); loadIpoRecords(); }
  else toast("❌ 저장 실패", true);
}

async function loadIpoStats() {
  const r = await fetch("/api/ipo/stats");
  const stats = await r.json();
  const tbody = document.getElementById("ipo-stats-body");
  if (!stats || stats.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted)">상장 완료 데이터 없음</td></tr>';
    return;
  }
  tbody.innerHTML = stats.map(s => {
    const gc = gradeColor(s.grade);
    return `<tr>
      <td><span style="color:${gc};font-weight:700">${s.grade}</span></td>
      <td class="num">${s.n}건</td>
      <td class="num">${fmtPct(s.avg_return)}</td>
      <td class="num">${fmtPct(s.min_return)}</td>
      <td class="num">${fmtPct(s.max_return)}</td>
      <td class="num">${s.n_pos}/${s.n}</td>
    </tr>`;
  }).join("");
}

// IPO 탭 클릭 시 기록 자동 로드
document.querySelector(".tab[data-tab='tab-ipo']").addEventListener("click", () => {
  loadIpoRecords();
});

// ── 성과 탭 (v3.31) ──────────────────────────────────────────────────────────
async function loadPerformance() {
  const status = document.getElementById("perf-status");
  const grid = document.getElementById("perf-grid");
  const tbody = document.getElementById("perf-tbody");
  status.textContent = "로딩 중...";
  try {
    const data = await (await fetch("/api/performance")).json();
    if (data.error) throw new Error(data.error);

    grid.innerHTML = data.map(s => {
      const pnlColor = s.total_pnl >= 0 ? "var(--buy)" : "var(--sell)";
      const wrText = s.win_rate !== null ? s.win_rate + "%" : "—";
      const sharpeText = s.sharpe !== null ? parseFloat(s.sharpe).toFixed(2) : "—";
      return `<div class="card">
        <div class="slot-card">
          <span class="slot-name">${s.slot_name}</span>
          <span style="color:${pnlColor};font-size:1.1rem;font-weight:bold;">
            ${s.total_pnl >= 0 ? "+" : ""}${s.total_pnl.toLocaleString()}원
          </span>
        </div>
        <div style="margin-top:0.5rem;display:grid;grid-template-columns:1fr 1fr;gap:0.3rem;">
          <span class="muted">승률</span><span>${wrText}</span>
          <span class="muted">수익률</span>
          <span style="color:${pnlColor}">${s.total_return_pct >= 0 ? "+" : ""}${s.total_return_pct}%</span>
          <span class="muted">MDD</span><span>${s.max_drawdown_pct}%</span>
          <span class="muted">샤프</span><span>${sharpeText}</span>
          <span class="muted">보유</span><span>${s.n_open_positions}종목</span>
          <span class="muted">완결</span><span>${s.n_closed}건</span>
        </div>
      </div>`;
    }).join("");

    tbody.innerHTML = data.map(s => {
      const pnlColor = s.total_pnl >= 0 ? "var(--buy)" : "var(--sell)";
      return `<tr>
        <td>${s.slot_name}</td>
        <td class="num">${s.n_closed}</td>
        <td class="num">${s.win_rate !== null ? s.win_rate + "%" : "—"}</td>
        <td class="num" style="color:${pnlColor}">
          ${s.total_pnl >= 0 ? "+" : ""}${s.total_pnl.toLocaleString()}원</td>
        <td class="num" style="color:${pnlColor}">
          ${s.total_return_pct >= 0 ? "+" : ""}${s.total_return_pct}%</td>
        <td class="num">${s.max_drawdown_pct}%</td>
        <td class="num">${s.sharpe !== null ? s.sharpe : "—"}</td>
        <td class="num">${s.n_open_positions}</td>
        <td class="num">${s.open_cost.toLocaleString()}원</td>
      </tr>`;
    }).join("");

    status.textContent = "업데이트: " + new Date().toLocaleTimeString();
  } catch(e) {
    status.textContent = "오류: " + e.message;
  }
}

document.getElementById("btn-perf-refresh").addEventListener("click", loadPerformance);
document.querySelector(".tab[data-tab='tab-perf']").addEventListener("click", loadPerformance);

// ─── 나만의 퀀트 (v3.47) ──────────────────────────
async function runMyquantScan() {
  const status = document.getElementById("myquant-status");
  const tbody = document.getElementById("myquant-tbody");
  const refresh = document.getElementById("myquant-refresh").checked;
  status.textContent = "스캔 중... (첫 실행은 수 분)";
  try {
    const data = await (await fetch(`/api/paper/myquant-scan?refresh=${refresh}`)).json();
    if (data.error) throw new Error(data.error);
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">조건 충족 종목 없음</td></tr>`;
    } else {
      tbody.innerHTML = data.items.map((it, i) => {
        const f = it.features || {};
        const pct = v => (v === null || v === undefined) ? "—" : (v * 100).toFixed(1) + "%";
        return `<tr>
          <td>${it.name} <span class="muted">${it.ticker}</span></td>
          <td class="num">${Number(it.price).toLocaleString()}</td>
          <td>${it.matched.join(", ")}</td>
          <td class="num">${pct(f.ma20_gap)}</td>
          <td class="num">${pct(f.dd20)}</td>
          <td class="num">${pct(f.ret5)}</td>
          <td><button type="button" onclick="myquantBuy(${i})">매수</button></td>
        </tr>`;
      }).join("");
      window._myquantItems = data.items;
    }
    status.textContent = `${data.items.length}종목 · ` + new Date().toLocaleTimeString();
  } catch(e) { status.textContent = "오류: " + e.message; }
}

async function myquantBuy(idx) {
  const it = (window._myquantItems || [])[idx];
  if (!it) return;
  const qty = prompt(`${it.name} (${it.ticker}) 매수 수량? (현재가 ${Number(it.price).toLocaleString()}원)`);
  if (!qty || isNaN(parseInt(qty))) return;
  const notes = `MQ[${it.matched.join(",")}] 스캔매수`;
  try {
    const res = await (await fetch("/api/paper/buy", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({slot: "마이퀀트", ticker: it.ticker, name: it.name,
                            quantity: parseInt(qty), price: it.price, notes})
    })).json();
    if (res.error || res.ok === false) throw new Error(res.error || "실패");
    alert(`매수 기록 완료 — 태그: ${it.matched.join(", ")}`);
    loadMyquantTags();
    if (typeof reload === "function") reload();
  } catch(e) { alert("매수 실패: " + e.message); }
}

async function loadMyquantTags() {
  try {
    const data = await (await fetch("/api/paper/myquant-tags")).json();
    if (data.text) document.getElementById("myquant-tags").textContent = data.text;
  } catch(e) { /* 무시 */ }
}

document.getElementById("btn-myquant-scan").addEventListener("click", runMyquantScan);
document.querySelector(".tab[data-tab='tab-myquant']").addEventListener("click", loadMyquantTags);

</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML_PAGE)


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "paper_ui", "version": "v3.35"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    log.info(f"📑 Paper Trading UI: http://{args.host}:{args.port}")
    log.info(f"   DB: {pdb.DEFAULT_DB_PATH}")
    log.info(f"   Seed: {pdb.DEFAULT_SEED_KRW:,}원, Fee: {pdb.DEFAULT_FEE_BPS}bp")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
