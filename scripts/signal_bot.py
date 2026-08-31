"""signal_bot.py — 기술적 분석 매매 신호 봇 (v1)

[[기술적_분석_활용지침]] + [[핵심_자산배분_포트폴리오]] 연동.

핵심 자산배분(위험65/안전30/현금5) 중 투자 종목 95%에 대해 **1시간봉** 기준으로
자산군별 보조 지표를 계산해 매매 신호를 산출한다. 실주문은 절대 하지 않으며
텔레그램 알림(telegram_bot의 장중 JobQueue)으로 전송만 한다.

지표 배정 (지침):
  - MACD            : 미국 대형 지수 추종 (나스닥100·필반) — 0선 위 안착 = 홀딩 유지
  - 볼린저+20MA      : 박스권 (코스피200·코스닥150) — 하단 매수/상단 매도, Band Walk 즉시 매도
  - StochRSI+거래량  : 나머지 섹터·테마·FX — 과매도 + 거래량 골든크로스 진입 타점

설계 원칙 (기존 봇 패턴 계승):
  - 외부 호출(_fetch_intraday_raw / _fetch_etf_map_raw)은 분리 → 테스트는 monkeypatch
  - 지표 계산은 순수 함수(결정론) → 단위 테스트로 검증
  - 빈 응답/예외 graceful skip (v3.21 가드 패턴)
  - 코드는 런타임 pykrx로 해석, 디스크 캐시
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from storage_paths import PATHS

log = logging.getLogger("signal_bot")

# ─── 워치리스트 (핵심_자산배분_포트폴리오 SSOT) ─────────────
# strategy: "macd" | "bollinger" | "stochrsi" | "monitor"
# fx/선물은 yfinance 심볼 직접 지정(KRX ETF 아님).


@dataclass
class WatchItem:
    name: str          # ETF/자산 이름 (pykrx 검색용)
    weight: float      # 포트폴리오 비중(%)
    strategy: str      # 적용 지표
    asset_class: str   # 분류
    code: Optional[str] = None         # KRX 6자리 코드(직접 지정 — 리스트 API 불필요)
    yf_override: Optional[str] = None  # yfinance 심볼 직접 지정(선물 등)


_FALLBACK_WATCHLIST: list[WatchItem] = [
    # 위험자산 (65%) — 코드는 FinanceDataReader ETF/KR로 확정(2026-06)
    WatchItem("KODEX 미국나스닥100", 5, "macd", "해외주식_지수", code="379810"),
    WatchItem("TIGER 미국필라델피아반도체나스닥", 5, "macd", "해외주식_지수", code="381180"),
    WatchItem("KODEX 미국AI전력핵심인프라", 4, "stochrsi", "해외주식_섹터", code="487230"),
    WatchItem("ACE 테슬라밸류체인액티브", 4, "stochrsi", "해외주식_섹터", code="457480"),
    WatchItem("KODEX 미국S&P500커뮤니케이션", 2, "stochrsi", "해외주식_섹터", code="463690"),
    WatchItem("TIGER 200", 20, "bollinger", "국내주식_지수", code="102110"),
    WatchItem("KODEX 코스닥150", 5, "bollinger", "국내주식_지수", code="229200"),
    WatchItem("KODEX 반도체", 6, "stochrsi", "국내주식_섹터", code="091160"),
    WatchItem("TIGER K방산&우주", 5, "stochrsi", "국내주식_섹터", code="463250"),
    WatchItem("KODEX 2차전지산업", 4, "stochrsi", "국내주식_섹터", code="305720"),
    WatchItem("달러선물", 4, "stochrsi", "FX파생", yf_override="KRW=X"),
    WatchItem("엔화선물", 1, "stochrsi", "FX파생", yf_override="JPYKRW=X"),
    # 안전자산 (30%) — 변동성 낮아 모니터링만
    WatchItem("TIGER 미국달러SOFR금리액티브(합성)", 15, "monitor", "금리연계", code="456610"),
    WatchItem("KODEX KOFR금리액티브(합성)", 5, "monitor", "금리연계", code="423160"),
    WatchItem("TIGER 미국달러단기채권액티브", 10, "monitor", "해외채권", code="329750"),
    # 나머지 5%는 현금 대기자금 — 시세·신호 대상이 아니므로 목록에 넣지 않음.
]

# 하위호환 별칭 (테스트·직접 참조용). 런타임 스캔은 load_watchlist() 사용.
WATCHLIST = _FALLBACK_WATCHLIST

# wiki 포트폴리오 노트 (SSOT). 파싱 성공 시 이걸로 워치리스트 구성.
WIKI_PORTFOLIO_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "obsidian-vault" / "wiki" / "투자" / "핵심_자산배분_포트폴리오.md"
)

# ─── 캐시 설정 ──────────────────────────────────────
_DATA_DIR = PATHS.shareable_root
_CACHE_DIR = PATHS.shareable_cache_dir
ETF_MAP_TTL_SEC = 24 * 3600
_ETF_MAP_CACHE: dict[str, tuple[float, dict[str, str]]] = {}

# 신호 임계값
STOCHRSI_OVERSOLD = 0.30     # 30% 이하 과매도
STOCHRSI_OVERBOUGHT = 0.80
VOLUME_SPIKE_MULT = 1.2      # 평소 대비 거래량 1.2배 이상
BAND_WALK_BARS = 2           # 연속 N봉 하단 이탈 시 Band Walk

# 개인 관심종목(assistant.db) 병합 — "종목 추가해줘"로 넣은 종목도 신호 대상이 된다.
# 자산배분 15종은 비중이 있는 포트폴리오, 관심종목은 비중 없이 관찰만 하므로 weight=0.
PERSONAL_STRATEGY = "stochrsi"      # 개별 종목 기본 지표(과매도 + 거래량 골든크로스)
PERSONAL_ASSET_CLASS = "관심종목"

# 신호 발생 기록 — 판정은 나중에 하더라도 기록은 지금부터 남긴다.
# 가격은 나중에 되살릴 수 있지만 "그때 어떤 신호가 떴고 MTF에 걸렸는지"는 못 되살린다.
SIGNAL_LOG_NAME = "signal_log.jsonl"

# 멀티 타임프레임(MTF) 필터 — 상위 TF(일봉) 추세를 거스르는 신호 억제
MTF_ENABLED = True
TREND_MA = 20                # 일봉 추세 판정 이동평균 기간
TREND_SLOPE_LOOKBACK = 5     # MA 기울기 비교 봉수


# ─── 지표 (순수 함수) ───────────────────────────────


def compute_rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    """Wilder RSI. 길이는 closes와 동일, 초기 구간은 None."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    def _rsi(g, l):
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)
    out[period] = _rsi(avg_g, avg_l)
    for i in range(period + 1, n):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = _rsi(avg_g, avg_l)
    return out


