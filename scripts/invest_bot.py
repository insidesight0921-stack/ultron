#!/usr/bin/env python3
"""
투자봇 (invest_bot) — pykrx 차트 분석 + Wiki 매매 규칙 대조.

4단계 네 번째 도구. 단일 종목에 대해:
  1. Shareable API/전용 수집기로 일봉 OHLCV 6개월 가져옴
  2. 순수 계산 함수로 RSI, MA(5/20/60/120), 변동성, 거래량 비율 산출
  3. 골든/데드크로스 발생 여부, 과매수/과매도 신호 판정
  4. (mode='accurate'면) wiki 매매 규칙 RAG → 31B에게 "조건 충족인지" 평가

⚠️ 실주문 자동화는 절대 하지 않음. 신호만 산출해서 사람이 판단. KIS 연동은
   5단계 paper trading 6개월 검증 후 결정.

API:
  resolve_ticker(query: str) -> tuple[str, str] | None    # 종목명/티커 → (ticker, name)
  fetch_ohlcv(ticker, start, end) -> pd.DataFrame
  compute_indicators(df) -> dict
  detect_signals(indicators) -> list[str]
  analyze(query) -> dict       # 위 단계 묶음
  compare_with_rules(query) -> tuple[str, list[dict]]   # + wiki RAG + 31B
  run(action, ticker_or_name=None, mode="accurate") -> tuple[str, list]

설계 결정:
- OHLCV와 ticker map 외부 수집·Shareable 쓰기는 market_data_collector에 위임하고,
  소비 경로는 Data API를 우선 사용 → 테스트는 monkeypatch로 외부 의존성 격리.
- 종목명↔티커 매핑은 in-memory 캐시 (TTL 24h). 첫 호출 시 ~1초.
- mode='fast': 지표만 출력 (LLM 무호출, ~2초)
  mode='accurate': 지표 + wiki 매매 규칙 + 31B 평가 (~50초)
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

from storage_paths import PATHS
from data_api_client import DataAPIError, ShareableDataClient, data_api_enabled
from market_data_collector import (
    collect_ohlcv,
    fetch_ticker_map_data,
    write_ticker_map_cache,
)

# ask.py 와 같은 RAG·LLM
from ask import retrieve, build_context, LLM_MODEL

OLLAMA_URL = "http://127.0.0.1:11434"
KEEP_ALIVE = "30m"

# 종목 매핑 캐시 TTL (24시간)
TICKER_MAP_TTL_SEC = 24 * 60 * 60

log = logging.getLogger("invest_bot")


# ─── 종목 매핑 ───────────────────────────────────────


_TICKER_MAP_CACHE: dict[str, tuple[float, dict]] = {}
_DATA_API_CLIENT = ShareableDataClient()

# 디스크 캐시 (봇 재시작 후에도 첫 호출 패널티 제거)
# 테스트에서 monkeypatch 가능하도록 모듈 레벨 변수
_DISK_CACHE_DIR: Path = PATHS.shareable_cache_dir


def _disk_cache_path(date: str) -> Path:
    return _DISK_CACHE_DIR / f"ticker_map_{date}.json"


def _load_disk_cache(date: str) -> dict[str, str] | None:
    """디스크에 저장된 ticker map 로드. TTL 지났거나 손상이면 None."""
    p = _disk_cache_path(date)
    if not p.exists():
        return None
    try:
        if time.time() - p.stat().st_mtime >= TICKER_MAP_TTL_SEC:
            return None
        m = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(m, dict) or not m:
            return None
        return m
    except Exception as e:
        log.warning(f"ticker map 디스크 캐시 로드 실패 — 무시하고 fresh fetch: {e}")
        return None


def _save_disk_cache(date: str, m: dict[str, str]) -> None:
    """호환 wrapper. ticker map 쓰기 구현은 market_data_collector가 소유한다."""
    try:
        write_ticker_map_cache(date, m, cache_dir=_DISK_CACHE_DIR)
    except Exception as e:
        log.warning(f"ticker map 디스크 캐시 저장 실패 — 무시: {e}")


def _fetch_ticker_map_raw(date: str) -> dict[str, str]:
    """호환 wrapper. 실제 외부 수집은 market_data_collector가 소유한다."""
    return fetch_ticker_map_data(date)


def get_ticker_map(force_refresh: bool = False) -> dict[str, str]:
    """ticker → 종목명 매핑. 24시간 캐시 (in-memory + 디스크).

    조회 우선순위:
      1. in-memory 캐시 (TTL 안 지났으면)
      2. Shareable 디스크 캐시 ticker_map_<YYYYMMDD>.json (TTL 안 지났으면)
         → in-memory에도 채워서 다음 호출은 바로 hit
      3. KRX fetch — in-memory + 디스크 동시 저장

    봇 재시작 직후 첫 호출도 같은 날 디스크 캐시가 있으면 ~ms로 끝남.
    """
    today = datetime.now().strftime("%Y%m%d")

    # 1. in-memory
    cached = _TICKER_MAP_CACHE.get(today)
    if cached and not force_refresh:
        ts, m = cached
        if time.time() - ts < TICKER_MAP_TTL_SEC:
            return m

    # 2. 디스크
    if not force_refresh:
        disk = _load_disk_cache(today)
        if disk is not None:
            _TICKER_MAP_CACHE[today] = (time.time(), disk)
            return disk

    # 3. fresh fetch
    m = _fetch_ticker_map_raw(today)
    _TICKER_MAP_CACHE[today] = (time.time(), m)
    _save_disk_cache(today, m)
    return m


def _resolve_ticker_via_api(query: str) -> tuple[str, str] | None:
    if query.isdigit() and len(query) == 6:
        item = _DATA_API_CLIENT.get_instrument(query)
        if item is None:
            return None
        return str(item["ticker"]), str(item["name"])
    items = _DATA_API_CLIENT.search_instruments(query, limit=20)
    if not items:
        return None
    exact = [item for item in items if item["name"] == query]
    selected = exact[0] if exact else min(items, key=lambda item: len(item["name"]))
    return str(selected["ticker"]), str(selected["name"])


def resolve_ticker(query: str) -> tuple[str, str] | None:
    """종목명 또는 티커 → (ticker, name). 못 찾으면 None.

    매칭 우선순위:
      1. 6자리 숫자 티커 직접 입력
      2. 정확한 이름 일치 ('삼성전자')
      3. 부분 일치 ('삼성전자우' 검색 시 '삼성전자' 우선) — 가장 짧은 매치
    """
    q = (query or "").strip()
    if not q:
        return None

    if data_api_enabled():
        try:
            resolved = _resolve_ticker_via_api(q)
            if resolved is not None:
                return resolved
        except (DataAPIError, ValueError) as exc:
            log.warning("Shareable Data API 조회 실패 — 기존 캐시 경로 사용: %s", exc)

    # 1. 티커 형식
    if q.isdigit() and len(q) == 6:
        try:
            tmap = get_ticker_map()
        except Exception as e:
            log.warning(f"종목 매핑 fetch 실패 (티커 직접 입력이라 무시): {e}")
            return (q, q)  # 이름 못 가져와도 티커는 유효
        return (q, tmap.get(q, q))

    # 2~3. 이름 매칭
    try:
        tmap = get_ticker_map()
    except Exception as e:
        log.error(f"종목 매핑 fetch 실패: {e}")
        return None

    # 정확 일치
    for t, name in tmap.items():
        if name == q:
            return (t, name)

    # 부분 일치 — 가장 짧은 이름 우선 (보통 본주가 짧음)
    matches = [(t, name) for t, name in tmap.items() if q in name]
    if not matches:
        return None
    matches.sort(key=lambda x: len(x[1]))
    return matches[0]


# ─── OHLCV ──────────────────────────────────────────


def _fetch_ohlcv_raw(ticker: str, start: str, end: str):
    """호환 wrapper. 외부 수집·쓰기는 market_data_collector가 소유한다."""
    payload = collect_ohlcv(
        ticker,
        start,
        end,
        ohlcv_dir=PATHS.shareable_cache_dir / "ohlcv",
    )
    return _ohlcv_payload_to_frame(payload)


def _ohlcv_payload_to_frame(payload: dict):
    import pandas as pd

    series = payload.get("series") if isinstance(payload, dict) else None
    if not isinstance(series, dict) or not isinstance(series.get("close"), list):
        raise ValueError("OHLCV payload 형식이 올바르지 않습니다.")
    column_map = {
        "open": "시가",
        "high": "고가",
        "low": "저가",
        "close": "종가",
        "volume": "거래량",
    }
    columns = {
        korean: series[field]
        for field, korean in column_map.items()
        if isinstance(series.get(field), list)
    }
    frame = pd.DataFrame(columns)
    dates = series.get("date") or series.get("dates")
    if isinstance(dates, list) and len(dates) == len(frame):
        frame.index = pd.to_datetime(dates, format="%Y%m%d")
    return frame


def fetch_ohlcv(ticker: str, days: int = 180):
    """최근 days일치 일봉. Data API 우선, 미스 시 전용 수집기를 사용한다."""
    end = datetime.now()
    start = end - timedelta(days=days * 2)  # 비영업일 고려해 넉넉히
    if data_api_enabled():
        try:
            payload = _DATA_API_CLIENT.latest_ohlcv(ticker)
            if payload is not None:
                return _ohlcv_payload_to_frame(payload)
        except (DataAPIError, ValueError) as exc:
            log.warning("Shareable Data API OHLCV 실패 — collector 사용: %s", exc)
    return _fetch_ohlcv_raw(
        ticker, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    )


# ─── 지표 계산 (순수 함수) ───────────────────────────


def _rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI(n). 데이터 부족하면 None."""
    if len(closes) < n + 1:
        return None
    gains = []
    losses = []
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    # Wilder smoothing
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = max(d, 0)
        loss = max(-d, 0)
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _ma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    var = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


