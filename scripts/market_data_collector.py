#!/usr/bin/env python3
"""Collect public market data into the Shareable cache.

This module owns external KRX/FDR collection and Shareable cache writes.
Consumers read through the Data API/cache and keep only domain calculations.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from shareable_store import normalize_exchange_market, normalize_market, normalize_ticker
from storage_paths import PATHS


INDEX_NAMES = {"KOSPI", "VKOSPI"}
EXCHANGE_MARKETS = ("KOSPI", "KOSDAQ")
FUNDAMENTAL_FIELDS = ("BPS", "PER", "PBR", "EPS", "DPS", "DIV")
log = logging.getLogger("market_data_collector")


class MarketCollectionError(RuntimeError):
    pass


@contextlib.contextmanager
def _quiet_external_library_output():
    """Prevent third-party clients from printing account identifiers to logs."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        yield


def normalize_index_name(index_name: str) -> str:
    value = str(index_name or "").strip().upper()
    if value not in INDEX_NAMES:
        raise ValueError("지원하지 않는 market index입니다.")
    return value


def normalize_as_of(as_of: str) -> str:
    value = str(as_of or "").strip()
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("as_of는 YYYYMMDD 형식이어야 합니다.") from exc
    return parsed.strftime("%Y%m%d")


def _fetch_index_frame(start: str, end: str, index_name: str):
    if index_name == "KOSPI":
        import FinanceDataReader as fdr

        return fdr.DataReader("KS11", start, end)
    if index_name == "VKOSPI":
        return _fetch_vkospi_frame(start, end)
    raise MarketCollectionError(f"알 수 없는 지수: {index_name}")


def _fetch_vkospi_frame(start: str, end: str):
    """KRX OPEN API로 VKOSPI 일별 종가 수집(2026-09-01 개통).

    **하루에 한 번씩 부른다.** 이 API는 `basDd` 하나만 받으므로 기간 조회가
    안 된다. 320개 지수가 한 응답에 오고 그중 한 행만 쓰므로 낭비지만,
    일 한도 10,000회에 견주면 문제되지 않는다(1년치 = 약 250회).

    **거래일을 모르므로 달력 대신 실제 응답으로 판정한다.** 휴장일은 행이
    없거나 지수가 없고, 그런 날은 그냥 건너뛴다 — 직전 값으로 메우면
    변동성이 실제보다 낮게 기록된다.
    """
    import krx_openapi

    if not krx_openapi._auth_key():
        raise MarketCollectionError(
            f"{krx_openapi.KEY_NAME} 미설정 — VKOSPI를 수집할 수 없습니다.")

    start_d = datetime.strptime(normalize_as_of(start), "%Y%m%d")
    end_d = datetime.strptime(normalize_as_of(end), "%Y%m%d")
    rows: list[tuple[str, float]] = []
    misses = 0
    day = end_d
    while day >= start_d:
        if day.weekday() < 5:                     # 주말은 부르지 않는다
            got = krx_openapi.fetch_vkospi(day.strftime("%Y%m%d"))
            if got.get("value"):
                rows.append((got["date"] or day.strftime("%Y%m%d"), got["value"]))
            else:
                misses += 1
        day -= timedelta(days=1)

    if not rows:
        raise MarketCollectionError(
            f"VKOSPI 응답에 값이 없습니다({start}~{end}, 미확보 {misses}일).")
    rows.sort(key=lambda r: r[0])
    log.info("VKOSPI 수집 %d일 (%s~%s) · 값 없는 날 %d",
             len(rows), rows[0][0], rows[-1][0], misses)

    import pandas as pd

    return pd.DataFrame({"종가": [v for _, v in rows]},
                        index=pd.to_datetime([d for d, _ in rows]))


def _fetch_ohlcv_frame(ticker: str, start: str, end: str):
    import FinanceDataReader as fdr

    return fdr.DataReader(ticker, start, end)


