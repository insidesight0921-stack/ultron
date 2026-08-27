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
import uuid
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
from private_data_api_client import (  # noqa: E402
    PrivateAPIError,
    PrivateDataClient,
    private_api_enabled,
)
from paper_trade_identity import build_paper_trade_write_identity  # noqa: E402
from private_write_runtime import load_private_write_runtime_bundle  # noqa: E402
from kium_bot import run as kium_run  # noqa: E402  # v3.19 신호 탭
import quant_bot as qb  # noqa: E402  # v3.23 콴텍 탭

log = logging.getLogger("paper_ui")


# main()에서 v3 bundle 전체가 검증된 경우에만 설정한다. import/test와 v1/v2는
# 기존 direct Paper 경로를 유지한다.
_PAPER_WRITE_CLIENT = None
_PAPER_WRITE_EXECUTOR = None


def _configure_paper_write_runtime(runtime_bundle) -> None:
    global _PAPER_WRITE_CLIENT, _PAPER_WRITE_EXECUTOR
    _PAPER_WRITE_CLIENT = None
    _PAPER_WRITE_EXECUTOR = None
    if runtime_bundle is None or not runtime_bundle.paper_writes_enabled:
        return
    _PAPER_WRITE_CLIENT = runtime_bundle.build_paper_client()
    _PAPER_WRITE_EXECUTOR = runtime_bundle.build_paper_executor(_PAPER_WRITE_CLIENT)


# 앱 시작 시 시드
pdb.ensure_seed()

# v3.47 — 나만의 퀀트 슬롯 보장(초기 자본 1,000만 · 필요시 DB에서 조정)
MYQUANT_SLOT = "마이퀀트"
try:
    pdb.ensure_slot(MYQUANT_SLOT)
except Exception:
    log.exception("마이퀀트 슬롯 생성 실패")


app = FastAPI(title="Paper Trading UI")


def _private_client() -> PrivateDataClient:
    return PrivateDataClient()


def _paper_slot_id(slot: object) -> int:
    if _PAPER_WRITE_CLIENT is None:
        raise RuntimeError("Paper Private client is unavailable")
    value = str(slot or "").strip()
    slots = _PAPER_WRITE_CLIENT.list_paper_slots()
    match = next(
        (
            item
            for item in slots
            if str(item.get("id")) == value or str(item.get("name", "")) == value
        ),
        None,
    )
    if match is None:
        raise ValueError("Paper slot was not found")
    return int(match["id"])


