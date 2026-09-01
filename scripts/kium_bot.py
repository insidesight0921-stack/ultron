#!/usr/bin/env python3
"""
키움봇 (kium_bot) — 5단계 모멘텀 스캐너 (v3.16).

KOSPI200 universe(또는 KOSDAQ150/KOSPI200+KOSDAQ150)에 대해 12-1 모멘텀
(Jegadeesh & Titman 1993) 스코어를 산출 → Top N 정렬 출력.

- 12-1 정의: 12개월 전 → 1개월 전 까지의 누적 수익률.
  최근 1개월(skip)은 단기 reversal 효과 차단을 위해 제외.
  수식: score = (P[t-skip] / P[t-lookback]) - 1
        (lookback=252거래일 ≈ 12개월, skip=21거래일 ≈ 1개월)

- 생존편향(survivorship bias) 주의:
  pykrx는 현재 상장 중인 종목만 반환 → 과거 기간에 상장폐지된 종목은
  스캔 universe에서 누락. 이로 인해 모멘텀 수익률이 1~2%p 과대 추정.
  v3.16에서는 명시 경고만, v3.18+에서 해소(historical PIT universe).

- mode='fast' 디폴트: Shareable API/전용 수집기 + 순수계산만 (LLM 무호출).

API:
  fetch_universe(market="KOSPI200") -> list[(ticker, name)]
  compute_momentum_score(prices, lookback_days, skip_days) -> float | None
  scan_universe(market, top_n, lookback_days, skip_days) -> list[dict]
  format_scan_result(results, top_n) -> str
  run(action="scan", top_n=10, market="KOSPI200") -> (str, list)

라우터 entrypoint: run(). 텔레그램·웹 UI 공통.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd  # v3.17: 변동성·MA 계산용
from data_api_client import DataAPIError, ShareableDataClient, data_api_enabled
from market_data_collector import (
    collect_market_index,
    collect_ohlcv,
    fetch_universe_data,
    write_universe_cache,
)
from storage_paths import PATHS

log = logging.getLogger("kium_bot")

# ─── 캐시 ────────────────────────────────────────────

UNIVERSE_TTL_SEC = 24 * 60 * 60  # 24h — universe는 하루 1회 갱신으로 충분
PRICE_TTL_SEC = 1 * 60 * 60  # 1h — 같은 날 반복 스캔 시 캐시

# in-memory cache: key=(market, date), value=(timestamp, list[(ticker, name)])
_UNIVERSE_CACHE: dict[str, tuple[float, list[tuple[str, str]]]] = {}
_DATA_API_CLIENT = ShareableDataClient()

_DISK_CACHE_DIR: Path = PATHS.shareable_cache_dir


# ─── universe (KRX 지수 구성종목) ───────────────────


def _disk_cache_path(market: str, date: str) -> Path:
    return _DISK_CACHE_DIR / f"universe_{market}_{date}.json"


def _load_disk_cache(market: str, date: str) -> list[tuple[str, str]] | None:
    p = _disk_cache_path(market, date)
    if not p.exists():
        return None
    try:
        if time.time() - p.stat().st_mtime >= UNIVERSE_TTL_SEC:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            return None
        # JSON list of [ticker, name] pairs
        return [(str(t), str(n)) for t, n in data]
    except Exception as e:
        log.warning(f"universe 디스크 캐시 로드 실패 — fresh fetch: {e}")
        return None


def _save_disk_cache(market: str, date: str, lst: list[tuple[str, str]]) -> None:
    if not lst:
        log.info(f"universe 디스크 캐시 skip — 빈 응답 (market={market}, date={date})")
        return
    try:
        write_universe_cache(market, date, lst, cache_dir=_DISK_CACHE_DIR)
    except Exception as e:
        log.warning(f"universe 디스크 캐시 저장 실패 — 무시: {e}")


def _fetch_universe_raw(market: str, date: str) -> list[tuple[str, str]]:
    """호환 wrapper. 실제 외부 수집 구현은 market_data_collector가 소유한다."""
    return fetch_universe_data(market, date)


def fetch_universe(market: str = "KOSPI200", force_refresh: bool = False) -> list[tuple[str, str]]:
    """24h 캐시 (in-memory + 디스크). market ∈ {KOSPI200, KOSDAQ150, KOSPI200+KOSDAQ150}."""
    market = (market or "KOSPI200").strip().upper()
    today = datetime.now().strftime("%Y%m%d")
    key = f"{market}_{today}"

    stale_api_universe: list[tuple[str, str]] | None = None
    if not force_refresh:
        cached = _UNIVERSE_CACHE.get(key)
        if cached:
            ts, lst = cached
            if time.time() - ts < UNIVERSE_TTL_SEC:
                return lst

        if data_api_enabled():
            try:
                payload = _DATA_API_CLIENT.latest_universe(market)
                if payload is not None and payload["as_of"] == today:
                    api_universe = [
                        (item["ticker"], item["name"])
                        for item in payload["instruments"]
                    ]
                    _UNIVERSE_CACHE[key] = (time.time(), api_universe)
                    return api_universe
                if payload is not None:
                    stale_api_universe = [
                        (item["ticker"], item["name"])
                        for item in payload["instruments"]
                    ]
            except (DataAPIError, ValueError) as exc:
                log.debug(
                    "Shareable Data API universe 실패 — 기존 캐시 사용 (%s): %s",
                    market,
                    exc,
                )

        disk = _load_disk_cache(market, today)
        if disk is not None:
            _UNIVERSE_CACHE[key] = (time.time(), disk)
            return disk

    try:
        lst = _fetch_universe_raw(market, today)
    except Exception as exc:
        if stale_api_universe:
            log.warning(
                "universe 최신 갱신 실패 — 마지막 Shareable 캐시 사용 (%s, %s): %s",
                market,
                payload["as_of"],
                exc,
            )
            return stale_api_universe
        raise
    # v3.21 — 빈 list면 in-memory + 디스크 둘 다 캐시 skip. 다음 호출에서 다시 fetch.
    # 휴장일/네트워크 오류 같은 일시적 빈 응답을 24h 영속화하는 함정 차단.
    if lst:
        _UNIVERSE_CACHE[key] = (time.time(), lst)
        _save_disk_cache(market, today, lst)
    else:
        log.warning(f"universe fetch 빈 응답 — 캐시 skip (market={market}, date={today}). 휴장일 또는 네트워크 오류 의심.")
    return lst


# ─── OHLCV ──────────────────────────────────────────


def _fetch_ohlcv_raw(ticker: str, start: str, end: str):
    """호환 wrapper. 실제 수집·캐시 쓰기는 market_data_collector가 소유한다."""
    payload = collect_ohlcv(ticker, start, end)
    return pd.DataFrame({"종가": payload["series"]["close"]})


def _load_ohlcv(ticker: str, start: str, end: str):
    """API 우선 OHLCV 읽기. 최신 기준일이 다르면 전용 수집기를 사용한다."""
    if data_api_enabled():
        try:
            payload = _DATA_API_CLIENT.latest_ohlcv(ticker)
            if payload is not None and payload["as_of"] == end:
                return pd.DataFrame({"종가": payload["series"]["close"]})
        except (DataAPIError, ValueError) as exc:
            log.debug(
                "Shareable Data API OHLCV 실패 — collector 사용 (%s): %s", ticker, exc
            )
    return _fetch_ohlcv_raw(ticker, start, end)


# ─── KOSPI 지수 + VKOSPI fetch (v3.17) ──────────────


def _recent_market_index_payload(payload: dict | None, *, max_age_days: int = 4):
    if not payload:
        return None
    try:
        dates = payload["series"]["date"]
        closes = payload["series"]["close"]
        latest = datetime.strptime(dates[-1], "%Y%m%d")
        age = (datetime.now().date() - latest.date()).days
        if age < 0 or age > max_age_days or len(dates) != len(closes):
            return None
        return pd.Series(closes)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _collect_market_index(index_name: str, *, days: int) -> dict:
    return collect_market_index(index_name, days=days)


def fetch_kospi_close(days: int = 280):
    """KOSPI 일봉 종가 시리즈 (pd.Series). 영업일 days개 안전 확보."""
    if data_api_enabled():
        try:
            cached = _recent_market_index_payload(
                _DATA_API_CLIENT.latest_market_index("KOSPI")
            )
            if cached is not None and len(cached) >= min(int(days), 200):
                return cached
        except (DataAPIError, ValueError) as exc:
            log.debug("KOSPI Data API 실패 — collector 갱신: %s", exc)
    try:
        payload = _collect_market_index("KOSPI", days=int(days))
        return pd.Series(payload["series"]["close"])
    except Exception as e:
        log.warning(f"KOSPI fetch 실패: {e}")
        return None


def fetch_vkospi_latest():
    """VKOSPI 최신 종가 (단일 float). 실패 시 None."""
    if data_api_enabled():
        try:
            cached = _recent_market_index_payload(
                _DATA_API_CLIENT.latest_market_index("VKOSPI")
            )
            if cached is not None:
                return float(cached.iloc[-1])
        except (DataAPIError, ValueError) as exc:
            log.debug("VKOSPI Data API 실패 — collector 갱신: %s", exc)
    try:
        payload = _collect_market_index("VKOSPI", days=20)
        return float(payload["series"]["close"][-1])
    except Exception as e:
        log.warning(f"VKOSPI fetch 실패: {e}")
        return None


# ─── 모멘텀 스코어 ──────────────────────────────────


def compute_momentum_score(
    prices,
    lookback_days: int = 252,
    skip_days: int = 21,
) -> float | None:
    """12-1 J&T 모멘텀 스코어.

    Args:
      prices: 종가 시퀀스 (pd.Series 또는 list/tuple). 시간순 오름차순.
      lookback_days: 과거 조회 거리 (252 ≈ 1년 영업일).
      skip_days: 최근 제외 일수 (21 ≈ 1개월).

    Returns:
      score = (P[t-skip] / P[t-lookback]) - 1
      예: 0.18 → +18%. None 반환은 데이터 부족 또는 비정상.
    """
    try:
        n = len(prices)
    except TypeError:
        return None
    if n < lookback_days + 1:
        return None
    if skip_days < 0 or lookback_days <= skip_days:
        return None

    # iloc은 pd.Series, [-x]은 list 둘 다 지원
    def at(i: int) -> float:
        if hasattr(prices, "iloc"):
            return float(prices.iloc[i])
        return float(prices[i])

    p_recent = at(-skip_days - 1)
    p_old = at(-lookback_days - 1)
    if p_old <= 0:
        return None
    return (p_recent / p_old) - 1.0


# ─── universe 스캔 ──────────────────────────────────


def scan_universe(
    market: str = "KOSPI200",
    top_n: int = 10,
    lookback_days: int = 252,
    skip_days: int = 21,
    universe: list[tuple[str, str]] | None = None,
) -> list[dict]:
    """universe 전체 모멘텀 스캔 → Top N (점수 내림차순).

    Args:
      market: 'KOSPI200' / 'KOSDAQ150' / 'KOSPI200+KOSDAQ150'
      top_n: 상위 N개
      lookback_days, skip_days: 12-1 파라미터
      universe: 직접 universe 주입 (테스트용)

    각 dict 필드:
      ticker, name, score, current_price, return_1m, return_12m
    """
    if universe is None:
        universe = fetch_universe(market=market)

    today = datetime.now()
    # 영업일 252개를 안전하게 확보하려면 캘린더일 ~400일 fetch
    start_date = (today - timedelta(days=int(lookback_days * 1.6) + 30)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    results: list[dict] = []
    for ticker, name in universe:
        try:
            df = _load_ohlcv(ticker, start_date, end_date)
        except Exception as e:
            log.debug(f"{ticker} ohlcv 호출 실패: {e}")
            continue
        if df is None or len(df) < lookback_days + 1:
            continue

        # collector/호환 테스트의 한국어·영어 종가 컬럼을 모두 허용.
        close = None
        for col in ("종가", "Close", "close"):
            if hasattr(df, "columns") and col in df.columns:
                close = df[col]
                break
        if close is None:
            continue

        score = compute_momentum_score(close, lookback_days, skip_days)
        if score is None:
            continue

        try:
            current = float(close.iloc[-1])
            ret_1m = (
                float(close.iloc[-1]) / float(close.iloc[-skip_days - 1]) - 1.0
                if len(close) > skip_days else None
            )
            ret_12m = (
                float(close.iloc[-1]) / float(close.iloc[-lookback_days - 1]) - 1.0
                if len(close) > lookback_days else None
            )
        except Exception:
            continue

        results.append({
            "ticker": ticker,
            "name": name,
            "score": score,
            "current_price": current,
            "return_1m": ret_1m,
            "return_12m": ret_12m,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[: int(top_n)]


# ─── 출력 포맷 ──────────────────────────────────────


def format_scan_result(
    results: list[dict],
    top_n: int | None = None,
    crash_signals: dict | None = None,
    weight: dict | None = None,
) -> str:
    """Top N 모멘텀 스캔 결과 → 사용자 친화 텍스트 (텔레그램·웹 UI 공통).

    v3.17: crash_signals/weight dict 옵션 — 헤더에 경고·비중 권고 섹션 추가.
    """
    if not results:
        return (
            "📊 키움봇 모멘텀 스캔\n"
            "(결과 없음 — universe가 비었거나 가격 데이터 부족)"
        )
    lines: list[str] = [
        f"📊 키움봇 모멘텀 스캔 (12-1 J&T) — Top {len(results)}",
        f"⚠️ 생존편향 주의 — 현재 상장 종목 기준 (1~2%p 과대 추정 가능)",
    ]
    if crash_signals:
        lines.append("")
        lines.append(crash_signals.get("recommendation", ""))
        vol = crash_signals.get("vol_spike", {})
        panic = crash_signals.get("market_panic", {})
        rev = crash_signals.get("momentum_reversal", {})
        def _flag(s):
            return "🚨" if s.get("hit") else "✓"
        ratio = vol.get("ratio")
        ret_w = panic.get("return_window")
        avg = rev.get("avg_return_1m")
        lines.append(
            f"  {_flag(vol)} 변동성 급등 (21d/63d ratio = "
            f"{ratio:.2f})" if ratio is not None
            else f"  {_flag(vol)} 변동성 급등 (데이터 부족)"
        )
        lines.append(
            f"  {_flag(panic)} 시장 패닉 (KOSPI 20일 = "
            f"{(ret_w * 100):+.1f}%)" if ret_w is not None
            else f"  {_flag(panic)} 시장 패닉 (데이터 부족)"
        )
        lines.append(
            f"  {_flag(rev)} 모멘텀 내부 역전 (Top 평균 1M = "
            f"{(avg * 100):+.1f}%)" if avg is not None
            else f"  {_flag(rev)} 모멘텀 내부 역전 (데이터 부족)"
        )
    if weight:
        eq = (weight.get("equity_weight") or 0) * 100
        bd = (weight.get("bond_weight") or 0) * 100
        reason = weight.get("reason", "")
        lines.append("")
        lines.append(f"📐 권장 비중: 주식 {eq:.0f}% / 채권 {bd:.0f}%")
        if reason:
            lines.append(f"   {reason}")
    lines.append("")
    for i, r in enumerate(results, 1):
        score_pct = (r.get("score") or 0) * 100
        ret_1m_v = r.get("return_1m")
        ret_1m_pct = (ret_1m_v * 100) if ret_1m_v is not None else None
        cur = r.get("current_price") or 0
        ret_1m_str = f"{ret_1m_pct:+5.1f}%" if ret_1m_pct is not None else "  N/A"
        lines.append(
            f"{i:>2}. {r['name']} ({r['ticker']})  "
            f"점수 {score_pct:+6.1f}%  최근1M {ret_1m_str}  현재 {cur:,.0f}원"
        )
    return "\n".join(lines)


# ─── 3중 모멘텀 크래시 감지 (v3.17) ───────────────
# AQR Daniel & Moskowitz (2016) "Momentum Crashes" 단순화 구현.
# 신호 1: 변동성 급등 (21일 vol > 63일 vol × 1.5)
# 신호 2: 시장 패닉 (KOSPI 20일 누적 수익률 ≤ -10%)
# 신호 3: 모멘텀 내부 역전 (Top 10 종목 최근 1개월 평균 수익률 < 0)
# 2개 이상 hit → 보수적 운용 권고.


def compute_volatility_spike(
    close,
    short_window: int = 21,
    long_window: int = 63,
    threshold: float = 1.5,
) -> dict:
    """변동성 급등 신호. close = 종가 시리즈 (pd.Series 또는 list)."""
    if close is None:
        return {"short_vol": None, "long_vol": None, "ratio": None,
                "hit": False, "threshold": threshold}
    if hasattr(close, "pct_change"):
        returns = close.pct_change().dropna()
    else:
        try:
            ser = pd.Series([float(x) for x in close])
        except (TypeError, ValueError):
            return {"short_vol": None, "long_vol": None, "ratio": None,
                    "hit": False, "threshold": threshold}
        returns = ser.pct_change().dropna()
    if len(returns) < long_window:
        return {"short_vol": None, "long_vol": None, "ratio": None,
                "hit": False, "threshold": threshold}
    short_std = float(returns.iloc[-short_window:].std())
    long_std = float(returns.iloc[-long_window:].std())
    if long_std <= 0:
        return {"short_vol": short_std, "long_vol": long_std, "ratio": None,
                "hit": False, "threshold": threshold}
    short_vol = short_std * (252 ** 0.5)  # 연환산
    long_vol = long_std * (252 ** 0.5)
    ratio = short_std / long_std  # ratio는 std 단위 비교 (연환산 무관)
    return {
        "short_vol": short_vol, "long_vol": long_vol, "ratio": ratio,
        "hit": ratio > threshold, "threshold": threshold,
    }


def compute_market_panic(
    kospi_close,
    window: int = 20,
    threshold: float = -0.10,
) -> dict:
    """시장 패닉 신호 — KOSPI window일 누적 수익률 ≤ threshold."""
    if kospi_close is None:
        return {"return_window": None, "hit": False,
                "threshold": threshold, "window": window}
    if hasattr(kospi_close, "iloc"):
        ser = kospi_close
    else:
        try:
            ser = pd.Series([float(x) for x in kospi_close])
        except (TypeError, ValueError):
            return {"return_window": None, "hit": False,
                    "threshold": threshold, "window": window}
    if len(ser) < window + 1:
        return {"return_window": None, "hit": False,
                "threshold": threshold, "window": window}
    p_now = float(ser.iloc[-1])
    p_old = float(ser.iloc[-window - 1])
    if p_old <= 0:
        return {"return_window": None, "hit": False,
                "threshold": threshold, "window": window}
    ret = (p_now / p_old) - 1.0
    return {
        "return_window": ret, "hit": ret <= threshold,
        "threshold": threshold, "window": window,
    }


def compute_momentum_reversal(
    top_results: list[dict] | None,
    top_n: int = 10,
) -> dict:
    """모멘텀 내부 역전 — Top N 종목 최근 1개월 평균 수익률 < 0."""
    if not top_results:
        return {"avg_return_1m": None, "hit": False, "n_evaluated": 0}
    subset = top_results[: int(top_n)]
    rets = [r.get("return_1m") for r in subset
            if r.get("return_1m") is not None]
    if not rets:
        return {"avg_return_1m": None, "hit": False, "n_evaluated": 0}
    avg = sum(rets) / len(rets)
    return {"avg_return_1m": avg, "hit": avg < 0,
            "n_evaluated": len(rets)}


def detect_crash_signals(
    kospi_close=None,
    top_results: list[dict] | None = None,
    threshold_count: int = 2,
) -> dict:
    """3중 크래시 통합 감지. dict 반환 — 호출 측이 출력 결정."""
    vol = compute_volatility_spike(kospi_close)
    panic = compute_market_panic(kospi_close)
    rev = compute_momentum_reversal(top_results)
    hits = sum(1 for s in (vol, panic, rev) if s.get("hit"))
    if hits >= threshold_count:
        rec = (
            f"⚠️ 보수적 운용 권고 — 3중 신호 중 {hits}개 hit. "
            "비중 30%로 강제 축소 검토 (AQR D&M 2016 룰)."
        )
    elif hits == 1:
        rec = "📊 1개 신호 hit — 모니터링 강화."
    else:
        rec = "✅ 크래시 신호 없음 — 정상 운용."
    return {
        "vol_spike": vol, "market_panic": panic,
        "momentum_reversal": rev, "hits": hits,
        "threshold_count": threshold_count, "recommendation": rec,
    }


# ─── VKOSPI 비중 룰 (v3.17 → v3.64 임계값 실측 교체) ──

# **이 값은 유래가 있다.** v3.17의 >30 / <15는 "VKOSPI는 보통 15~30을
# 오간다"는 교과서 상식에서 가져온 상수였고, 이 시장에서 재보니 틀렸다.
#
#   2026-09-01 실측 · KRX OPEN API · 407거래일(20241226~20260831)
#     최저 17.67 · p20 20.69 · 중앙 28.40 · p80 60.59 · 최고 96.94
#     기존 임계값: 상단(>30) 198일 48.6% · **하단(<15) 0일 — 죽은 가지**
#
# 이 계열이 진짜 변동성지수인지는 음성 대조로 확인했다: KOSPI 20일 실현
# 변동성(연율)과 상관 +0.837, 배수 중앙 1.28 — 내재>실현이라는 정상 부호다.
#
# 임계값은 분포의 상·하위 20%로 잡는다. 레벨을 백분위로 정하면 "평소보다
# 불안하면 채권, 평소보다 잔잔하면 주식"이라는 **뜻**이 유지된다.
VKOSPI_HIGH = 60.6   # p80. 초과 81일(19.9%)
VKOSPI_LOW = 20.7    # p20. 미만 82일(20.1%)

# **아직 증명된 규칙이 아니다.** 362일 모의(주식/채권 연3%)에서 고정 70%
# 대비 누적 +106.8%→+106.0%(동급), MDD 28.29%→24.40%, 샤프 1.65→1.82로
# 개선됐고 상단 임계 45~75 구간에서 결과가 평평했다(과최적화 아님). 그러나
# ① MDD 개선은 2026년 급락 **한 번**에 전부 기대고 있고, ② VKOSPI 밴드별
# 향후 20일 KOSPI 수익률 차이는 순환이동 순열검정 p=0.061로 우연과 구분되지
# 않는다(독립창 17개뿐 — 유의성 확보엔 5~9년 필요).
# 그래서 이건 "수익을 올리는 규칙"이 아니라 **방어 규칙**으로만 붙인다.
VKOSPI_MEASURED_AT = "2026-09-01"
VKOSPI_MEASURED_N = 407

# **위 두 상수는 초기값(폴백)일 뿐이다.** 실제로 도는 값은 승인 원장의 마지막
# 항목이다 — 분기 재측정에서 사람이 승인한 값. 자동 갱신을 쓰지 않는 이유와
# 원장의 형태는 `vkospi_threshold_review` 상단에 적혀 있다.
VKOSPI_LEDGER_FILE = PATHS.private_state_dir / "vkospi_thresholds.json"


def active_thresholds() -> tuple[float, float, str]:
    """지금 유효한 (상단, 하단, 출처). 원장을 못 읽으면 초기값으로 돈다.

    **원장이 없는 것과 깨진 것을 구분한다.** 없으면 조용히 초기값이지만,
    깨졌으면 경고를 남긴다 — 승인 이력이 사라진 채로 도는 것은 사고다.
    """
    try:
        import vkospi_threshold_review as _vtr

        if not VKOSPI_LEDGER_FILE.exists():
            return VKOSPI_HIGH, VKOSPI_LOW, "코드 초기값"
        ledger = _vtr.load_ledger(VKOSPI_LEDGER_FILE)
        return _vtr.active_thresholds(
            ledger, fallback_high=VKOSPI_HIGH, fallback_low=VKOSPI_LOW)
    except Exception as exc:  # noqa: BLE001
        log.warning("VKOSPI 임계값 원장을 읽지 못했다 — 초기값으로 돈다: %s", exc)
        return VKOSPI_HIGH, VKOSPI_LOW, "코드 초기값(원장 읽기 실패)"


def compute_weight_recommendation(
    vkospi: float | None = None,
    kospi_close=None,
    ma_window: int = 200,
    thresholds: tuple[float, float] | None = None,
) -> dict:
    """주식/채권 비중 룰.

    1차 (KOSPI 200일선): 위 → 주식 70%, 아래 → 50% (균형)
    2차 (VKOSPI):       > VKOSPI_HIGH → 채권 +10%p,
                        < VKOSPI_LOW  → 주식 +10%p
    클램프: 주식 30~90%.

    임계값의 유래와 한계는 모듈 상단 주석을 볼 것. 요약하면 **분포에서 잰
    값이고, 방어 규칙으로만 쓴다.**
    """
    base_equity = 0.70  # 디폴트
    parts: list[str] = []
    kospi_above_ma = None

    if kospi_close is not None:
        if hasattr(kospi_close, "iloc"):
            ser = kospi_close
        else:
            try:
                ser = pd.Series([float(x) for x in kospi_close])
            except (TypeError, ValueError):
                ser = None
        if ser is not None and len(ser) >= ma_window:
            current = float(ser.iloc[-1])
            ma = float(ser.iloc[-ma_window:].mean())
            kospi_above_ma = current > ma
            if kospi_above_ma:
                base_equity = 0.70
                parts.append(f"KOSPI > {ma_window}일선 → 주식 우위(70%)")
            else:
                base_equity = 0.50
                parts.append(f"KOSPI < {ma_window}일선 → 균형(50%)")

    vkospi_band = None
    if thresholds is not None:
        hi_th, lo_th = float(thresholds[0]), float(thresholds[1])
        th_source = "명시"
    else:
        hi_th, lo_th, th_source = active_thresholds()
    if vkospi is not None:
        try:
            v = float(vkospi)
            if v > hi_th:
                vkospi_band = "high"
                base_equity -= 0.10
                parts.append(
                    f"VKOSPI {v:.1f} > {hi_th}(상위 20%) → 채권 +10%p")
            elif v < lo_th:
                vkospi_band = "low"
                base_equity += 0.10
                parts.append(
                    f"VKOSPI {v:.1f} < {lo_th}(하위 20%) → 주식 +10%p")
            else:
                vkospi_band = "mid"
                parts.append(f"VKOSPI {v:.1f} 중립")
        except (TypeError, ValueError):
            pass

    base_equity = max(0.30, min(0.90, base_equity))
    return {
        "equity_weight": round(base_equity, 2),
        "bond_weight": round(1.0 - base_equity, 2),
        "kospi_above_ma": kospi_above_ma,
        "vkospi_band": vkospi_band,
        "vkospi_value": float(vkospi) if vkospi is not None else None,
        "vkospi_high": hi_th,
        "vkospi_low": lo_th,
        "vkospi_threshold_source": th_source,
        "reason": " · ".join(parts) if parts else "데이터 부족",
    }


# ─── 라우터 entrypoint ──────────────────────────────


VALID_ACTIONS = {"scan"}
VALID_MARKETS = {"KOSPI200", "KOSDAQ150", "KOSPI200+KOSDAQ150"}


def run(
    action: str = "scan",
    top_n: int = 10,
    market: str = "KOSPI200",
    **kwargs,
) -> tuple[str, list[dict]]:
    """라우터에서 호출하는 단일 entrypoint.

    반환: (사용자 표시용 텍스트, sources(현재 빈 리스트 — wiki RAG 미사용))
    """
    action = (action or "scan").strip().lower()
    if action not in VALID_ACTIONS:
        return (f"❌ 알 수 없는 action: {action!r} (허용: {sorted(VALID_ACTIONS)})", [])

    try:
        top_n_val = max(1, min(50, int(top_n)))
    except (TypeError, ValueError):
        top_n_val = 10

    market_val = (market or "KOSPI200").strip().upper()
    if market_val not in VALID_MARKETS:
        return (
            f"❌ 지원하지 않는 universe: {market!r} "
            f"(허용: {sorted(VALID_MARKETS)})",
            [],
        )

    try:
        results = scan_universe(market=market_val, top_n=top_n_val)
    except Exception as e:
        log.exception("kium_bot scan 실패")
        return (f"❌ 키움봇 스캔 실패: {e}", [])

    # v3.17: with_crash_signals=True면 KOSPI fetch + 3중 감지 + VKOSPI 비중 권고
    crash_signals = None
    weight = None
    if kwargs.get("with_crash_signals"):
        try:
            kospi_close = fetch_kospi_close()
            crash_signals = detect_crash_signals(
                kospi_close=kospi_close, top_results=results
            )
            vkospi_v = fetch_vkospi_latest()
            weight = compute_weight_recommendation(
                vkospi=vkospi_v, kospi_close=kospi_close
            )
        except Exception as e:
            log.warning(f"crash_signals 계산 실패 — 결과만 출력: {e}")

    return (
        format_scan_result(results, top_n=top_n_val,
                           crash_signals=crash_signals, weight=weight),
        [],
    )


# ─── CLI ────────────────────────────────────────────


def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--market", default="KOSPI200")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    msg, _ = run("scan", top_n=args.top, market=args.market)
    print(msg)


if __name__ == "__main__":
    _cli()