def compute_stoch_rsi(closes: list[float], period: int = 14,
                      smooth_k: int = 3, smooth_d: int = 3):
    """StochRSI %K, %D (0~1 스케일). 골든크로스 판정용."""
    rsi = compute_rsi(closes, period)
    n = len(closes)
    raw: list[Optional[float]] = [None] * n
    for i in range(n):
        window = [r for r in rsi[max(0, i - period + 1):i + 1] if r is not None]
        if len(window) < period:
            continue
        lo, hi = min(window), max(window)
        raw[i] = 0.0 if hi == lo else (rsi[i] - lo) / (hi - lo)

    def _sma(series, w):
        out: list[Optional[float]] = [None] * len(series)
        for i in range(len(series)):
            seg = series[max(0, i - w + 1):i + 1]
            if any(v is None for v in seg) or len(seg) < w:
                continue
            out[i] = sum(seg) / w
        return out

    k = _sma(raw, smooth_k)
    d = _sma(k, smooth_d)
    return k, d


def compute_macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line, signal line, histogram."""
    n = len(closes)
    def _ema(series, span):
        out: list[Optional[float]] = [None] * len(series)
        if len(series) < span:
            return out
        mult = 2.0 / (span + 1)
        ema = sum(series[:span]) / span
        out[span - 1] = ema
        for i in range(span, len(series)):
            ema = (series[i] - ema) * mult + ema
            out[i] = ema
        return out
    ema_f = _ema(closes, fast)
    ema_s = _ema(closes, slow)
    macd: list[Optional[float]] = [None] * n
    for i in range(n):
        if ema_f[i] is not None and ema_s[i] is not None:
            macd[i] = ema_f[i] - ema_s[i]
    macd_vals = [m for m in macd if m is not None]
    sig_tail = _ema(macd_vals, signal)
    sig: list[Optional[float]] = [None] * n
    offset = n - len(macd_vals)
    for j, v in enumerate(sig_tail):
        sig[offset + j] = v
    hist: list[Optional[float]] = [None] * n
    for i in range(n):
        if macd[i] is not None and sig[i] is not None:
            hist[i] = macd[i] - sig[i]
    return macd, sig, hist


def compute_bollinger(closes: list[float], period: int = 20, num_std: float = 2.0):
    """볼린저 밴드 (mid=SMA, upper/lower=mid±num_std*std)."""
    n = len(closes)
    mid: list[Optional[float]] = [None] * n
    up: list[Optional[float]] = [None] * n
    lo: list[Optional[float]] = [None] * n
    for i in range(n):
        if i + 1 < period:
            continue
        seg = closes[i - period + 1:i + 1]
        m = sum(seg) / period
        var = sum((x - m) ** 2 for x in seg) / period
        sd = var ** 0.5
        mid[i] = m
        up[i] = m + num_std * sd
        lo[i] = m - num_std * sd
    return mid, up, lo


# ─── 신호 생성 ──────────────────────────────────────


@dataclass
class Signal:
    name: str
    ticker: str
    strategy: str
    action: str      # "매수" | "매도" | "홀딩유지" | "관망" | "경고"
    emoji: str
    reason: str
    price: float
    weight: float


def _macd_signal(item, closes, ticker) -> Optional[Signal]:
    macd, sig, hist = compute_macd(closes)
    if macd[-1] is None or macd[-2] is None:
        return None
    price = closes[-1]
    cur, prev = macd[-1], macd[-2]
    if prev <= 0 < cur:
        return Signal(item.name, ticker, "MACD", "매수", "📈",
                      "MACD 0선 상향 돌파 — 상승 추세 진입", price, item.weight)
    if prev >= 0 > cur:
        return Signal(item.name, ticker, "MACD", "경고", "⚠️",
                      "MACD 0선 하향 이탈 — 추세 약화, 홀딩 재검토", price, item.weight)
    if cur > 0:
        return Signal(item.name, ticker, "MACD", "홀딩유지", "✅",
                      "MACD 0선 위 안착 — 상승 추세 유효(패닉 매도 금지)", price, item.weight)
    return None  # 0선 아래 횡보 → 신호 없음


def _bollinger_signal(item, closes, ticker) -> Optional[Signal]:
    mid, up, lo = compute_bollinger(closes)
    if lo[-1] is None or up[-1] is None or lo[-2] is None:
        return None
    price = closes[-1]
    # Band Walk: 연속 BAND_WALK_BARS 봉 종가가 하단 아래
    walk = all(
        closes[-(b + 1)] is not None and lo[-(b + 1)] is not None
        and closes[-(b + 1)] < lo[-(b + 1)]
        for b in range(BAND_WALK_BARS)
    )
    if walk:
        return Signal(item.name, ticker, "볼린저", "매도", "🚨",
                      f"Band Walk — {BAND_WALK_BARS}봉 연속 하단 이탈, 즉시 매도", price, item.weight)
    if price <= lo[-1]:
        return Signal(item.name, ticker, "볼린저", "매수", "🟢",
                      "밴드 하단 터치 — 평균 회귀 매수 구간", price, item.weight)
    if price >= up[-1]:
        return Signal(item.name, ticker, "볼린저", "매도", "🔴",
                      "밴드 상단 터치 — 평균 회귀 매도 구간", price, item.weight)
    return None


def _stochrsi_signal(item, closes, volumes, ticker) -> Optional[Signal]:
    k, d = compute_stoch_rsi(closes)
    if k[-1] is None or k[-2] is None or d[-1] is None or d[-2] is None:
        return None
    price = closes[-1]
    golden = k[-2] <= d[-2] and k[-1] > d[-1]   # %K가 %D 상향 돌파
    oversold = k[-1] <= STOCHRSI_OVERSOLD
    # 거래량 스파이크 (최근봉 vs 직전 20봉 평균)
    vol_ok = True
    if volumes and len(volumes) >= 21 and all(v is not None for v in volumes[-21:]):
        avg = sum(volumes[-21:-1]) / 20
        vol_ok = avg > 0 and volumes[-1] >= avg * VOLUME_SPIKE_MULT
    if golden and oversold and vol_ok:
        return Signal(item.name, ticker, "StochRSI", "매수", "🟢",
                      f"과매도({k[-1]*100:.0f}%) + 거래량 실린 골든크로스 — 진입 타점", price, item.weight)
    if k[-1] >= STOCHRSI_OVERBOUGHT and k[-2] >= d[-2] and k[-1] < d[-1]:
        return Signal(item.name, ticker, "StochRSI", "경고", "⚠️",
                      f"과매수({k[-1]*100:.0f}%) + 데드크로스 — 익절 타이밍 점검", price, item.weight)
    return None


def evaluate(item: WatchItem, candles: dict, ticker: str) -> Optional[Signal]:
    """candles: {'closes':[...], 'volumes':[...]}. 신호 없으면 None."""
    closes = candles.get("closes") or []
    volumes = candles.get("volumes") or []
    closes = [c for c in closes if c is not None]
    if len(closes) < 30:
        return None
    if item.strategy == "macd":
        return _macd_signal(item, closes, ticker)
    if item.strategy == "bollinger":
        return _bollinger_signal(item, closes, ticker)
    if item.strategy == "stochrsi":
        return _stochrsi_signal(item, closes, volumes, ticker)
    if item.strategy == "monitor":
        # 안전자산: 1시간봉 -1% 이상 급락 시에만 경고
        if len(closes) >= 2 and closes[-2] > 0:
            chg = (closes[-1] - closes[-2]) / closes[-2]
            if chg <= -0.01:
                return Signal(item.name, ticker, "모니터", "경고", "⚠️",
                              f"안전자산 1시간 {chg*100:.1f}% 급락 — 확인 필요", closes[-1], item.weight)
    return None


# ─── 멀티 타임프레임 필터 (v3) ──────────────────────


def compute_trend(daily_closes: list, ma: int = TREND_MA,
                  slope_lookback: int = TREND_SLOPE_LOOKBACK) -> str:
    """일봉 종가로 상위 TF 추세 판정 → "up" | "down" | "neutral".

    기준: 현재가 vs MA(ma) 위치 + MA 기울기(최근 slope_lookback 변화).
      - 종가>MA & MA 상승 → up
      - 종가<MA & MA 하락 → down
      - 그 외(혼조) → neutral
    데이터 부족 시 neutral(=필터 미적용과 동일하게 통과).
    """
    closes = [c for c in (daily_closes or []) if c is not None]
    if len(closes) < ma + slope_lookback:
        return "neutral"
    def _sma_at(idx):
        seg = closes[idx - ma + 1: idx + 1]
        return sum(seg) / ma
    ma_now = _sma_at(len(closes) - 1)
    ma_prev = _sma_at(len(closes) - 1 - slope_lookback)
    price = closes[-1]
    rising = ma_now > ma_prev
    falling = ma_now < ma_prev
    if price > ma_now and rising:
        return "up"
    if price < ma_now and falling:
        return "down"
    return "neutral"


# 신호 액션 방향: 매수성(롱 진입/유지) vs 매도성(청산/경고)
_BULLISH = {"매수", "홀딩유지"}
_BEARISH = {"매도", "경고"}


def apply_mtf_filter(sig: Optional[Signal], trend: str) -> Optional[Signal]:
    """상위 TF 추세를 거스르는 신호 억제. 지침: 상위 추세가 방향, 하위가 타이밍.

      - 매수성 신호인데 일봉 하락추세 → 억제(역추세 진입 방지)
      - 매도성 신호인데 일봉 상승추세 → 억제(불필요한 패닉 매도 방지)
      - neutral이거나 monitor 전략 신호는 그대로 통과
    통과 시 reason에 추세 표기를 덧붙인다.
    """
    if sig is None:
        return None
    if not MTF_ENABLED or trend == "neutral" or sig.strategy == "모니터":
        return sig
    if sig.action in _BULLISH and trend == "down":
        log.debug(f"MTF 억제(역추세 매수): {sig.name} 일봉 하락")
        return None
    if sig.action in _BEARISH and trend == "up":
        log.debug(f"MTF 억제(추세 내 매도): {sig.name} 일봉 상승")
        return None
    arrow = {"up": "📈일봉상승", "down": "📉일봉하락"}.get(trend, "")
    if arrow:
        sig.reason = f"{sig.reason} · 추세확인({arrow})"
    return sig


# ─── 외부 데이터 (테스트는 monkeypatch) ──────────────


def _fetch_etf_map_raw(date: str) -> dict[str, str]:
    """ETF 이름 → 코드. pykrx 외부 호출."""
    from pykrx import stock
    out: dict[str, str] = {}
    for t in stock.get_etf_ticker_list(date):
        try:
            out[stock.get_etf_ticker_name(t)] = t
        except Exception:
            pass
    return out


def get_etf_map(force_refresh: bool = False) -> dict[str, str]:
    today = datetime.now().strftime("%Y%m%d")
    cached = _ETF_MAP_CACHE.get(today)
    if cached and not force_refresh:
        ts, m = cached
        if time.time() - ts < ETF_MAP_TTL_SEC:
            return m
    disk = _load_etf_map_disk(today)
    if disk is not None and not force_refresh:
        _ETF_MAP_CACHE[today] = (time.time(), disk)
        return disk
    m = _fetch_etf_map_raw(today)
    if m:  # v3.21 빈 응답 가드
        _ETF_MAP_CACHE[today] = (time.time(), m)
        _save_etf_map_disk(today, m)
    return m


def _etf_map_path(date: str) -> Path:
    return _CACHE_DIR / f"etf_map_{date}.json"


def _load_etf_map_disk(date: str) -> Optional[dict]:
    p = _etf_map_path(date)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_etf_map_disk(date: str, m: dict) -> None:
    if not m:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _etf_map_path(date).write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.debug(f"etf_map 캐시 저장 실패: {e}")


def resolve_etf_ticker(name: str) -> Optional[str]:
    """ETF 이름 → 6자리 코드. 정확 일치 우선, 없으면 부분 일치(최단)."""
    try:
        m = get_etf_map()
    except Exception as e:
        log.warning(f"ETF 매핑 fetch 실패: {e}")
        return None
    if name in m:
        return m[name]
    key = name.replace(" ", "")
    cands = [(n, c) for n, c in m.items() if key[:10] in n.replace(" ", "")]
    if not cands:
        return None
    cands.sort(key=lambda x: len(x[0]))
    return cands[0][1]


def _fetch_intraday_raw(yf_symbol: str, interval: str = "60m", period: str = "60d"):
    """yfinance 1시간봉. 반환: {'closes':[...], 'volumes':[...]} 또는 None.

    외부 호출 — 테스트는 monkeypatch. yfinance 미설치/빈 응답 시 None.
    """
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance 미설치 — pip install yfinance 필요")
        return None
    try:
        df = yf.Ticker(yf_symbol).history(period=period, interval=interval)
        if df is None or df.empty:
            return None
        return {
            "closes": [float(x) for x in df["Close"].tolist()],
            "volumes": [float(x) for x in df["Volume"].tolist()],
        }
    except Exception as e:
        log.warning(f"intraday fetch 실패 {yf_symbol}: {e}")
        return None


def _fetch_daily_raw(yf_symbol: str, period: str = "6mo"):
    """yfinance 일봉 종가 리스트. 상위 TF 추세용. 실패 시 None."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        df = yf.Ticker(yf_symbol).history(period=period, interval="1d")
        if df is None or df.empty:
            return None
        return [float(x) for x in df["Close"].tolist()]
    except Exception as e:
        log.warning(f"daily fetch 실패 {yf_symbol}: {e}")
        return None