def _annualized_vol(closes: list[float], n: int = 21) -> float | None:
    """일별 로그수익률의 표준편차 × sqrt(252) — 연환산 변동성(%)."""
    if len(closes) < n + 1:
        return None
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            continue
        rets.append(math.log(closes[i] / closes[i - 1]))
    rets = rets[-n:]
    if len(rets) < 2:
        return None
    return _stddev(rets) * math.sqrt(252) * 100


def _crossed(prev_short, prev_long, cur_short, cur_long) -> str:
    """이전 봉 vs 오늘 봉의 단/장기 MA 교차 판정."""
    if None in (prev_short, prev_long, cur_short, cur_long):
        return "none"
    if prev_short < prev_long and cur_short >= cur_long:
        return "golden"
    if prev_short > prev_long and cur_short <= cur_long:
        return "dead"
    return "none"


def compute_indicators(df) -> dict:
    """DataFrame(OHLCV) → 지표 dict.

    DataFrame 요구 컬럼: '종가' (있으면 우선) 또는 'Close', 그리고 '거래량'/'Volume'.
    pykrx 한국어 컬럼 사용.
    """
    if df is None or len(df) == 0:
        return {"error": "데이터 없음"}

    # 컬럼명 보정
    close_col = "종가" if "종가" in df.columns else ("Close" if "Close" in df.columns else None)
    vol_col = "거래량" if "거래량" in df.columns else ("Volume" if "Volume" in df.columns else None)
    if close_col is None:
        return {"error": "종가 컬럼 없음"}

    closes = [float(v) for v in df[close_col].tolist()]
    vols = [float(v) for v in (df[vol_col].tolist() if vol_col else [])]

    last_close = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else last_close
    pct_change = ((last_close - prev_close) / prev_close * 100) if prev_close else 0.0

    ma5 = _ma(closes, 5)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    ma120 = _ma(closes, 120)
    rsi = _rsi(closes, 14)
    vol21 = _annualized_vol(closes, 21)
    vol63 = _annualized_vol(closes, 63)

    # 골든/데드크로스 (전일 vs 금일)
    cross_5_20 = "none"
    cross_20_60 = "none"
    if len(closes) >= 21:
        prev_closes = closes[:-1]
        cross_5_20 = _crossed(
            _ma(prev_closes, 5), _ma(prev_closes, 20), ma5, ma20
        )
    if len(closes) >= 61:
        prev_closes = closes[:-1]
        cross_20_60 = _crossed(
            _ma(prev_closes, 20), _ma(prev_closes, 60), ma20, ma60
        )

    # 거래량 비율 (오늘 / 20일 평균)
    vol_ratio = None
    if vols and len(vols) >= 20:
        avg20 = sum(vols[-20:]) / 20
        if avg20 > 0:
            vol_ratio = vols[-1] / avg20

    return {
        "last_close": last_close,
        "prev_close": prev_close,
        "pct_change": pct_change,
        "MA5": ma5,
        "MA20": ma20,
        "MA60": ma60,
        "MA120": ma120,
        "RSI14": rsi,
        "vol21_ann_pct": vol21,
        "vol63_ann_pct": vol63,
        "cross_5_20": cross_5_20,
        "cross_20_60": cross_20_60,
        "vol_ratio_20d": vol_ratio,
        "samples": len(closes),
    }