def _execute_paper_ui_write(
    action: str,
    *,
    source_event_id: object,
    slot: object,
    ticker: str,
    quantity: int,
    price: float,
    name: str | None = None,
    notes: str | None = None,
) -> dict[str, object]:
    if _PAPER_WRITE_EXECUTOR is None:
        raise RuntimeError("Paper Private executor is unavailable")
    slot_id = _paper_slot_id(slot)
    event_id = str(source_event_id or "").strip()
    try:
        uuid.UUID(event_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Paper order event ID is required") from exc
    identity = build_paper_trade_write_identity(
        caller="paper-ui",
        operation=f"paper.{action}",
        slot_id=slot_id,
        actor_id="local-paper-ui",
        source_event_id=event_id,
        item_key="single-order",
    )
    execution = _PAPER_WRITE_EXECUTOR.execute(
        caller="paper-ui",
        action=action,
        slot_id=slot_id,
        identity=identity,
        user_approved=True,
        policy_approved=False,
        ticker=ticker,
        name=name,
        quantity=quantity,
        price=price,
        notes=notes,
    )
    return {**execution.result, "replayed": execution.replayed}


def _list_portfolios() -> list[dict]:
    if private_api_enabled():
        try:
            return _private_client().list_paper_portfolios()
        except (PrivateAPIError, ValueError) as exc:
            log.warning("Private API portfolio fallback: %s", type(exc).__name__)
    return pdb.list_portfolios()


def _list_positions(slot=None) -> list[dict]:
    if private_api_enabled():
        try:
            return _private_client().list_paper_positions(slot=slot)
        except (PrivateAPIError, ValueError) as exc:
            log.warning("Private API position fallback: %s", type(exc).__name__)
    return pdb.list_positions(slot=slot)


def _list_slots() -> list[dict]:
    if private_api_enabled():
        try:
            return _private_client().list_paper_slots()
        except (PrivateAPIError, ValueError) as exc:
            log.warning("Private API slot fallback: %s", type(exc).__name__)
    slots = pdb.list_slots()
    out = []
    for slot in slots:
        summary = pdb.slot_summary(slot["id"]) or {}
        out.append(
            {
                **slot,
                "n_positions": summary.get("n_positions", 0),
                "n_trades": summary.get("n_trades", 0),
            }
        )
    return out


def _list_trades(slot=None, *, limit: int = 100) -> list[dict]:
    if private_api_enabled():
        try:
            return _private_client().list_paper_trades(slot=slot, limit=limit)
        except (PrivateAPIError, ValueError) as exc:
            log.warning("Private API trade fallback: %s", type(exc).__name__)
    return pdb.list_trades(slot=slot, limit=limit)


def _list_ipo_records() -> list[dict]:
    if private_api_enabled():
        try:
            return _private_client().list_paper_ipo_records()
        except (PrivateAPIError, ValueError) as exc:
            log.warning("Private API IPO records fallback: %s", type(exc).__name__)
    return pdb.ipo_list()


def _list_ipo_stats() -> list[dict]:
    if private_api_enabled():
        try:
            return _private_client().list_paper_ipo_stats()
        except (PrivateAPIError, ValueError) as exc:
            log.warning("Private API IPO stats fallback: %s", type(exc).__name__)
    return pdb.ipo_stats()


def _get_performance() -> list[dict]:
    if private_api_enabled():
        try:
            return _private_client().list_paper_performance()
        except (PrivateAPIError, ValueError) as exc:
            log.warning("Private API performance fallback: %s", type(exc).__name__)
    return pdb.performance_stats()


def _get_myquant_tags() -> dict:
    if private_api_enabled():
        try:
            return _private_client().get_paper_myquant_tags()
        except (PrivateAPIError, ValueError) as exc:
            log.warning("Private API myquant-tags fallback: %s", type(exc).__name__)
    import trade_analytics as ta

    roundtrips = ta.compute_roundtrips(pdb.list_trades(limit=100000))
    return {
        "tags": ta.tag_performance(roundtrips),
        "text": ta.format_tag_performance(roundtrips),
    }


# ─── IPO봇 API (v3.29) ────────────────────────────────────────────────────────


@app.get("/api/ipo/scan")
async def api_ipo_scan():
    """ipo_bot scan 실행 → JSON 반환."""
    import subprocess, json as _json, sys

    try:
        result = subprocess.run(
            [sys.executable, str(PROJECT / "scripts" / "ipo_bot.py"), "scan", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(PROJECT),
        )
        if result.returncode == 0 and result.stdout.strip():
            return JSONResponse(_json.loads(result.stdout))
        return JSONResponse(
            {"error": result.stderr[:500] or "scan 실패"}, status_code=500
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/ipo/records")
async def api_ipo_records():
    return JSONResponse(_list_ipo_records())


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
    return JSONResponse(_list_ipo_stats())


@app.get("/api/paper/portfolios")
async def api_portfolios():
    return JSONResponse(_list_portfolios())


@app.get("/api/paper/slots")
async def api_slots():
    return JSONResponse(_list_slots())


@app.get("/api/paper/positions")
async def api_positions(slot: str | None = None):
    return JSONResponse(_list_positions(slot=slot))


@app.get("/api/paper/trades")
async def api_trades(slot: str | None = None, limit: int = 100):
    return JSONResponse(_list_trades(slot=slot, limit=limit))


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
                return JSONResponse(
                    {
                        "ticker": str(ticker),
                        "name": name,
                        "price": price,
                    }
                )
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
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), str(ticker).strip()
        )
        if df is None or len(df) == 0:
            return JSONResponse({"error": "데이터 없음"}, status_code=404)

        # 컬럼 — 한글/영어 모두 지원
        col_map = {}
        for src_col, dst_col in [
            ("시가", "open"),
            ("고가", "high"),
            ("저가", "low"),
            ("종가", "close"),
            ("거래량", "volume"),
            ("Open", "open"),
            ("High", "high"),
            ("Low", "low"),
            ("Close", "close"),
            ("Volume", "volume"),
        ]:
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

        return JSONResponse(
            {
                "ticker": str(ticker),
                "name": name,
                "dates": [d.strftime("%Y-%m-%d") for d in df.index],
                "opens": [float(x) for x in df[col_map["open"]]],
                "highs": [float(x) for x in df[col_map["high"]]],
                "lows": [float(x) for x in df[col_map["low"]]],
                "closes": [float(x) for x in df[col_map["close"]]],
                "volumes": (
                    [float(x) for x in df[col_map["volume"]]]
                    if "volume" in col_map
                    else []
                ),
                "n": len(df),
            }
        )
    except Exception as e:
        log.warning(f"chart-data 실패 {ticker}: {e}")
        return JSONResponse({"error": str(e)}, status_code=410)


def _slot_hard_stop_reason(slot) -> str | None:
    """슬롯 일일 손실 한도로 매수가 막혀 있으면 사유, 아니면 None.

    모듈·상태 파일이 없으면 None(허용)을 돌려준다. 한도를 '판정하지 못한 것'과
    '한도에 걸린 것'은 다르며, 전자로 매수를 막으면 원인 모를 차단이 된다.
    """
    try:
        import slot_hard_stop
        return slot_hard_stop.guard_buy(str(slot))
    except Exception as exc:  # noqa: BLE001
        log.warning("일일 한도 확인 실패(매수 허용): %s", exc)
        return None


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
    source_event_id = body.get("source_event_id")

    if not slot or not ticker or quantity is None or price is None:
        return JSONResponse(
            {"error": "필수 인자: slot, ticker, quantity, price"},
            status_code=400,
        )

    # v3.48 — 슬롯 일일 손실 한도. 막힌 슬롯은 신규 매수만 거부하고
    # 보유 종목의 손절·트레일링은 건드리지 않는다(매도는 항상 허용).
    blocked = _slot_hard_stop_reason(slot)
    if blocked:
        return JSONResponse({"error": blocked}, status_code=409)

    try:
        if _PAPER_WRITE_EXECUTOR is None:
            out = pdb.record_buy(
                slot=slot,
                ticker=ticker,
                name=name,
                quantity=int(quantity),
                price=float(price),
                notes=notes,
            )
        else:
            out = _execute_paper_ui_write(
                "buy",
                source_event_id=source_event_id,
                slot=slot,
                ticker=ticker,
                name=name,
                quantity=int(quantity),
                price=float(price),
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
    source_event_id = body.get("source_event_id")

    if not slot or not ticker or quantity is None or price is None:
        return JSONResponse(
            {"error": "필수 인자: slot, ticker, quantity, price"},
            status_code=400,
        )

    try:
        if _PAPER_WRITE_EXECUTOR is None:
            out = pdb.record_sell(
                slot=slot,
                ticker=ticker,
                quantity=int(quantity),
                price=float(price),
                notes=notes,
            )
        else:
            out = _execute_paper_ui_write(
                "sell",
                source_event_id=source_event_id,
                slot=slot,
                ticker=ticker,
                quantity=int(quantity),
                price=float(price),
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
        return JSONResponse(
            {
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
            }
        )
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
        phase = phase_override if phase_override in qb.PHASES else snap.consensus_phase
        if not phase:
            return JSONResponse(
                {
                    "phase": None,
                    "confidence": snap.confidence,
                    "needs_recheck": snap.needs_recheck,
                    "weights": None,
                    "recommendations": [],
                    "error": "거시 국면 미확정 — ECOS/FRED 키 또는 네트워크 점검",
                    "snapshot": {
                        "phase_kr": snap.phase_kr,
                        "phase_us": snap.phase_us,
                        "summary_text": qb.format_snapshot(snap),
                    },
                }
            )
        weights_all = qb.parse_phase_weights_from_wiki()
        recs = qb.recommend_top_n(
            phase=phase,
            market=market,
            top_n=top_n,
            weights=weights_all,
        )
        return JSONResponse(
            {
                "phase": phase,
                "confidence": snap.confidence,
                "needs_recheck": snap.needs_recheck,
                "weights": weights_all.get(phase),
                "recommendations": [
                    {
                        "ticker": r.ticker,
                        "name": r.name,
                        "composite_score": r.composite_score,
                        "current_price": r.current_price,
                        "z_factors": r.z_factors,
                        "raw_factors": r.raw_factors,
                    }
                    for r in recs
                ],
                "snapshot": {
                    "phase_kr": snap.phase_kr,
                    "phase_us": snap.phase_us,
                    "summary_text": qb.format_snapshot(snap),
                },
                "market": market,
                "phase_override": phase_override,
            }
        )
    except Exception as e:
        log.exception("quant-recommend 실패")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/paper/kium-scan")
async def api_kium_scan(
    top_n: int = 10, market: str = "KOSPI200", with_crash_signals: bool = True
):
    """v3.19: 키움봇 모멘텀 스캔 + 크래시 감지를 paper_ui에서 직접 호출.

    응답: {"results": [...], "crash_signals": {...}, "weight": {...}}
    또는 raw 텍스트 출력의 구조화 버전 (UI가 표·경고·비중 분리 렌더링).
    """
    try:
        from kium_bot import (
            scan_universe,
            fetch_kospi_close,
            fetch_vkospi_latest,
            detect_crash_signals,
            compute_weight_recommendation,
        )

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
        return JSONResponse(
            {
                "results": results,
                "crash_signals": crash,
                "weight": weight,
                "market": market,
                "top_n": top_n,
            }
        )
    except Exception as e:
        log.exception("kium-scan 실패")
        return JSONResponse({"error": str(e)}, status_code=500)


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

            cpath = (
                PATHS.private_state_dir
                / f"myquant_scan_{datetime.now().strftime('%Y%m%d')}.json"
            )
            cpath.unlink(missing_ok=True)
        items = eb.scan_current(market_code=market)
        return JSONResponse(
            {
                "items": items,
                "slot": MYQUANT_SLOT,
                "conditions": list(eb.SCAN_CONDITIONS),
            }
        )
    except Exception as e:
        log.exception("myquant-scan 실패")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/paper/myquant-tags")
async def api_myquant_tags():
    """마이퀀트 태그별 실현 성과 (buy notes의 MQ[...] 기준)."""
    try:
        return JSONResponse(_get_myquant_tags())
    except Exception as e:
        log.exception("myquant-tags 실패")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/performance")
async def api_performance():
    """v3.31: 슬롯별 실현 성과 통계 (승률·수익률·MDD·샤프)."""
    try:
        return JSONResponse(_get_performance())
    except Exception as e:
        log.exception("performance 조회 실패")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/signal/accuracy")
async def api_signal_accuracy(horizon: int = 5):
    """v3.48: 기술적 신호 적중률(탭 B). 저장된 결과만 집계한다.

    가격 조회·결과 채우기는 `signal_review.py`(매일 스케줄)가 소유한다.
    UI 요청이 외부 API를 때리면 화면이 느려지고 장중에 값이 흔들린다.
    """
    try:
        import signal_review as sv
        outcomes = sv.load_outcomes()
        summary = sv.summarize(outcomes.values(), horizon=int(horizon))
        pending = sum(1 for o in outcomes.values() if o.get("pending"))
        recent = sorted(
            (o for o in outcomes.values() if not o.get("pending")),
            key=lambda o: str(o.get("at") or ""), reverse=True,
        )[:30]
        return JSONResponse({"summary": summary, "pending": pending,
                             "evaluated": len(outcomes) - pending, "recent": recent})
    except Exception as e:
        log.exception("신호 적중률 조회 실패")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/strategy/compare")
async def api_strategy_compare():
    """v3.49: 전략별 성과 비교(탭 A). 완결 라운드트립을 축별로 집계한다.

    진입 전략 태그 커버리지도 함께 돌려준다 — 커버리지가 0이면
    "전략별 비교"라는 말 자체가 성립하지 않으므로 화면이 그 사실을 먼저 말해야 한다.
    """
    try:
        import strategy_compare as sc
        kept, excluded = sc.load_roundtrips_with_exclusions()
        return JSONResponse(sc.overview(kept, excluded=excluded))
    except Exception as e:
        log.exception("전략 비교 조회 실패")
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── HTML 페이지 ────────────────────────────────────


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Paper Trading</title>
<style>
  /* v3.48 리디자인 — 라이트 기본 + OS 다크 자동. 색은 손익 부호와 슬롯 식별에만 쓴다. */
  :root {
    color-scheme: light;
    --plane:#f9f9f7; --surface:#fff; --surface-2:#f4f4f1;
    --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
    --hairline:#e1e0d9; --ring:rgba(11,11,11,.10);
    --accent:#2a78d6; --accent-ink:#1c5cab;
    --slot-1:#2a78d6; --slot-2:#eb6834; --slot-3:#1baf7a; --slot-4:#eda100;
    --buy:#006300; --sell:#c0322f; --warn:#fab219;
    --bg:var(--plane); --fg:var(--ink); --card:var(--surface); --border:var(--hairline);
    --radius:10px;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --plane:#0d0d0d; --surface:#1a1a19; --surface-2:#232321;
      --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
      --hairline:#2c2c2a; --ring:rgba(255,255,255,.10);
      --accent:#3987e5; --accent-ink:#86b6ef;
      --slot-1:#3987e5; --slot-2:#d95926; --slot-3:#199e70; --slot-4:#c98500;
      --buy:#0ca30c; --sell:#e66767;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin:0; }
  body { background:var(--plane); color:var(--ink); font-size:14px; line-height:1.5;
         font-family: system-ui, -apple-system, 'Apple SD Gothic Neo', 'Segoe UI', sans-serif;
         -webkit-font-smoothing:antialiased; }
  .container { max-width:1120px; margin:0 auto; padding:32px 24px 64px; }
  h1 { font-size:22px; font-weight:620; letter-spacing:-.01em; margin:0; }
  h2 { font-size:13px; font-weight:600; color:var(--ink-2); letter-spacing:.04em;
       text-transform:uppercase; margin:0 0 12px; }
  h3 { font-size:14px; font-weight:600; margin:0; }
  .muted { color:var(--muted); font-size:12px; }
  .grid { display:grid; gap:12px; }
  .grid-2 { grid-template-columns:repeat(2,1fr); }
  .grid-3, .grid-4 { grid-template-columns:repeat(4,1fr); }
  .grid-auto { grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); }
  .stat-rows { display:grid; grid-template-columns:auto 1fr; gap:6px 16px;
               margin-top:10px; font-size:13px; }
  .stat-rows > :nth-child(odd) { color:var(--muted); }
  .stat-rows > :nth-child(even) { text-align:right; font-variant-numeric:tabular-nums; }
  @media (max-width:860px){ .grid-3,.grid-2,.grid-4{grid-template-columns:1fr} .kpis{grid-template-columns:1fr 1fr} }

  .masthead { display:flex; align-items:flex-end; justify-content:space-between;
              gap:16px; flex-wrap:wrap; margin-bottom:24px; }
  .status-pill { display:inline-flex; align-items:center; gap:7px; background:var(--surface);
                 border:1px solid var(--hairline); border-radius:999px; padding:6px 12px;
                 font-size:12px; color:var(--ink-2); }
  .dot { width:7px; height:7px; border-radius:50%; background:var(--buy); }

  .kpis { display:grid; grid-template-columns:repeat(4,1fr); gap:1px; background:var(--hairline);
          border:1px solid var(--hairline); border-radius:var(--radius);
          overflow:hidden; margin-bottom:28px; }
  .kpi { background:var(--surface); padding:16px 18px; }
  .kpi-label { font-size:12px; color:var(--muted); margin-bottom:6px; }
  .kpi-value { font-size:22px; font-weight:600; letter-spacing:-.02em; }
  .kpi-value .unit { font-size:13px; font-weight:500; color:var(--ink-2); margin-left:2px; }
  .kpi-meta { font-size:12px; color:var(--muted); margin-top:4px; }
  .delta.up { color:var(--buy); font-weight:600; }
  .delta.down { color:var(--sell); font-weight:600; }

  .alloc-bar { display:flex; gap:2px; height:10px; margin-bottom:10px; }
  .alloc-bar span { border-radius:3px; }
  .alloc-legend { display:flex; flex-wrap:wrap; gap:16px; font-size:12px; color:var(--ink-2); }
  .alloc-legend i, .slot-tag i, .group-label i, .chip i {
    display:inline-block; width:8px; height:8px; border-radius:2px; }
  .alloc-legend i { margin-right:6px; vertical-align:1px; }

  /* 레거시 탭(신호·콴텍·IPO·마이퀀트·성과)은 카드 안에 내용을 바로 넣는다 → 기본 여백 필요.
     card-head/card-body를 쓰는 새 카드만 .flush로 여백을 0으로 만든다. */
  .card { background:var(--surface); border:1px solid var(--hairline);
          border-radius:var(--radius); padding:18px; }
  .card.flush { padding:0; }
  .card.flush > .card-body { padding:18px; }
  .card > table { margin-left:-18px; width:calc(100% + 36px); }
  .card > h3 { margin-bottom:12px; }
  .card + .card { margin-top:20px; }
  .toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .toolbar + p, .toolbar + .muted { margin-top:10px; }
  .card-head { display:flex; align-items:center; justify-content:space-between; gap:12px;
               padding:14px 18px; border-bottom:1px solid var(--hairline); flex-wrap:wrap; }
  .card-body { padding:18px; }
  .card-foot { padding:10px 18px; border-top:1px solid var(--hairline); font-size:12px;
               color:var(--muted); background:var(--surface-2);
               border-radius:0 0 var(--radius) var(--radius); }

  .slot { background:var(--surface); border:1px solid var(--hairline); border-radius:var(--radius);
          padding:14px 16px; position:relative; overflow:hidden; }
  .slot::before { content:''; position:absolute; left:0; top:0; bottom:0; width:3px;
                  background:var(--slot-color, var(--accent)); }
  .slot-name { font-size:13px; font-weight:600; }
  .slot-share { font-size:12px; color:var(--muted); font-weight:400; margin-left:4px; }
  .slot-capital { font-size:19px; font-weight:600; letter-spacing:-.02em; margin-top:6px; }
  .slot-meta { font-size:12px; color:var(--muted); margin-top:6px; display:flex; gap:10px; }
  .slot-card { display:flex; justify-content:space-between; align-items:baseline; }

  .tabs { display:flex; gap:2px; border-bottom:1px solid var(--hairline);
          margin:0 0 20px; overflow-x:auto; }
  .tab { appearance:none; background:none; border:0; border-bottom:2px solid transparent;
         color:var(--muted); font:inherit; font-size:13px; padding:10px 14px;
         cursor:pointer; white-space:nowrap; }
  .tab:hover { color:var(--ink); }
  .tab.active { color:var(--ink); border-bottom-color:var(--accent); font-weight:600; }
  .tab-content { display:none; }
  .tab-content.active { display:block; }

  table { width:100%; border-collapse:collapse; }
  th, td { padding:10px 18px; text-align:left; }
  thead th { font-size:12px; font-weight:500; color:var(--muted);
             border-bottom:1px solid var(--hairline); white-space:nowrap; }
  tbody td { border-bottom:1px solid var(--hairline); }
  tbody tr:last-child td { border-bottom:0; }
  tbody tr:hover td { background:var(--surface-2); }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  .ticker { color:var(--muted); font-variant-numeric:tabular-nums; margin-left:6px; font-size:12px; }
  tr.group td { background:var(--surface-2); padding:8px 18px; }
  tr.group:hover td { background:var(--surface-2); }
  .group-label { display:flex; align-items:center; gap:8px; font-weight:600; font-size:13px; }
  .group-label .count { font-weight:400; font-size:12px; color:var(--muted); }
  .indent { padding-left:34px; }
  .slot-tag { display:inline-flex; align-items:center; gap:7px; font-size:13px; color:var(--ink-2); }

  .chips { display:flex; gap:6px; flex-wrap:wrap; }
  .chip { appearance:none; font:inherit; font-size:12px; cursor:pointer; background:var(--surface);
          color:var(--ink-2); border:1px solid var(--hairline); border-radius:999px; padding:5px 12px; }
  .chip:hover { background:var(--surface-2); }
  .chip[aria-pressed="true"] { background:var(--surface-2); color:var(--ink);
                               font-weight:600; border-color:var(--ring); }
  .chip i { margin-right:6px; vertical-align:1px; }

  form { display:grid; gap:12px; }
  form .row { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  label.field { display:grid; gap:6px; font-size:12px; color:var(--ink-2); }
  input, select { background:var(--surface); color:var(--ink); border:1px solid var(--hairline);
                  border-radius:7px; padding:9px 11px; font:inherit; width:100%; }
  /* 체크박스·라디오는 폭 100%를 상속하면 안 된다 */
  input[type="checkbox"], input[type="radio"] { width:auto; margin:0; padding:0; }
  label.muted { display:inline-flex; align-items:center; gap:6px; }
  select { max-width:260px; }
  #tab-order select { max-width:none; }
  input::placeholder { color:var(--muted); }
  input:focus, select:focus { outline:2px solid var(--accent); outline-offset:-1px;
                              border-color:var(--accent); }
  button { font:inherit; font-weight:600; font-size:13px; border-radius:7px; padding:9px 18px;
           cursor:pointer; border:1px solid var(--hairline); background:var(--surface); color:var(--ink); }
  button:hover { background:var(--surface-2); }
  .btn-quote, .btn-scan { background:var(--accent); border-color:var(--accent); color:#fff; }
  .btn-quote:hover, .btn-scan:hover { background:var(--accent-ink); border-color:var(--accent-ink); }
  .btn-buy { border-color:var(--buy); color:var(--buy); background:var(--surface); }
  .btn-sell { border-color:var(--sell); color:var(--sell); background:var(--surface); }
  .tag { display:inline-block; font-size:11px; font-weight:600; padding:2px 7px;
         border-radius:5px; border:1px solid var(--ring); color:var(--ink-2); }
  .badge-buy, .tag-buy { color:var(--buy); }
  .badge-sell, .tag-sell { color:var(--sell); }

  .toast { position:fixed; bottom:16px; right:16px; background:var(--surface);
           border:1px solid var(--hairline); box-shadow:0 4px 16px var(--ring);
           padding:10px 16px; border-radius:8px; opacity:0; transition:opacity .3s; }
  .toast.show { opacity:1; }
  .toast.error { border-color:var(--sell); }

  #tv-chart-container { width:100%; height:540px; background:var(--surface);
                        border:1px solid var(--hairline); border-radius:var(--radius); }
  .crash-warn { background:var(--warn); color:#000; padding:8px 14px; border-radius:7px; font-weight:600; }
  .crash-ok { background:var(--buy); color:#fff; padding:8px 14px; border-radius:7px; }
  .crash-mid { background:var(--accent); color:#fff; padding:8px 14px; border-radius:7px; }
  .signal-row td { cursor:pointer; }
  .signal-row:hover td { background:var(--surface-2); }
  .weight-line { color:var(--accent); font-size:14px; margin:6px 0; }
  /* 태그별 성과처럼 줄맞춤이 필요한 텍스트 블록 */
  pre { margin:0; font:12px/1.7 ui-monospace, SFMono-Regular, Menlo, monospace;
        color:var(--ink-2); white-space:pre-wrap; }
  .card > p:last-child, .card > .muted:last-child { margin-bottom:0; }
</style>
</head>
<body>
<div class="container">
  <header class="masthead">
    <div>
      <h1>Paper Trading</h1>
      <p class="muted">실주문 없음 · 가상 매매 · 6개월 검증 후 KIS 입문</p>
    </div>
    <span class="status-pill"><span class="dot"></span><span id="asof">모의 운용 중</span></span>
  </header>

  <section id="kpis" class="kpis"></section>

  <h2>슬롯 배분</h2>
  <div class="alloc" style="margin-bottom:12px;">
    <div id="alloc-bar" class="alloc-bar"></div>
    <div id="alloc-legend" class="alloc-legend"></div>
  </div>
  <div id="slots" class="grid grid-4" style="margin-bottom:28px;"></div>

  <div class="tabs">
    <button class="tab active" data-tab="tab-order">주문</button>
    <button class="tab" data-tab="tab-chart">차트</button>
    <button class="tab" data-tab="tab-signals">키움봇 신호</button>
    <button class="tab" data-tab="tab-quant">콴텍봇</button>
    <button class="tab" data-tab="tab-ipo">IPO봇</button>
    <button class="tab" data-tab="tab-myquant">나만의 퀀트</button>
    <button class="tab" data-tab="tab-strategy">전략 비교</button>
    <button class="tab" data-tab="tab-signal-acc">신호 정확도</button>
    <button class="tab" data-tab="tab-perf">성과</button>
  </div>

  <!-- 탭 1: 주문 -->
  <div id="tab-order" class="tab-content active">
    <div class="card flush">
      <div class="card-head">
        <h3>주문</h3>
        <span class="muted">가상 매매만 실행됩니다</span>
      </div>
      <div class="card-body">
        <form id="order-form">
          <div class="row">
            <label class="field">슬롯
              <select name="slot" id="slot-select" required></select>
            </label>
            <label class="field">종목코드
              <span style="display:flex; gap:8px;">
                <input type="text" name="ticker" id="ticker" placeholder="005930" required style="flex:1">
                <button type="button" class="btn-quote" id="btn-quote">현재가</button>
              </span>
            </label>
          </div>
          <label class="field">종목명
            <input type="text" name="name" id="name" placeholder="자동 채움">
          </label>
          <div class="row">
            <label class="field">수량
              <input type="number" name="quantity" id="quantity" placeholder="0" min="1" required>
            </label>
            <label class="field">단가
              <input type="number" name="price" id="price" placeholder="현재가 조회" min="1" step="any" required>
            </label>
          </div>
          <label class="field">메모
            <input type="text" name="notes" id="notes" placeholder="선택">
          </label>
          <div class="row">
            <button type="button" class="btn-buy" id="btn-buy">매수</button>
            <button type="button" class="btn-sell" id="btn-sell">매도</button>
          </div>
        </form>
      </div>
    </div>

    <div class="card flush">
      <div class="card-head">
        <h3>보유 포지션</h3>
        <div class="chips" id="chips-pos" data-filter="pos"></div>
      </div>
      <div id="positions"><table><thead>
        <tr><th>종목</th><th class="num">수량</th><th class="num">평균가</th><th class="num">평가금액</th></tr>
      </thead><tbody data-group="pos"></tbody></table></div>
      <div class="card-foot">봇별로 묶어 표시 · 평가금액은 평균가 기준 장부가입니다</div>
    </div>

    <div class="card flush">
      <div class="card-head">
        <h3>거래 내역</h3>
        <div class="chips" id="chips-trd" data-filter="trd"></div>
      </div>
      <div id="trades"><table><thead>
        <tr><th>시각</th><th>봇</th><th>종목</th><th>방향</th>
            <th class="num">수량</th><th class="num">단가</th><th class="num">수수료</th></tr>
      </thead><tbody data-group="trd"></tbody></table></div>
      <div class="card-foot">최신순 100건 · 칩을 누르면 해당 봇 거래만 남습니다</div>
    </div>
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

  <!-- 탭 6.5: 나만의 퀀트 (v3.47) -->
  <div id="tab-myquant" class="tab-content">
    <div class="card">
      <div class="toolbar">
        <button type="button" class="btn-scan" id="btn-myquant-scan">조건 스캔 (KOSPI200)</button>
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
      <h3>태그별 실현 성과</h3>
      <pre id="myquant-tags" class="muted" style="white-space: pre-wrap;">아직 마이퀀트 거래가 없습니다.</pre>
      <p class="muted">※ 통계 관찰이며 투자 권유 아님. 표본 5건 미만(†)은 참고용.</p>
    </div>
  </div>

  <!-- 탭 A: 전략별 성과 비교 (v3.49) -->
  <div id="tab-strategy" class="tab-content">
    <div class="card" style="margin-bottom:20px;">
      <div class="toolbar">
        <div class="chips" id="strat-axes"></div>
        <button type="button" class="btn-scan" id="btn-strat-refresh">새로고침</button>
        <span id="strat-status" class="muted"></span>
      </div>
      <p class="muted" style="margin-top:10px;" id="strat-caveat"></p>
    </div>
    <div id="strat-attr" style="margin-bottom:20px;"></div>
    <div class="card">
      <h3 id="strat-title">비교</h3>
      <table>
        <thead><tr>
          <th>구분</th><th class="num">건수</th><th class="num">비중</th>
          <th class="num">승률</th><th class="num">실현 손익</th>
          <th class="num">평균 수익률</th><th class="num">전체 대비</th>
          <th class="num">PF</th><th class="num">평균 보유</th>
        </tr></thead>
        <tbody id="strat-tbody">
          <tr><td colspan="9" class="muted">완결된 거래가 아직 없습니다.</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 탭 B: 신호 정확도 (v3.48) -->
  <div id="tab-signal-acc" class="tab-content">
    <div class="card" style="margin-bottom:20px;">
      <div class="toolbar">
        <button type="button" class="btn-scan" id="btn-sigacc-refresh">새로고침</button>
        <select id="sigacc-horizon">
          <option value="1">1거래일</option>
          <option value="5" selected>5거래일</option>
        </select>
        <span id="sigacc-status" class="muted"></span>
      </div>
      <p class="muted" style="margin-top:10px;">
        신호 후 수익률을 <b>같은 종목의 '아무 날이나 진입했을 때' 평균(베이스라인)</b>과 비교한다.
        차이(edge)가 양수여야 신호에 값이 있다. MTF 필터에 억제된 신호도 함께 평가해
        필터가 값을 더하는지 확인한다. 체결·수수료·슬리피지는 반영하지 않는다.
      </p>
    </div>
    <div id="sigacc-summary" class="grid grid-auto" style="margin-bottom:20px;"></div>
    <div class="card">
      <h3>최근 평가된 신호</h3>
      <table>
        <thead><tr>
          <th>시각</th><th>종목</th><th>전략</th><th>액션</th>
          <th class="num">수익률</th><th class="num">베이스라인</th><th class="num">edge</th><th>발송</th>
        </tr></thead>
        <tbody id="sigacc-tbody">
          <tr><td colspan="8" class="muted">아직 평가된 신호가 없습니다.</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 탭 6: 성과 통계 (v3.31) -->
  <div id="tab-perf" class="tab-content">
    <div class="card" style="margin-bottom:20px;">
      <div class="toolbar">
        <button type="button" class="btn-scan" id="btn-perf-refresh">새로고침</button>
        <span id="perf-status" class="muted"></span>
      </div>
      <p class="muted" style="margin-top:10px;">
        <b>거래당 샤프</b>는 완결 거래 1건을 한 관측치로 본 값입니다. 실전 전환 기준의
        「샤프 1.0」은 <b>일간</b> 수익률 기준이라 서로 다른 수치이며, 일간 마크투마켓
        곡선이 없어 아직 산출하지 않습니다. MDD도 거래 순서 기준이라 보유 중 평가손실은
        반영되지 않아 실제보다 얕게 나옵니다. 두 값 모두 전환 판단에 쓰지 마세요.
      </p>
    </div>
    <div id="perf-grid" class="grid grid-auto" style="margin-bottom:20px;"></div>
    <div class="card">
      <h3>완결 거래 성과 상세</h3>
      <table>
        <thead><tr>
          <th>슬롯</th><th class="num">완결 거래</th><th class="num">승률</th>
          <th class="num">실현 손익</th><th class="num">수익률</th>
          <th class="num">MDD</th><th class="num">거래당 샤프</th>
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

const SLOT_COLORS = ["var(--slot-1)", "var(--slot-2)", "var(--slot-3)", "var(--slot-4)"];
let SLOT_ORDER = [];            // 화면 표시 순서(자본 큰 순)
let SLOT_COLOR = {};            // 슬롯명 → 색 (성과 탭도 참조)
const won = n => fmt(Math.round(Number(n) || 0)) + "원";
const signed = n => (Number(n) > 0 ? "+" : "") + fmt(Math.round(Number(n) || 0));

async function loadSlots() {
  const slots = await (await fetch("/api/paper/slots")).json();
  const sel = $("slot-select");
  const prev = sel.value;
  sel.innerHTML = "";
  SLOT_ORDER = slots.map(s => s.name);
  SLOT_COLOR = {};
  slots.forEach((s, i) => { SLOT_COLOR[s.name] = SLOT_COLORS[i % SLOT_COLORS.length]; });
  window.SLOT_COLOR = SLOT_COLOR;

  for (const s of slots) {
    const opt = document.createElement("option");
    opt.value = s.name; opt.textContent = s.name;
    sel.appendChild(opt);
  }
  if (prev) sel.value = prev;

  // 슬롯 카드
  $("slots").innerHTML = slots.map(s => `
    <div class="slot" style="--slot-color:${SLOT_COLOR[s.name]}">
      <div class="slot-name">${s.name}<span class="slot-share">${(s.allocation_pct*100).toFixed(0)}%</span></div>
      <div class="slot-capital">${won(s.current_capital)}</div>
      <div class="slot-meta"><span>포지션 ${s.n_positions}</span><span>거래 ${s.n_trades}</span></div>
    </div>`).join("");

  // 배분 막대 — 목표 비율이 아니라 실제 자본 비중을 그린다
  const total = slots.reduce((a, s) => a + Number(s.current_capital || 0), 0);
  $("alloc-bar").innerHTML = slots.map(s =>
    `<span style="flex:${Math.max(Number(s.current_capital) || 0, 1)};background:${SLOT_COLOR[s.name]}"></span>`
  ).join("");
  $("alloc-legend").innerHTML = slots.map(s => {
    const share = total ? (Number(s.current_capital) / total * 100).toFixed(1) : "0.0";
    return `<span><i style="background:${SLOT_COLOR[s.name]}"></i>${s.name} ${share}%` +
           ` <span class="muted">(목표 ${(s.allocation_pct*100).toFixed(0)}%)</span></span>`;
  }).join("");
  window._slots = slots;
  return slots;
}

async function loadKpis() {
  let perf = [];
  try { perf = await (await fetch("/api/performance")).json(); } catch (e) { perf = []; }
  if (!Array.isArray(perf)) perf = [];
  const slots = window._slots || [];
  const capital = slots.reduce((a, s) => a + Number(s.current_capital || 0), 0);
  const pnl = perf.reduce((a, s) => a + Number(s.total_pnl || 0), 0);
  const closed = perf.reduce((a, s) => a + Number(s.n_closed || 0), 0);
  const wins = perf.reduce((a, s) =>
    a + (s.win_rate === null ? 0 : Number(s.win_rate) / 100 * Number(s.n_closed || 0)), 0);
  const winRate = closed ? (wins / closed * 100).toFixed(1) + "%" : "—";
  const open = perf.reduce((a, s) => a + Number(s.n_open_positions || 0), 0);
  const openCost = perf.reduce((a, s) => a + Number(s.open_cost || 0), 0);
  const cls = pnl > 0 ? "up" : (pnl < 0 ? "down" : "");
  $("kpis").innerHTML = `
    <div class="kpi"><div class="kpi-label">슬롯 자본 합계</div>
      <div class="kpi-value">${fmt(Math.round(capital))}<span class="unit">원</span></div>
      <div class="kpi-meta">슬롯 ${slots.length}개</div></div>
    <div class="kpi"><div class="kpi-label">실현손익</div>
      <div class="kpi-value delta ${cls}">${signed(pnl)}<span class="unit">원</span></div>
      <div class="kpi-meta">완결 ${closed}건</div></div>
    <div class="kpi"><div class="kpi-label">승률</div>
      <div class="kpi-value">${winRate}</div>
      <div class="kpi-meta">완결 거래 기준</div></div>
    <div class="kpi"><div class="kpi-label">보유</div>
      <div class="kpi-value">${open}<span class="unit">종목</span></div>
      <div class="kpi-meta">매입원가 ${fmt(Math.round(openCost))}원</div></div>`;
  $("asof").textContent = "모의 운용 중 · " + new Date().toLocaleString("ko-KR", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function renderChips(barId, counts, totalLabel) {
  const bar = $(barId);
  const order = SLOT_ORDER.length ? SLOT_ORDER : Object.keys(counts);
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  bar.innerHTML = `<button class="chip" aria-pressed="true" data-slot="all">전체 ${total}${totalLabel}</button>` +
    order.map(name =>
      `<button class="chip" aria-pressed="false" data-slot="${name}">` +
      `<i style="background:${SLOT_COLOR[name] || "var(--muted)"}"></i>${name} ${counts[name] || 0}</button>`
    ).join("");
}

function wireChips(barId, key) {
  $(barId).addEventListener("click", e => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    $(barId).querySelectorAll(".chip").forEach(c => c.setAttribute("aria-pressed", "false"));
    chip.setAttribute("aria-pressed", "true");
    const slot = chip.dataset.slot;
    document.querySelectorAll(`tbody[data-group="${key}"] tr`).forEach(row => {
      const match = slot === "all" || row.dataset.slot === slot;
      row.style.display = (row.classList.contains("empty") ? row.dataset.slot === slot : match) ? "" : "none";
    });
  });
}

async function loadPositions() {
  const positions = await (await fetch("/api/paper/positions")).json();
  const tbody = document.querySelector('tbody[data-group="pos"]');
  const counts = {};
  SLOT_ORDER.forEach(n => { counts[n] = 0; });
  positions.forEach(p => { counts[p.slot_name] = (counts[p.slot_name] || 0) + 1; });
  renderChips("chips-pos", counts, "종목");

  const order = SLOT_ORDER.length ? SLOT_ORDER : [...new Set(positions.map(p => p.slot_name))];
  let html = "";
  for (const slot of order) {
    const rows = positions.filter(p => p.slot_name === slot);
    const amount = rows.reduce((a, p) => a + p.quantity * p.avg_price, 0);
    const color = SLOT_COLOR[slot] || "var(--muted)";
    if (!rows.length) {
      html += `<tr class="group" data-slot="${slot}"><td colspan="4">
        <span class="group-label"><i style="background:${color}"></i>${slot}
        <span class="count">보유 없음</span></span></td></tr>`;
      continue;
    }
    html += `<tr class="group" data-slot="${slot}"><td colspan="3">
      <span class="group-label"><i style="background:${color}"></i>${slot}
      <span class="count">${rows.length}종목</span></span></td>
      <td class="num">${won(amount)}</td></tr>`;
    html += rows.map(p => `<tr data-slot="${slot}">
      <td class="indent">${p.name || ""}<span class="ticker">${p.ticker}</span></td>
      <td class="num">${fmt(p.quantity)}</td>
      <td class="num">${fmt(Math.round(p.avg_price))}</td>
      <td class="num">${won(p.quantity * p.avg_price)}</td></tr>`).join("");
  }
  tbody.innerHTML = html || `<tr><td colspan="4" class="muted">보유 포지션 없음</td></tr>`;
}

async function loadTrades() {
  const trades = await (await fetch("/api/paper/trades?limit=100")).json();
  const tbody = document.querySelector('tbody[data-group="trd"]');
  const counts = {};
  SLOT_ORDER.forEach(n => { counts[n] = 0; });
  trades.forEach(t => { counts[t.slot_name] = (counts[t.slot_name] || 0) + 1; });
  renderChips("chips-trd", counts, "건");

  if (!trades.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">거래 내역 없음</td></tr>`;
    return;
  }
  // 최신순 그대로 두고 봇은 색점으로 구분한다(시간 흐름이 우선).
  let html = trades.map(t => `<tr data-slot="${t.slot_name}">
      <td class="muted">${t.executed_at}</td>
      <td><span class="slot-tag"><i style="background:${SLOT_COLOR[t.slot_name] || "var(--muted)"}"></i>${t.slot_name}</span></td>
      <td>${t.name || ""}<span class="ticker">${t.ticker}</span></td>
      <td><span class="tag tag-${t.side}">${t.side === "buy" ? "매수" : "매도"}</span></td>
      <td class="num">${fmt(t.quantity)}</td>
      <td class="num">${fmt(Math.round(t.price))}</td>
      <td class="num">${fmt(Math.round(t.fees))}</td>
    </tr>`).join("");
  // 거래가 없는 봇을 칩으로 고르면 빈 표 대신 안내를 보여준다
  html += SLOT_ORDER.filter(n => !counts[n]).map(n =>
    `<tr class="empty" data-slot="${n}" style="display:none">
      <td colspan="7" class="muted">${n} 봇 거래가 아직 없습니다.</td></tr>`).join("");
  tbody.innerHTML = html;
}

wireChips("chips-pos", "pos");
wireChips("chips-trd", "trd");

async function reload() {
  await loadSlots();                       // 슬롯 색·순서를 먼저 확정
  await Promise.all([loadPositions(), loadTrades(), loadKpis()]);
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

const PAPER_DUPLICATE_GUARD_MS = 60_000;

async function submitOrder(side) {
  if (window._paperOrderPending) {
    return toast("주문 처리 중입니다. 잠시 기다려주세요.", true);
  }
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
  const signature = side + ":" + JSON.stringify(body);
  const lastAppliedAt = Number(window._paperLastAppliedAt || 0);
  if (
    window._paperLastAppliedSignature === signature &&
    Date.now() - lastAppliedAt < PAPER_DUPLICATE_GUARD_MS
  ) {
    return toast("같은 주문이 방금 처리됐습니다. 60초 후 다시 시도하거나 주문 내용을 변경하세요.", true);
  }
  if (window._paperOrderSignature !== signature) {
    window._paperOrderSignature = signature;
    window._paperOrderEventId = crypto.randomUUID();
  }
  body.source_event_id = window._paperOrderEventId;
  window._paperOrderPending = true;
  $("btn-buy").disabled = true;
  $("btn-sell").disabled = true;
  try {
    const r = await fetch(`/api/paper/${side}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({error: `HTTP ${r.status}`}));
    if (d.ok) {
      window._paperLastAppliedSignature = signature;
      window._paperLastAppliedAt = Date.now();
      window._paperOrderSignature = null;
      window._paperOrderEventId = null;
      toast(`${side === 'buy' ? '🟢 매수' : '🔴 매도'} 완료`);
      $("ticker").value = ""; $("name").value = "";
      $("quantity").value = ""; $("price").value = ""; $("notes").value = "";
      reload();
    } else {
      toast(`실패: ${d.error}`, true);
    }
  } catch (e) {
    toast(`주문 전송 실패: ${e.message}`, true);
  } finally {
    window._paperOrderPending = false;
    $("btn-buy").disabled = false;
    $("btn-sell").disabled = false;
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

// ── 전략 비교 탭 (v3.49) ─────────────────────────────────────────────────────
const STRAT_AXES = [["slot", "봇"], ["reason", "청산 사유"], ["hold", "보유 기간"],
                    ["era", "규칙 전후"], ["tag", "진입 태그"]];
let stratData = null, stratAxis = "slot";

function renderStratAxes() {
  document.getElementById("strat-axes").innerHTML = STRAT_AXES.map(
    ([k, t]) => `<button type="button" class="chip" data-axis="${k}" ` +
                `aria-pressed="${k === stratAxis}">${t}</button>`).join("");
}

function renderStratTable() {
  const c = stratData.axes[stratAxis];
  const tbody = document.getElementById("strat-tbody");
  document.getElementById("strat-title").textContent =
    `${c.title} (완결 ${c.total.n}건)`;
  document.getElementById("strat-caveat").textContent = c.caveat || "";
  const num = (x, u) => (x === null || x === undefined) ? "—" : x + (u || "");
  const won = x => (x === null || x === undefined) ? "—" : x.toLocaleString() + "원";
  tbody.innerHTML = c.rows.length ? c.rows.map(r => {
    const pcls = r.pnl > 0 ? "up" : (r.pnl < 0 ? "down" : "");
    const ecls = r.edge_ret === null ? "" : (r.edge_ret > 0 ? "up" : (r.edge_ret < 0 ? "down" : ""));
    const edge = r.edge_ret === null ? "—" :
                 (r.edge_ret > 0 ? "+" : "") + r.edge_ret + "%p";
    return `<tr>
      <td>${r.label}${r.small ? '<span class="ticker">†</span>' : ""}</td>
      <td class="num">${r.n}</td>
      <td class="num">${num(r.share_n, "%")}</td>
      <td class="num">${num(r.win_rate, "%")}</td>
      <td class="num delta ${pcls}">${won(r.pnl)}</td>
      <td class="num">${num(r.avg_ret, "%")}</td>
      <td class="num delta ${ecls}">${edge}</td>
      <td class="num">${num(r.profit_factor)}</td>
      <td class="num">${num(r.avg_hold_days, "일")}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="9" class="muted">해당 축에 거래가 없습니다.</td></tr>`;
}

function renderStratAttribution() {
  const a = stratData.attribution;
  const box = document.getElementById("strat-attr");
  const cards = [];
  const exc = stratData.excluded || {n: 0};
  const flg = stratData.flagged || {n: 0};
  if (exc.n) {
    cards.push(`<div class="card">
      <div class="slot-name">데이터 품질 제외 ${exc.n}건 · ${exc.pnl.toLocaleString()}원</div>
      <p class="muted" style="margin-top:8px;">
        진입가가 며칠 전 종가와 원 단위까지 일치한 자동 매수 배치입니다. 시장이 낸 손익이
        아니라 <b>가격 오류가 만든 손익</b>이라 아래 표에서 뺐습니다.
        <b>기록은 DB에 그대로 남아 있습니다.</b> 손실 배치와 수익 배치를 같은 기준으로
        함께 뺐습니다 — 유리한 쪽만 고르면 그게 더 큰 왜곡입니다.
      </p>
      <div class="stat-rows" style="margin-top:10px;">
        ${(exc.rows || []).slice(0, 12).map(r => `<span>${r.name || r.ticker} · ${(r.buy_at||"").slice(0,16)}</span>` +
          `<span>${r.ret > 0 ? "+" : ""}${r.ret}% · ${r.pnl.toLocaleString()}원</span>`).join("")}
      </div>
    </div>`);
  }
  if (flg.n) {
    cards.push(`<div class="card" style="margin-top:20px;">
      <div class="slot-name">체결 가정 편향 표시 ${flg.n}건 · ${flg.pnl.toLocaleString()}원</div>
      <p class="muted" style="margin-top:8px;">
        장외 시각이나 개장 직후에 기록돼 <b>전 거래일 종가</b>가 진입가로 남은 건입니다.
        판단은 실제였으므로 <b>집계에는 포함</b>했고 표시만 합니다. 앞으로는 대기 큐가
        개장 시 실제 가격으로 다시 판단합니다.
      </p>
    </div>`);
  }
  const odd = stratData.outliers || [];
  if (odd.length) {
    cards.push(`<div class="card">
      <div class="slot-name">수익률 이상치 ${odd.length}건 · ${stratData.outlier_pnl.toLocaleString()}원</div>
      <p class="muted" style="margin-top:8px;">
        아래 거래는 스윙 매매에서 나올 수 없는 수익률입니다. 초기에 UI를 시험하며
        매수가를 임의로 입력한 기록으로 보입니다. <b>표에는 그대로 포함되어 있습니다</b> —
        조용히 빼면 나중에 왜 숫자가 달라졌는지 알 수 없기 때문입니다. 정리 여부는 직접 판단하세요.
      </p>
      <div class="stat-rows" style="margin-top:10px;">
        ${odd.map(o => `<span>${o.name || o.ticker} · ${o.slot}</span>` +
          `<span>${o.ret > 0 ? "+" : ""}${o.ret}% · ${o.pnl.toLocaleString()}원 · ${(o.sell_at || "").slice(0, 10)}</span>`).join("")}
      </div>
    </div>`);
  }
  if (a.usable) { box.innerHTML = cards.join(""); return; }
  cards.push(`<div class="card" style="margin-top:20px;">
    <div class="slot-name">진입 전략 귀속 ${a.tagged}/${a.total}건 (${a.coverage === null ? "—" : a.coverage + "%"})</div>
    <p class="muted" style="margin-top:8px;">
      진입 조건(추세 위 눌림 · 정배열 · 신고가 돌파 · 과낙폭 반등)이 매수 기록에 남지 않아
      <b>진입 조건별 비교는 아직 불가능</b>합니다. 아래 표는 봇·청산 사유·보유 기간·규칙 시기로만
      나눈 것이며, 어느 진입 전략이 나은지는 말해 주지 않습니다.
    </p>
  </div>`);
  box.innerHTML = cards.join("");
}

async function loadStrategyCompare() {
  const status = document.getElementById("strat-status");
  status.textContent = "로딩 중...";
  try {
    const d = await (await fetch("/api/strategy/compare")).json();
    if (d.error) throw new Error(d.error);
    stratData = d;
    renderStratAxes();
    renderStratAttribution();
    renderStratTable();
    status.textContent = `완결 ${d.n}건 · ` + new Date().toLocaleTimeString();
  } catch (e) {
    status.textContent = "실패: " + e.message;
  }
}

document.getElementById("strat-axes").addEventListener("click", e => {
  const chip = e.target.closest(".chip");
  if (!chip || !stratData) return;
  stratAxis = chip.dataset.axis;
  renderStratAxes();
  renderStratTable();
});
document.getElementById("btn-strat-refresh").addEventListener("click", loadStrategyCompare);
document.querySelector(".tab[data-tab='tab-strategy']").addEventListener("click", () => {
  if (!stratData) loadStrategyCompare();
});

// ── 신호 정확도 탭 (v3.48) ───────────────────────────────────────────────────
function sigaccCard(title, a) {
  const pct = v => (v === null || v === undefined) ? "—" : v + "%";
  const edge = a.avg_edge;
  const cls = edge === null ? "" : (edge > 0 ? "up" : (edge < 0 ? "down" : ""));
  return `<div class="slot">
    <div class="slot-name">${title}</div>
    <div class="slot-capital delta ${cls}">${edge === null ? "—" : (edge > 0 ? "+" : "") + edge + "%p"}</div>
    <div class="stat-rows">
      <span>표본</span><span>${a.n}건</span>
      <span>적중률</span><span>${pct(a.hit_rate)}</span>
      <span>평균 수익률</span><span>${pct(a.avg_ret)}</span>
      <span>판정</span><span>${a.verdict}</span>
    </div>
  </div>`;
}

async function loadSignalAccuracy() {
  const status = document.getElementById("sigacc-status");
  const box = document.getElementById("sigacc-summary");
  const tbody = document.getElementById("sigacc-tbody");
  const horizon = document.getElementById("sigacc-horizon").value;
  status.textContent = "로딩 중...";
  try {
    const d = await (await fetch(`/api/signal/accuracy?horizon=${horizon}`)).json();
    if (d.error) throw new Error(d.error);
    const s = d.summary;
    const cards = [sigaccCard("전체", s.total),
                   sigaccCard("발송된 신호", s.mtf.sent),
                   sigaccCard("MTF 억제분", s.mtf.suppressed)];
    for (const [k, a] of Object.entries(s.by_strategy)) cards.push(sigaccCard(k, a));
    box.innerHTML = cards.join("");

    const rows = d.recent || [];
    const key = `ret_${horizon}d`, bkey = `base_${horizon}d`, ekey = `edge_${horizon}d`;
    tbody.innerHTML = rows.length ? rows.map(r => {
      const v = r[key], e = r[ekey];
      const cls = v === null ? "" : (v > 0 ? "up" : (v < 0 ? "down" : ""));
      const ecls = e === null ? "" : (e > 0 ? "up" : (e < 0 ? "down" : ""));
      const num = x => (x === null || x === undefined) ? "—" : x + "%";
      return `<tr>
        <td class="muted">${r.at || ""}</td>
        <td>${r.name || ""}<span class="ticker">${r.ticker || ""}</span></td>
        <td>${r.strategy || ""}</td>
        <td>${r.action || ""}</td>
        <td class="num delta ${cls}">${num(v)}</td>
        <td class="num">${num(r[bkey])}</td>
        <td class="num delta ${ecls}">${num(e)}</td>
        <td>${r.suppressed ? "억제" : "발송"}</td>
      </tr>`;
    }).join("") : `<tr><td colspan="8" class="muted">아직 평가된 신호가 없습니다.</td></tr>`;

    status.textContent = `평가 ${d.evaluated}건 · 대기 ${d.pending}건 · ` +
      new Date().toLocaleTimeString();
  } catch (e) {
    status.textContent = "실패: " + e.message;
  }
}
document.getElementById("btn-sigacc-refresh").addEventListener("click", loadSignalAccuracy);
document.getElementById("sigacc-horizon").addEventListener("change", loadSignalAccuracy);
document.querySelector(".tab[data-tab='tab-signal-acc']").addEventListener("click", loadSignalAccuracy);

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
      const cls = s.total_pnl > 0 ? "up" : (s.total_pnl < 0 ? "down" : "");
      const retCls = s.total_return_pct > 0 ? "up" : (s.total_return_pct < 0 ? "down" : "");
      const wrText = s.win_rate !== null ? s.win_rate + "%" : "—";
      const sharpeText = (s.trade_sharpe !== null && s.trade_sharpe !== undefined)
        ? parseFloat(s.trade_sharpe).toFixed(2) : "—";
      const color = (window.SLOT_COLOR || {})[s.slot_name] || "var(--accent)";
      return `<div class="slot" style="--slot-color:${color}">
        <div class="slot-name">${s.slot_name}</div>
        <div class="slot-capital delta ${cls}">
          ${s.total_pnl > 0 ? "+" : ""}${s.total_pnl.toLocaleString()}원</div>
        <div class="stat-rows">
          <span>승률</span><span>${wrText}</span>
          <span>수익률</span><span class="delta ${retCls}">${s.total_return_pct > 0 ? "+" : ""}${s.total_return_pct}%</span>
          <span>MDD</span><span>${s.max_drawdown_pct}%</span>
          <span>거래당 샤프</span><span>${sharpeText}</span>
          <span>보유</span><span>${s.n_open_positions}종목</span>
          <span>완결</span><span>${s.n_closed}건</span>
        </div>
      </div>`;
    }).join("");

    tbody.innerHTML = data.map(s => {
      const cls = s.total_pnl > 0 ? "up" : (s.total_pnl < 0 ? "down" : "");
      const retCls = s.total_return_pct > 0 ? "up" : (s.total_return_pct < 0 ? "down" : "");
      return `<tr>
        <td>${s.slot_name}</td>
        <td class="num">${s.n_closed}</td>
        <td class="num">${s.win_rate !== null ? s.win_rate + "%" : "—"}</td>
        <td class="num delta ${cls}">
          ${s.total_pnl > 0 ? "+" : ""}${s.total_pnl.toLocaleString()}원</td>
        <td class="num delta ${retCls}">
          ${s.total_return_pct > 0 ? "+" : ""}${s.total_return_pct}%</td>
        <td class="num">${s.max_drawdown_pct}%</td>
        <td class="num">${(s.trade_sharpe !== null && s.trade_sharpe !== undefined) ? s.trade_sharpe : "—"}</td>
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
  const sourceEventId = crypto.randomUUID();
  try {
    const res = await (await fetch("/api/paper/buy", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({slot: "마이퀀트", ticker: it.ticker, name: it.name,
                            quantity: parseInt(qty), price: it.price, notes,
                            source_event_id: sourceEventId})
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

    _configure_paper_write_runtime(load_private_write_runtime_bundle())

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    log.info(f"📑 Paper Trading UI: http://{args.host}:{args.port}")
    log.info(f"   DB: {pdb.DEFAULT_DB_PATH}")
    log.info(f"   Seed: {pdb.DEFAULT_SEED_KRW:,}원, Fee: {pdb.DEFAULT_FEE_BPS}bp")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