def _yf_symbol(item: WatchItem, ticker: Optional[str]) -> Optional[str]:
    if item.yf_override:
        return item.yf_override
    if ticker:
        return f"{ticker}.KS"
    return None


# ─── wiki 파싱 (v2 — 콴텍봇 parse_phase_weights_from_wiki 패턴) ──


def _strategy_from_text(text: str) -> str:
    t = (text or "").lower()
    if "macd" in t:
        return "macd"
    if "볼린저" in text or "bollinger" in t:
        return "bollinger"
    if "stochrsi" in t or "스토캐스틱" in text:
        return "stochrsi"
    if "모니터" in text:
        return "monitor"
    return "stochrsi"  # 기본값


def parse_watchlist_from_wiki(md_text: str) -> list:
    """포트폴리오 노트의 마크다운 표(분류|종목|코드|비중|적용지표)를 파싱.

    실패/빈 결과는 빈 리스트 반환(호출부에서 fallback). 순수 함수 — 테스트 용이.
    """
    out: list = []
    if not md_text:
        return out
    for line in md_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        asset_class, name, code, weight_s, indicator = cells[0], cells[1], cells[2], cells[3], cells[4]
        # 헤더/구분선 skip
        if name in ("종목", "") or set(name) <= set("-: "):
            continue
        # 비중 "5%" → 5.0
        try:
            weight = float(weight_s.replace("%", "").strip())
        except ValueError:
            continue
        code = code.strip()
        if not code or code == "-":
            continue
        is_etf = code.isdigit() and len(code) == 6
        out.append(WatchItem(
            name=name,
            weight=weight,
            strategy=_strategy_from_text(indicator),
            asset_class=asset_class,
            code=code if is_etf else None,
            yf_override=None if is_etf else code,
        ))
    return out