def fetch_ticker_map_data(
    as_of: str,
    *,
    fetcher: Callable[[str], dict[str, str]] | None = None,
) -> dict[str, str]:
    """Return a validated KOSPI/KOSDAQ ticker-name map from the public source."""
    as_of_n = normalize_as_of(as_of)
    if fetcher is not None:
        raw = fetcher(as_of_n)
    else:
        try:
            with _quiet_external_library_output():
                from pykrx import stock

                raw = {}
                for market in EXCHANGE_MARKETS:
                    for ticker in stock.get_market_ticker_list(as_of_n, market=market):
                        try:
                            raw[str(ticker)] = str(stock.get_market_ticker_name(ticker))
                        except Exception:
                            continue
        except Exception as exc:
            raise MarketCollectionError("ticker map 외부 수집에 실패했습니다.") from exc

    clean: dict[str, str] = {}
    for ticker, name in (raw or {}).items():
        try:
            ticker_n = normalize_ticker(ticker)
        except ValueError:
            continue
        name_n = str(name or "").strip()
        if name_n:
            clean[ticker_n] = name_n
    if not clean:
        raise MarketCollectionError("유효한 ticker map 응답이 없습니다.")
    return clean


def write_ticker_map_cache(
    as_of: str,
    mapping: dict[str, str],
    *,
    cache_dir: Path | str = PATHS.shareable_cache_dir,
) -> Path | None:
    """Atomically persist a validated public ticker map; skip empty input."""
    as_of_n = normalize_as_of(as_of)
    clean = fetch_ticker_map_data(as_of_n, fetcher=lambda _date: mapping)
    if not clean:
        return None
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"ticker_map_{as_of_n}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def collect_ticker_map(
    *,
    as_of: str | None = None,
    cache_dir: Path | str = PATHS.shareable_cache_dir,
    fetcher: Callable[[str], dict[str, str]] | None = None,
) -> dict[str, str]:
    as_of_n = normalize_as_of(as_of or datetime.now().strftime("%Y%m%d"))
    mapping = fetch_ticker_map_data(as_of_n, fetcher=fetcher)
    write_ticker_map_cache(as_of_n, mapping, cache_dir=cache_dir)
    return mapping