# ─── 신호 판정 ───────────────────────────────────────


def detect_signals(ind: dict) -> list[str]:
    """지표 dict → 사람이 읽을 수 있는 신호 리스트."""
    if "error" in ind:
        return []
    sigs: list[str] = []

    rsi = ind.get("RSI14")
    if rsi is not None:
        if rsi < 30:
            sigs.append(f"과매도 (RSI {rsi:.1f})")
        elif rsi > 70:
            sigs.append(f"과매수 (RSI {rsi:.1f})")

    if ind.get("cross_5_20") == "golden":
        sigs.append("MA5↗MA20 골든크로스")
    elif ind.get("cross_5_20") == "dead":
        sigs.append("MA5↘MA20 데드크로스")

    if ind.get("cross_20_60") == "golden":
        sigs.append("MA20↗MA60 중장기 골든크로스")
    elif ind.get("cross_20_60") == "dead":
        sigs.append("MA20↘MA60 중장기 데드크로스")

    last = ind.get("last_close")
    ma20 = ind.get("MA20")
    if last is not None and ma20 is not None:
        if last > ma20:
            sigs.append(f"MA20 위 ({((last - ma20) / ma20 * 100):+.1f}%)")
        else:
            sigs.append(f"MA20 아래 ({((last - ma20) / ma20 * 100):+.1f}%)")

    vr = ind.get("vol_ratio_20d")
    if vr is not None:
        if vr > 2:
            sigs.append(f"거래량 폭증 ({vr:.1f}× 20일 평균)")
        elif vr < 0.5:
            sigs.append(f"거래량 위축 ({vr:.1f}×)")

    v21 = ind.get("vol21_ann_pct")
    v63 = ind.get("vol63_ann_pct")
    if v21 is not None and v63 is not None and v63 > 0:
        if v21 > v63 * 1.5:
            sigs.append(f"단기 변동성 급등 ({v21:.0f}% > 63일 {v63:.0f}%×1.5)")

    return sigs