def personal_watch_items(items: Optional[list] = None, *, private_client=None) -> list:
    """개인 관심종목 → WatchItem 목록.

    items를 주면 그대로 변환(테스트용), 없으면 Private API에서 읽는다.
    소비자 경로는 assistant.db를 직접 열거나 API 실패 시 DB로 폴백하지 않는다.
    조회 실패는 경고만 남기고 빈 목록 — 관심종목 때문에 자산배분 스캔이 멈추면 안 된다.
    """
    if items is None:
        try:
            if private_client is None:
                from private_data_api_client import PrivateDataClient
                private_client = PrivateDataClient()
            items = private_client.list_watchlist()
        except Exception as e:
            log.warning(
                "Private API 관심종목 조회 실패(%s) — 자산배분만 스캔",
                type(e).__name__,
            )
            return []
    out = []
    for it in items:
        ticker = getattr(it, "ticker", None) or (it.get("ticker") if isinstance(it, dict) else None)
        if not ticker:
            continue
        name = getattr(it, "name", None) or (it.get("name") if isinstance(it, dict) else None)
        out.append(WatchItem(name=name or ticker, weight=0.0,
                             strategy=PERSONAL_STRATEGY,
                             asset_class=PERSONAL_ASSET_CLASS, code=ticker))
    return out