def fetch_fundamental_data(
    as_of: str,
    *,
    market: str = "KOSPI",
    fetcher: Callable[[str, str], Any] | None = None,
) -> dict[str, dict[str, float]]:
    """Fetch and sanitize public per-ticker valuation/fundamental fields."""
    as_of_n = normalize_as_of(as_of)
    market_n = normalize_exchange_market(market)
    if fetcher is not None:
        frame = fetcher(as_of_n, market_n)
    else:
        try:
            with _quiet_external_library_output():
                from pykrx import stock

                frame = stock.get_market_fundamental_by_ticker(
                    as_of_n, market=market_n
                )
        except Exception as exc:
            raise MarketCollectionError("fundamental 외부 수집에 실패했습니다.") from exc
    if frame is None or not hasattr(frame, "index") or len(frame) == 0:
        return {}

    output: dict[str, dict[str, float]] = {}
    for ticker in frame.index:
        try:
            ticker_n = normalize_ticker(ticker)
            row = frame.loc[ticker]
        except (ValueError, KeyError):
            continue
        values: dict[str, float] = {}
        for field in FUNDAMENTAL_FIELDS:
            try:
                value = float(row.get(field, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            values[field] = value if math.isfinite(value) else 0.0
        output[ticker_n] = values
    return output


def fetch_market_cap_data(
    as_of: str,
    *,
    market: str = "KOSPI",
    fetcher: Callable[[str, str], Any] | None = None,
) -> dict[str, float]:
    """Fetch and sanitize positive public market capitalizations by ticker."""
    as_of_n = normalize_as_of(as_of)
    market_n = normalize_exchange_market(market)
    if fetcher is not None:
        frame = fetcher(as_of_n, market_n)
    else:
        try:
            with _quiet_external_library_output():
                from pykrx import stock

                frame = stock.get_market_cap_by_ticker(as_of_n, market=market_n)
        except Exception as exc:
            raise MarketCollectionError("시가총액 외부 수집에 실패했습니다.") from exc
    if frame is None or not hasattr(frame, "index") or len(frame) == 0:
        return {}

    output: dict[str, float] = {}
    for ticker in frame.index:
        try:
            ticker_n = normalize_ticker(ticker)
            value = float(frame.loc[ticker].get("시가총액", 0) or 0)
        except (ValueError, TypeError, KeyError):
            continue
        if math.isfinite(value) and value > 0:
            output[ticker_n] = value
    return output


def _sanitize_fundamental_mapping(
    mapping: dict[str, dict[str, Any]],
) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = {}
    for ticker, raw_values in (mapping or {}).items():
        try:
            ticker_n = normalize_ticker(ticker)
        except ValueError:
            continue
        if not isinstance(raw_values, dict):
            continue
        values: dict[str, float] = {}
        for field in FUNDAMENTAL_FIELDS:
            try:
                value = float(raw_values.get(field, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            values[field] = value if math.isfinite(value) else 0.0
        output[ticker_n] = values
    return output


def _sanitize_market_cap_mapping(mapping: dict[str, Any]) -> dict[str, float]:
    output: dict[str, float] = {}
    for ticker, raw_value in (mapping or {}).items():
        try:
            ticker_n = normalize_ticker(ticker)
            value = float(raw_value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(value) and value > 0:
            output[ticker_n] = value
    return output


def write_factor_snapshot_cache(
    market: str,
    as_of: str,
    fundamentals: dict[str, dict[str, Any]],
    market_caps: dict[str, Any],
    *,
    root: Path | str = PATHS.shareable_root,
) -> Path:
    market_n = normalize_exchange_market(market)
    as_of_n = normalize_as_of(as_of)
    fundamentals_n = _sanitize_fundamental_mapping(fundamentals)
    market_caps_n = _sanitize_market_cap_mapping(market_caps)
    if not fundamentals_n and not market_caps_n:
        raise MarketCollectionError("유효한 factor snapshot이 없습니다.")
    payload = {
        "market": market_n,
        "as_of": as_of_n,
        "fundamentals": fundamentals_n,
        "market_caps": market_caps_n,
    }
    destination = Path(root) / "cache" / "factors"
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"factor_snapshot_{market_n}_{as_of_n}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def collect_factor_snapshot(
    market: str,
    *,
    as_of: str | None = None,
    root: Path | str = PATHS.shareable_root,
    max_lookback_days: int = 7,
    fundamental_fetcher: Callable[[str, str], Any] | None = None,
    cap_fetcher: Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    """Collect the most recent trading-day factor snapshot up to ``as_of``."""
    market_n = normalize_exchange_market(market)
    end_s = normalize_as_of(as_of or datetime.now().strftime("%Y%m%d"))
    end = datetime.strptime(end_s, "%Y%m%d")
    lookback = int(max_lookback_days)
    if not 0 <= lookback <= 31:
        raise ValueError("max_lookback_days는 0~31 범위여야 합니다.")

    for offset in range(lookback + 1):
        candidate = (end - timedelta(days=offset)).strftime("%Y%m%d")
        market_caps = fetch_market_cap_data(
            candidate, market=market_n, fetcher=cap_fetcher
        )
        if not market_caps:
            continue
        fundamentals = fetch_fundamental_data(
            candidate, market=market_n, fetcher=fundamental_fetcher
        )
        if not fundamentals:
            continue
        write_factor_snapshot_cache(
            market_n,
            candidate,
            fundamentals,
            market_caps,
            root=root,
        )
        return {
            "market": market_n,
            "as_of": candidate,
            "fundamentals": fundamentals,
            "market_caps": market_caps,
        }
    raise MarketCollectionError("최근 거래일 factor snapshot을 수집하지 못했습니다.")


def fetch_universe_data(
    market: str,
    as_of: str,
    *,
    fetcher: Callable[[str, str], list[tuple[str, str]]] | None = None,
) -> list[tuple[str, str]]:
    market_n = normalize_market(market)
    as_of_n = normalize_as_of(as_of)
    if fetcher is not None:
        raw = fetcher(market_n, as_of_n)
    else:
        with _quiet_external_library_output():
            from pykrx import stock

            codes = {"KOSPI200": "1028", "KOSDAQ150": "2203"}
            if market_n in codes:
                tickers = stock.get_index_portfolio_deposit_file(codes[market_n], as_of_n)
            else:
                kospi = set(stock.get_index_portfolio_deposit_file(codes["KOSPI200"], as_of_n))
                kosdaq = set(stock.get_index_portfolio_deposit_file(codes["KOSDAQ150"], as_of_n))
                tickers = sorted(kospi | kosdaq)
            raw = []
            for ticker in tickers:
                try:
                    name = stock.get_market_ticker_name(ticker)
                except Exception:
                    name = str(ticker)
                raw.append((str(ticker), str(name)))

    instruments: list[tuple[str, str]] = []
    for ticker, name in raw or []:
        try:
            ticker_n = normalize_ticker(ticker)
        except ValueError:
            continue
        name_n = str(name or "").strip()
        if name_n:
            instruments.append((ticker_n, name_n))
    if not instruments:
        raise MarketCollectionError("유효한 universe 종목이 없습니다.")
    return instruments


def write_universe_cache(
    market: str,
    as_of: str,
    instruments: list[tuple[str, str]],
    *,
    cache_dir: Path | str = PATHS.shareable_cache_dir,
) -> Path:
    market_n = normalize_market(market)
    as_of_n = normalize_as_of(as_of)
    clean = fetch_universe_data(
        market_n, as_of_n, fetcher=lambda _market, _date: instruments
    )
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"universe_{market_n}_{as_of_n}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps([[ticker, name] for ticker, name in clean], ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def collect_universe(
    market: str,
    *,
    as_of: str | None = None,
    cache_dir: Path | str = PATHS.shareable_cache_dir,
    fetcher: Callable[[str, str], list[tuple[str, str]]] | None = None,
) -> list[tuple[str, str]]:
    as_of_n = normalize_as_of(as_of or datetime.now().strftime("%Y%m%d"))
    instruments = fetch_universe_data(market, as_of_n, fetcher=fetcher)
    write_universe_cache(market, as_of_n, instruments, cache_dir=cache_dir)
    return instruments


def frame_to_ohlcv_payload(frame: Any, *, ticker: str, as_of: str) -> dict[str, Any]:
    ticker_n = normalize_ticker(ticker)
    as_of_n = normalize_as_of(as_of)
    if frame is None or not hasattr(frame, "columns") or len(frame) == 0:
        raise MarketCollectionError("OHLCV 응답이 비어 있습니다.")
    column_candidates = {
        "open": ("시가", "Open", "open"),
        "high": ("고가", "High", "high"),
        "low": ("저가", "Low", "low"),
        "close": ("종가", "Close", "close"),
        "volume": ("거래량", "Volume", "volume"),
    }
    selected = {
        field: next(
            (column for column in candidates if column in frame.columns), None
        )
        for field, candidates in column_candidates.items()
    }
    if selected["close"] is None:
        raise MarketCollectionError("OHLCV 종가 컬럼이 없습니다.")

    series: dict[str, list[Any]] = {"date": []}
    for field, column in selected.items():
        if column is not None:
            series[field] = []
    for row_index, raw_date in enumerate(frame.index):
        try:
            date_value = (
                raw_date.strftime("%Y%m%d")
                if hasattr(raw_date, "strftime")
                else datetime.fromisoformat(str(raw_date)).strftime("%Y%m%d")
            )
            row_values = {
                field: float(frame.iloc[row_index][column])
                for field, column in selected.items()
                if column is not None
            }
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in row_values.values()):
            continue
        series["date"].append(date_value)
        for field, value in row_values.items():
            series[field].append(value)
    if not series["date"]:
        raise MarketCollectionError("유효한 OHLCV 시계열이 없습니다.")
    return {
        "ticker": ticker_n,
        "as_of": as_of_n,
        "series": series,
    }


def write_ohlcv_payload_cache(
    payload: dict[str, Any],
    *,
    ohlcv_dir: Path | str = PATHS.shareable_cache_dir / "ohlcv",
) -> Path:
    ticker_n = normalize_ticker(payload.get("ticker", ""))
    as_of_n = normalize_as_of(payload.get("as_of", ""))
    raw_series = payload.get("series")
    if not isinstance(raw_series, dict):
        raise MarketCollectionError("OHLCV series 형식이 올바르지 않습니다.")
    allowed = ("date", "open", "high", "low", "close", "volume")
    series = {
        field: list(raw_series[field])
        for field in allowed
        if isinstance(raw_series.get(field), list)
    }
    closes = series.get("close")
    if not closes:
        raise MarketCollectionError("OHLCV close 시계열이 없습니다.")
    expected = len(closes)
    if any(len(values) != expected for values in series.values()):
        raise MarketCollectionError("OHLCV 시계열 길이가 일치하지 않습니다.")
    destination = Path(ohlcv_dir)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{ticker_n}_{as_of_n}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(series, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def write_ohlcv_cache(
    ticker: str,
    as_of: str,
    close_series: Any,
    *,
    dates: list[str] | None = None,
    ohlcv_dir: Path | str = PATHS.shareable_cache_dir / "ohlcv",
) -> Path | None:
    ticker_n = normalize_ticker(ticker)
    as_of_n = normalize_as_of(as_of)
    if close_series is None:
        return None
    values = close_series.tolist() if hasattr(close_series, "tolist") else close_series
    try:
        closes = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    if not closes:
        return None
    payload: dict[str, Any] = {"close": closes}
    if dates is not None and len(dates) == len(closes):
        payload["date"] = [str(value) for value in dates]
    return write_ohlcv_payload_cache(
        {"ticker": ticker_n, "as_of": as_of_n, "series": payload},
        ohlcv_dir=ohlcv_dir,
    )


def collect_ohlcv(
    ticker: str,
    start: str,
    end: str,
    *,
    ohlcv_dir: Path | str = PATHS.shareable_cache_dir / "ohlcv",
    fetcher: Callable[[str, str, str], Any] = _fetch_ohlcv_frame,
) -> dict[str, Any]:
    ticker_n = normalize_ticker(ticker)
    start_n = normalize_as_of(start)
    end_n = normalize_as_of(end)
    frame = fetcher(ticker_n, start_n, end_n)
    payload = frame_to_ohlcv_payload(frame, ticker=ticker_n, as_of=end_n)
    write_ohlcv_payload_cache(payload, ohlcv_dir=ohlcv_dir)
    return payload


def frame_to_payload(frame: Any, *, index_name: str, as_of: str) -> dict[str, Any]:
    index_n = normalize_index_name(index_name)
    as_of_n = normalize_as_of(as_of)
    if frame is None or not hasattr(frame, "columns") or len(frame) == 0:
        raise MarketCollectionError("market index 응답이 비어 있습니다.")

    close_column = next(
        (column for column in ("종가", "Close", "close") if column in frame.columns),
        None,
    )
    if close_column is None:
        raise MarketCollectionError("market index 종가 컬럼이 없습니다.")

    dates: list[str] = []
    closes: list[float] = []
    for raw_date, raw_close in zip(frame.index, frame[close_column]):
        try:
            if hasattr(raw_date, "strftime"):
                date_value = raw_date.strftime("%Y%m%d")
            else:
                date_value = datetime.fromisoformat(str(raw_date)).strftime("%Y%m%d")
            close_value = float(raw_close)
        except (TypeError, ValueError):
            continue
        if len(date_value) == 8 and date_value.isdigit() and math.isfinite(close_value):
            dates.append(date_value)
            closes.append(close_value)
    if not dates:
        raise MarketCollectionError("유효한 market index 시계열이 없습니다.")

    return {
        "index": index_n,
        "as_of": as_of_n,
        "series": {"date": dates, "close": closes},
    }


def write_market_index_cache(
    payload: dict[str, Any],
    *,
    root: Path | str = PATHS.shareable_root,
) -> Path:
    """지수 캐시 쓰기 — **기존 이력과 병합한다. 덮어쓰지 않는다.**

    2026-09-01 사고: 아침에 `vkospi_calibrate --collect 365`가 만든 407일
    이력을, 오후의 일간 수집기(20일 창)가 같은 파일명으로 **780바이트로
    덮었다.** 임계값 검증·소급 평가가 전부 이 이력에 걸려 있는데, 매일 도는
    잡이 1년치를 조용히 지우는 구조였다 — 짧은 창은 갱신용이지 이력의
    대체물이 아니다.

    그래서 같은 지수의 **가장 최신 기존 파일**을 읽어 날짜 기준으로 합친다
    (같은 날짜는 새 값이 이긴다). 파일명이 as_of로 갈려도 읽는 쪽이
    `sorted(glob)[-1]`을 쓰므로 최신 파일이 전체 이력을 담아야 한다.
    """
    index_n = normalize_index_name(payload.get("index", ""))
    as_of_n = normalize_as_of(payload.get("as_of", ""))
    cache_dir = Path(root) / "cache" / "indices"
    cache_dir.mkdir(parents=True, exist_ok=True)

    merged: dict[str, float] = {}
    existing = sorted(cache_dir.glob(f"market_index_{index_n}_*.json"))
    if existing:
        try:
            prior = json.loads(existing[-1].read_text(encoding="utf-8"))
            series = prior.get("series") or {}
            for d, c in zip(series.get("date") or [], series.get("close") or []):
                merged[str(d)] = float(c)
        except (OSError, ValueError, TypeError):
            # 깨진 기존 파일은 병합만 포기한다 — 새 데이터 쓰기는 계속.
            log.warning("기존 %s 캐시를 읽지 못해 병합 없이 씁니다", index_n)

    series = payload.get("series") or {}
    for d, c in zip(series.get("date") or [], series.get("close") or []):
        merged[str(d)] = float(c)          # 같은 날짜는 새 값이 이긴다

    days = sorted(merged)
    out = {
        "index": index_n,
        "as_of": as_of_n,
        "series": {"date": days, "close": [merged[d] for d in days]},
    }
    target = cache_dir / f"market_index_{index_n}_{as_of_n}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def collect_market_index(
    index_name: str,
    *,
    days: int,
    as_of: str | None = None,
    root: Path | str = PATHS.shareable_root,
    fetcher: Callable[[str, str, str], Any] = _fetch_index_frame,
) -> dict[str, Any]:
    index_n = normalize_index_name(index_name)
    days_n = int(days)
    if not 1 <= days_n <= 2000:
        raise ValueError("days는 1~2000 범위여야 합니다.")
    end_s = normalize_as_of(as_of or datetime.now().strftime("%Y%m%d"))
    end = datetime.strptime(end_s, "%Y%m%d")
    start_s = (end - timedelta(days=int(days_n * 1.6) + 30)).strftime("%Y%m%d")
    frame = fetcher(start_s, end_s, index_n)
    payload = frame_to_payload(frame, index_name=index_n, as_of=end_s)
    write_market_index_cache(payload, root=root)
    return payload


def collect_all(*, root: Path | str = PATHS.shareable_root) -> list[dict[str, Any]]:
    results = [collect_market_index("KOSPI", days=320, root=root)]
    try:
        results.append(collect_market_index("VKOSPI", days=20, root=root))
    except MarketCollectionError as exc:
        log.warning("VKOSPI 수집 건너뜀: %s", exc)
    return results


def collect_all_public(
    *, root: Path | str = PATHS.shareable_root
) -> list[dict[str, Any]]:
    """Collect all scheduled public datasets, isolating failures by dataset."""
    root_path = Path(root)
    cache_dir = root_path / "cache"
    summaries: list[dict[str, Any]] = []

    for payload in collect_all(root=root_path):
        summaries.append(
            {
                "dataset": "market_index",
                "name": payload["index"],
                "as_of": payload["as_of"],
                "rows": len(payload["series"]["close"]),
            }
        )

    today = datetime.now().strftime("%Y%m%d")
    try:
        mapping = collect_ticker_map(as_of=today, cache_dir=cache_dir)
        summaries.append(
            {"dataset": "ticker_map", "as_of": today, "rows": len(mapping)}
        )
    except Exception as exc:
        log.warning("ticker map 수집 건너뜀: %s", exc)

    for market in EXCHANGE_MARKETS:
        try:
            payload = collect_factor_snapshot(market, as_of=today, root=root_path)
            summaries.append(
                {
                    "dataset": "factors",
                    "market": market,
                    "as_of": payload["as_of"],
                    "fundamental_rows": len(payload["fundamentals"]),
                    "market_cap_rows": len(payload["market_caps"]),
                }
            )
        except Exception as exc:
            log.warning("%s factor 수집 건너뜀: %s", market, exc)

    for market in ("KOSPI200", "KOSDAQ150", "KOSPI200+KOSDAQ150"):
        try:
            instruments = collect_universe(market, as_of=today, cache_dir=cache_dir)
            summaries.append(
                {
                    "dataset": "universe",
                    "market": market,
                    "as_of": today,
                    "rows": len(instruments),
                }
            )
        except Exception as exc:
            log.warning("%s universe 수집 건너뜀: %s", market, exc)
    return summaries


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("all", "indices", "ticker-map", "factors"))
    parser.add_argument("--index", choices=("KOSPI", "VKOSPI", "all"))
    parser.add_argument("--days", type=int)
    args = parser.parse_args()
    if args.dataset == "all" or (args.dataset is None and args.index is None):
        summaries = collect_all_public()
    elif args.dataset == "ticker-map":
        today = datetime.now().strftime("%Y%m%d")
        mapping = collect_ticker_map(as_of=today)
        summaries = [{"dataset": "ticker_map", "as_of": today, "rows": len(mapping)}]
    elif args.dataset == "factors":
        summaries = []
        for market in EXCHANGE_MARKETS:
            payload = collect_factor_snapshot(market)
            summaries.append(
                {
                    "dataset": "factors",
                    "market": market,
                    "as_of": payload["as_of"],
                    "fundamental_rows": len(payload["fundamentals"]),
                    "market_cap_rows": len(payload["market_caps"]),
                }
            )
    elif args.dataset == "indices" or args.index == "all":
        results = collect_all()
        summaries = [
            {
                "dataset": "market_index",
                "name": payload["index"],
                "as_of": payload["as_of"],
                "rows": len(payload["series"]["close"]),
                "latest_date": payload["series"]["date"][-1],
                "latest_close": payload["series"]["close"][-1],
            }
            for payload in results
        ]
    else:
        default_days = 320 if args.index == "KOSPI" else 20
        results = [collect_market_index(args.index, days=args.days or default_days)]
        summaries = [
            {
                "dataset": "market_index",
                "name": payload["index"],
                "as_of": payload["as_of"],
                "rows": len(payload["series"]["close"]),
                "latest_date": payload["series"]["date"][-1],
                "latest_close": payload["series"]["close"][-1],
            }
            for payload in results
        ]
    print(json.dumps(summaries, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