# ─── analyze (지표만) ────────────────────────────────


def analyze(query: str) -> dict:
    """종목 한 개 → 지표 + 신호 dict.

    실패 시 {'error': '...'} 형태 반환.
    """
    resolved = resolve_ticker(query)
    if resolved is None:
        return {"error": f"종목을 찾지 못했습니다: {query!r}"}
    ticker, name = resolved

    try:
        df = fetch_ohlcv(ticker, days=180)
    except Exception as e:
        log.exception(f"OHLCV fetch 실패 {ticker}")
        return {"error": f"시세 조회 실패: {e}"}

    indicators = compute_indicators(df)
    if "error" in indicators:
        return {"error": indicators["error"], "ticker": ticker, "name": name}

    signals = detect_signals(indicators)
    return {
        "ticker": ticker,
        "name": name,
        "indicators": indicators,
        "signals": signals,
    }


def format_analysis(result: dict) -> str:
    if "error" in result:
        return f"❌ {result['error']}"
    name = result["name"]
    ticker = result["ticker"]
    ind = result["indicators"]
    sigs = result["signals"]

    lines = [
        f"📈 {name} ({ticker})",
        f"종가: {ind['last_close']:,.0f}원 ({ind['pct_change']:+.2f}%)",
    ]
    if ind.get("RSI14") is not None:
        lines.append(f"RSI(14): {ind['RSI14']:.1f}")
    ma_line = []
    for k in ("MA5", "MA20", "MA60", "MA120"):
        v = ind.get(k)
        if v is not None:
            ma_line.append(f"{k}={v:,.0f}")
    if ma_line:
        lines.append("이동평균: " + " / ".join(ma_line))
    if ind.get("vol21_ann_pct") is not None:
        lines.append(
            f"변동성(연환산): 21일 {ind['vol21_ann_pct']:.0f}% / "
            f"63일 {ind['vol63_ann_pct']:.0f}%"
            if ind.get("vol63_ann_pct") is not None
            else f"변동성(연환산 21일): {ind['vol21_ann_pct']:.0f}%"
        )
    if sigs:
        lines.append("")
        lines.append("🚨 신호")
        for s in sigs:
            lines.append(f"  • {s}")
    else:
        lines.append("\n(눈에 띄는 신호 없음)")
    return "\n".join(lines)


# ─── compare_with_rules (지표 + RAG + 31B) ────────────


RULE_QUERIES = [
    "매매 진입 조건",
    "매매 청산 조건",
    "리스크 관리 원칙",
    "수급 판단 기준",
]