def merge_watchlists(base: list, personal: list) -> list:
    """자산배분 + 관심종목 병합. 이미 자산배분에 있는 코드는 중복 추가하지 않는다."""
    if not personal:
        return base          # 관심종목이 없으면 원본 그대로(불필요한 복사 없음)
    seen = {item.code for item in base if item.code}
    seen |= {item.yf_override for item in base if item.yf_override}
    merged = list(base)
    for item in personal:
        if item.code and item.code in seen:
            continue
        merged.append(item)
        if item.code:
            seen.add(item.code)
    return merged


def load_watchlist(include_personal: bool = True) -> list:
    """wiki 파싱 우선, 실패 시 코드 내장 fallback. 로깅으로 출처 표시.

    include_personal=True면 개인 관심종목을 뒤에 덧붙인다(중복 코드는 제외).
    """
    base = _FALLBACK_WATCHLIST
    try:
        if WIKI_PORTFOLIO_PATH.exists():
            md = WIKI_PORTFOLIO_PATH.read_text(encoding="utf-8")
            wl = parse_watchlist_from_wiki(md)
            if wl:
                log.debug(f"워치리스트 wiki 파싱 {len(wl)}종목")
                base = wl
            else:
                log.warning("wiki 파싱 0종목 — fallback 사용")
    except Exception as e:
        log.warning(f"wiki 파싱 실패({e}) — fallback 사용")
    if not include_personal:
        return base
    return merge_watchlists(base, personal_watch_items())


# ─── 신호 대상 범위 (2026-08-31 결정) ────────────────
#
# 신호는 **개인 관심종목에만** 보낸다. 이전에는 자산배분 포트폴리오(wiki SSOT,
# TIGER 200·KODEX 코스닥150 등 15종목)와 관심종목을 합쳐서 스캔했고, 실제로
# 자산배분 쪽 신호만 올라와 "이전 데이터가 남아 있다"는 인상을 줬다.
#
# 관심종목은 실제 보유·거래 종목과 일치하므로 알림이 바로 행동과 연결된다.
# 자산배분 15종목은 paper 운용과 별개의 장기 배분이라 1시간봉 신호의 대상이
# 아니다.
#
# **조회(`load_watchlist`)와 분리한다.** `/watchlist`로 전체를 보는 것과
# 신호를 어디에 보낼지는 다른 질문이다.
SIGNAL_SCOPE = "personal"          # "personal" | "all"


def signal_watchlist(scope: Optional[str] = None) -> list:
    """신호를 보낼 대상 목록.

    **관심종목 조회에 실패해도 자산배분으로 대체하지 않는다.** 대체하면
    사용자가 끈 것이 조용히 되살아난다 — 0건이면 신호가 없는 것이 맞다.
    """
    scope = scope or SIGNAL_SCOPE
    if scope != "personal":
        return load_watchlist()
    items = personal_watch_items()
    if not items:
        log.warning("관심종목 0건 — 신호 대상 없음 "
                    "(자산배분으로 대체하지 않습니다)")
    return items