COMPARE_SYSTEM_PROMPT = """당신은 현준의 투자 비서입니다.

지금부터 [종목 지표·신호]와 [현준의 매매 원칙 노트]가 주어집니다.
두 정보만 근거로 한국어로 간결하게 평가하세요.

1. 진입 조건과 정합/불일치 — 어떤 진입 신호가 충족되거나 임박?
2. 청산/손절 조건과 정합/불일치 — 보유 중일 경우 트리거?
3. 결론: 매수/홀드/관망/손절 중 어느 쪽에 가까운가? (단정 금지, "원칙상 ~에 가깝다")

규칙:
- 지표·노트에 없는 사실은 추측 금지.
- 매매 권유·구체 가격/수량 결정 절대 금지.
- 답변에 [출처] 표기 금지 (시스템이 자동 표시).
"""


def _gather_rules(k_each: int = 2) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    chunks: list[dict] = []
    for q in RULE_QUERIES:
        try:
            res = retrieve(q, k=k_each)
        except Exception as e:
            log.warning(f"RAG 검색 실패({q}): {e}")
            continue
        for c in res:
            sig = (c.get("file", ""), c.get("section", ""))
            if sig in seen:
                continue
            seen.add(sig)
            chunks.append(c)
    return chunks


def compare_with_rules(query: str) -> tuple[str, list[dict]]:
    """analyze + wiki 매매 규칙 RAG → 31B 평가. (answer, chunks)."""
    result = analyze(query)
    if "error" in result:
        return (format_analysis(result), [])
    indicators_text = format_analysis(result)

    chunks = _gather_rules(k_each=2)
    if not chunks:
        return (
            indicators_text + "\n\n⚠️ wiki에서 매매 규칙 노트를 찾지 못했습니다.",
            [],
        )

    context = build_context(chunks)
    user_prompt = (
        f"[종목 지표·신호]\n{indicators_text}\n\n"
        f"---\n\n"
        f"[현준의 매매 원칙 노트]\n{context}\n\n"
        f"---\n\n"
        "위 두 정보를 대조해 평가해 주세요."
    )

    body = json.dumps(
        {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": COMPARE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": 0.3, "num_ctx": 32768},
        }
    ).encode("utf-8")
    req = Request(
        f"{OLLAMA_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=300) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.exception("compare_with_rules LLM 호출 실패")
        return (f"{indicators_text}\n\n❌ LLM 평가 실패: {e}", chunks)

    answer = (resp.get("message", {}).get("content") or "").strip()
    final = f"{indicators_text}\n\n---\n\n{answer}"
    return (final, chunks)


# ─── 라우터 entrypoint ───────────────────────────────


def run(
    action: str,
    ticker_or_name: str | None = None,
    mode: str = "accurate",
    **kwargs,
) -> tuple[str, list[dict]]:
    """라우터에서 호출하는 단일 entrypoint.

    action ∈ {"analyze", "compare_with_rules"}
    mode   ∈ {"fast", "accurate"}  — fast이고 action='compare'면 analyze로 강제 강등.
                                     fast이면 LLM 호출 없는 빠른 응답.
    """
    action = (action or "").strip().lower()
    mode = (mode or "accurate").strip().lower()
    if mode not in ("fast", "accurate"):
        mode = "accurate"

    if action in ("analyze", "compare_with_rules"):
        if not ticker_or_name:
            return ("❌ 종목명 또는 티커가 필요합니다 (예: '삼성전자', '005930').", [])

    if action == "analyze":
        result = analyze(ticker_or_name)
        return (format_analysis(result), [])

    if action == "compare_with_rules":
        if mode == "fast":
            # fast 모드는 LLM 안 부르고 지표만
            result = analyze(ticker_or_name)
            return (
                format_analysis(result) + "\n\n💡 fast 모드: 원칙 대조는 생략 (정확도 모드로 다시 물어보세요).",
                [],
            )
        return compare_with_rules(ticker_or_name)

    return (f"❌ 알 수 없는 action: {action!r} (analyze/compare_with_rules)", [])


# ─── CLI ─────────────────────────────────────────────


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_an = sub.add_parser("analyze")
    p_an.add_argument("query")
    p_cmp = sub.add_parser("compare")
    p_cmp.add_argument("query")
    p_cmp.add_argument("--mode", default="accurate", choices=["fast", "accurate"])
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.cmd == "analyze":
        msg, _ = run("analyze", ticker_or_name=args.query)
        print(msg)
    elif args.cmd == "compare":
        msg, srcs = run("compare_with_rules", ticker_or_name=args.query, mode=args.mode)
        print(msg)
        if srcs:
            print("\n📚 참조 노트")
            for c in srcs:
                print(f"  · {c['file']} :: {c.get('section', '')}")


if __name__ == "__main__":
    _cli()