# ─── 실행 ───────────────────────────────────────────


def build_log_record(sig: "Signal", *, at: str, trend: str = "neutral",
                     suppressed: bool = False, asset_class: str = "") -> dict:
    """신호 1건 → 기록 한 줄(순수).

    **억제된 신호도 남긴다.** MTF 필터가 값을 더하는지 빼는지는 억제분이 있어야 잴 수 있다.
    """
    return {
        "at": at,
        "ticker": sig.ticker,
        "name": sig.name,
        "asset_class": asset_class,
        "strategy": sig.strategy,
        "action": sig.action,
        "price": sig.price,
        "weight": sig.weight,
        "trend": trend,
        "suppressed": bool(suppressed),
        "reason": sig.reason,
    }


def append_log(path, record: dict) -> None:
    """JSONL 한 줄 추가. 기록 실패가 스캔을 막지 않도록 예외를 삼킨다."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning(f"신호 기록 실패({e}) — 스캔은 계속")


def default_log_path() -> Path:
    return PATHS.private_state_dir / SIGNAL_LOG_NAME


def scan(log_path=None, now: Optional[str] = None) -> list[Signal]:
    """워치리스트 전체 스캔 → 신호 리스트(actionable만).

    평가된 신호는 억제분까지 `log_path`(JSONL)에 기록한다.

    **log_path=None이면 기록하지 않는다.** 기본값을 운영 경로로 두었더니
    `scan()`을 그냥 호출한 테스트들이 운영 기록에 가짜 신호를 써 넣었다
    (2026-08-27 발견: 100건 중 86건이 테스트 픽스처 값 147.2였다).
    기록할 곳은 부르는 쪽이 명시한다 — 운영 호출부는 `run()` 하나뿐이고
    그쪽은 테스트로 지킨다. 테스트가 빠뜨리면 아무 일도 일어나지 않는 편이,
    운영 데이터가 조용히 오염되는 것보다 낫다.
    """
    stamp = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    signals: list[Signal] = []
    for item in signal_watchlist():
        ticker = item.code or (None if item.yf_override else resolve_etf_ticker(item.name))
        if not item.yf_override and not ticker:
            log.debug(f"코드 해석 실패 — skip: {item.name}")
            continue
        sym = _yf_symbol(item, ticker)
        if not sym:
            continue
        candles = _fetch_intraday_raw(sym)
        if not candles:
            continue
        raw = evaluate(item, candles, ticker or sym)
        sig, trend = raw, "neutral"
        if raw and MTF_ENABLED:
            trend = compute_trend(_fetch_daily_raw(sym) or [])
            sig = apply_mtf_filter(raw, trend)
        if raw and log_path is not None:
            append_log(log_path, build_log_record(
                raw, at=stamp, trend=trend, suppressed=(sig is None),
                asset_class=item.asset_class))
        if sig:
            signals.append(sig)
    return signals


def format_signals(signals: list[Signal]) -> str:
    if not signals:
        return ""
    # 매수/매도/경고 우선 정렬 (홀딩유지는 뒤로)
    order = {"매수": 0, "매도": 1, "경고": 2, "홀딩유지": 3, "관망": 4}
    signals = sorted(signals, key=lambda s: (order.get(s.action, 9), -s.weight))
    lines = ["📡 *기술적 신호* (1시간봉)\n"]
    for s in signals:
        tag = "관심" if not s.weight else f"{s.weight:.0f}%"
        lines.append(
            f"{s.emoji} *{s.action}* {s.name} ({tag}) [{s.strategy}]\n"
            f"  {s.reason}\n"
            f"  현재 {s.price:,.0f}"
        )
    lines.append("\n_보조 신호일 뿐 — 거시 방향성 우선. 실주문 없음._")
    return "\n".join(lines)


def format_watchlist(items: Optional[list] = None) -> str:
    """현재 신호 대상 종목 목록. '어떤 종목 보고 있어?'에 답하는 텍스트.

    **2026-08-31 정정: 기본값이 `load_watchlist()`(전체 28종목)였다.** 신호
    범위를 관심종목으로 좁힌(v3.57) 뒤에도 이 화면은 옛 전체 목록을 보여줘,
    사용자가 "저 종목들을 신호 목록에서 지워 달라"고 요청하는 일이 실제로
    생겼다 — 지울 것이 없는데 목록이 있다고 말한 것이다. 표시는 **실제로
    신호를 보내는 목록**(`signal_watchlist`)과 같아야 한다.
    """
    if items is None:
        items = signal_watchlist()
    if not items:
        return "신호 대상 종목이 없습니다."
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(it.asset_class, []).append(it)
    lines = [f"📡 *기술적 신호 대상* ({len(items)}종목)\n"]
    for cls, rows in groups.items():
        lines.append(f"*{cls}*")
        for it in rows:
            code = it.code or it.yf_override or "-"
            tag = "관심" if not it.weight else f"{it.weight:.0f}%"
            lines.append(f"  • {it.name} ({code}) · {tag} · {it.strategy}")
        lines.append("")
    lines.append("_신호는 개인 관심종목에만 갑니다(`종목 추가/삭제`로 관리)._")
    lines.append("_자산배분 종목(`핵심_자산배분_포트폴리오.md`)은 신호 대상이 아닙니다._")
    return "\n".join(lines).strip()


def run() -> tuple[str, list[Signal]]:
    """텔레그램 호출용 entrypoint. (메시지, 신호리스트) 반환.

    운영 기록 경로를 여기서 명시한다 — `scan()`의 기본값은 '기록 안 함'이다.
    """
    sigs = scan(log_path=default_log_path())
    return format_signals(sigs), sigs


# ─── 멱등(dedup) 유틸 — telegram_bot 장중 잡에서 사용 ────


def dedup_key(sig: "Signal") -> str:
    return f"{sig.ticker}|{sig.strategy}|{sig.action}"


def filter_new_signals(sigs: list, sent_keys: set) -> tuple[list, set]:
    """이미 보낸 키는 제외. (fresh 리스트, 갱신된 키 집합) 반환."""
    fresh = []
    keys = set(sent_keys)
    for s in sigs:
        k = dedup_key(s)
        if k not in keys:
            fresh.append(s)
            keys.add(k)
    return fresh, keys


def load_sent_keys(path, today: str) -> set:
    """당일자 dedup 키 로드. 날짜 다르면 빈 집합(자동 롤오버)."""
    from pathlib import Path as _P
    p = _P(path)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("date") == today:
                return set(data.get("keys", []))
    except Exception:
        pass
    return set()


def save_sent_keys(path, today: str, keys: set) -> None:
    from pathlib import Path as _P
    p = _P(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"date": today, "keys": sorted(keys)}, ensure_ascii=False),
                     encoding="utf-8")
    except Exception as e:
        log.debug(f"dedup 저장 실패: {e}")
