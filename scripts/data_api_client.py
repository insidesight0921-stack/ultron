"""Small loopback-only client for the Phase 2 Shareable Data API."""
from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from shareable_store import (
    normalize_exchange_market,
    normalize_market,
    normalize_market_index,
    normalize_query,
    normalize_ticker,
)


DEFAULT_BASE_URL = "http://127.0.0.1:8090"
DEFAULT_FAILURE_COOLDOWN = 30.0


class DataAPIError(RuntimeError):
    pass


class DataAPIUnavailable(DataAPIError):
    pass


def data_api_enabled() -> bool:
    return os.getenv("AI_AGENT_DATA_API_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def validate_base_url(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.username or parsed.password:
        raise ValueError("Data API URL은 인증정보 없는 loopback HTTP여야 합니다.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Data API URL에는 경로·쿼리·fragment를 넣을 수 없습니다.")
    host = parsed.hostname
    if host == "localhost":
        return value
    try:
        if host and ipaddress.ip_address(host).is_loopback:
            return value
    except ValueError:
        pass
    raise ValueError("Data API URL은 loopback 주소만 허용합니다.")


class ShareableDataClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 1.0,
        failure_cooldown: float = DEFAULT_FAILURE_COOLDOWN,
        opener: Any = urlopen,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self.timeout = float(timeout)
        self.failure_cooldown = max(0.0, float(failure_cooldown))
        self._opener = opener
        self._state_lock = threading.Lock()
        self._unavailable_until = 0.0

    def _circuit_is_open(self) -> bool:
        with self._state_lock:
            return time.monotonic() < self._unavailable_until

    def _mark_unavailable(self) -> None:
        with self._state_lock:
            self._unavailable_until = time.monotonic() + self.failure_cooldown

    def _mark_available(self) -> None:
        with self._state_lock:
            self._unavailable_until = 0.0

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self._circuit_is_open():
            raise DataAPIUnavailable("Data API 장애 cooldown 중입니다.")
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{self.base_url}{path}{query}",
            headers={"Accept": "application/json"},
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 404:
                return None
            self._mark_unavailable()
            raise DataAPIUnavailable(f"Data API HTTP {exc.code}") from exc
        except (URLError, OSError, TimeoutError, ValueError, TypeError) as exc:
            self._mark_unavailable()
            raise DataAPIUnavailable("Data API를 사용할 수 없습니다.") from exc
        self._mark_available()
        return payload

    def get_instrument(self, ticker: str) -> dict[str, Any] | None:
        ticker_n = normalize_ticker(ticker)
        payload = self._get_json(f"/v1/shareable/instruments/{ticker_n}")
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise DataAPIError("instrument 응답 형식이 올바르지 않습니다.")
        if payload.get("ticker") != ticker_n or not isinstance(payload.get("name"), str):
            raise DataAPIError("instrument 응답 필드가 올바르지 않습니다.")
        return payload

    def search_instruments(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        query_n = normalize_query(query)
        if not 1 <= limit <= 50:
            raise ValueError("limit은 1~50 범위여야 합니다.")
        payload = self._get_json(
            "/v1/shareable/instruments",
            {"query": query_n, "limit": limit},
        )
        if not isinstance(payload, list):
            raise DataAPIError("instrument 검색 응답 형식이 올바르지 않습니다.")
        results: list[dict[str, Any]] = []
        for item in payload:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("ticker"), str)
                or not isinstance(item.get("name"), str)
            ):
                raise DataAPIError("instrument 검색 항목이 올바르지 않습니다.")
            normalize_ticker(item["ticker"])
            results.append(item)
        return results

    def latest_ohlcv(self, ticker: str) -> dict[str, Any] | None:
        ticker_n = normalize_ticker(ticker)
        payload = self._get_json(f"/v1/shareable/ohlcv/{ticker_n}/latest")
        if payload is None:
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("ticker") != ticker_n
            or not isinstance(payload.get("as_of"), str)
            or not isinstance(payload.get("series"), dict)
        ):
            raise DataAPIError("OHLCV 응답 형식이 올바르지 않습니다.")
        series = payload["series"]
        close = series.get("close")
        if not isinstance(close, list) or not close:
            raise DataAPIError("OHLCV close 시계열이 없습니다.")
        normalized: dict[str, list[Any]] = {}
        for field in ("date", "dates"):
            values = series.get(field)
            if values is None:
                continue
            if not isinstance(values, list) or any(
                not isinstance(value, str)
                or len(value) != 8
                or not value.isdigit()
                for value in values
            ):
                raise DataAPIError("OHLCV date 값이 올바르지 않습니다.")
            normalized[field] = values
        for field in ("open", "high", "low", "close", "volume"):
            values = series.get(field)
            if values is None:
                continue
            if not isinstance(values, list):
                raise DataAPIError(f"OHLCV {field} 시계열이 올바르지 않습니다.")
            try:
                normalized[field] = [float(value) for value in values]
            except (TypeError, ValueError) as exc:
                raise DataAPIError(f"OHLCV {field} 값이 숫자가 아닙니다.") from exc
        expected = len(normalized["close"])
        if any(len(values) != expected for values in normalized.values()):
            raise DataAPIError("OHLCV 시계열 길이가 일치하지 않습니다.")
        payload["series"] = normalized
        return payload

    def latest_factors(self, market: str) -> dict[str, Any] | None:
        market_n = normalize_exchange_market(market)
        payload = self._get_json(
            f"/v1/shareable/factors/{quote(market_n, safe='')}/latest"
        )
        if payload is None:
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("market") != market_n
            or not isinstance(payload.get("as_of"), str)
            or not isinstance(payload.get("fundamentals"), dict)
            or not isinstance(payload.get("market_caps"), dict)
        ):
            raise DataAPIError("factor 응답 형식이 올바르지 않습니다.")

        fundamentals: dict[str, dict[str, float]] = {}
        for ticker, raw_values in payload["fundamentals"].items():
            ticker_n = normalize_ticker(ticker)
            if not isinstance(raw_values, dict):
                raise DataAPIError("fundamental 종목 형식이 올바르지 않습니다.")
            values: dict[str, float] = {}
            for field in ("BPS", "PER", "PBR", "EPS", "DPS", "DIV"):
                if field not in raw_values:
                    continue
                try:
                    values[field] = float(raw_values[field])
                except (TypeError, ValueError) as exc:
                    raise DataAPIError("fundamental 값이 숫자가 아닙니다.") from exc
            if values:
                fundamentals[ticker_n] = values

        market_caps: dict[str, float] = {}
        for ticker, raw_value in payload["market_caps"].items():
            ticker_n = normalize_ticker(ticker)
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise DataAPIError("시가총액 값이 숫자가 아닙니다.") from exc
            if value > 0:
                market_caps[ticker_n] = value
        if not fundamentals and not market_caps:
            raise DataAPIError("factor 응답이 비어 있습니다.")
        payload["fundamentals"] = fundamentals
        payload["market_caps"] = market_caps
        return payload

    def latest_fundamentals(
        self,
        market: str,
        ticker: str,
    ) -> dict[str, Any] | None:
        """Return one public instrument slice from the latest factor snapshot."""
        ticker_n = normalize_ticker(ticker)
        payload = self.latest_factors(market)
        if payload is None:
            return None
        fundamentals = payload["fundamentals"].get(ticker_n, {})
        market_cap = payload["market_caps"].get(ticker_n)
        if not fundamentals and market_cap is None:
            return None
        return {
            "market": payload["market"],
            "ticker": ticker_n,
            "as_of": payload["as_of"],
            "fundamentals": dict(fundamentals),
            "market_cap": market_cap,
        }

    def latest_universe(self, market: str) -> dict[str, Any] | None:
        market_n = normalize_market(market)
        payload = self._get_json(
            f"/v1/shareable/universes/{quote(market_n, safe='')}/latest"
        )
        if payload is None:
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("market") != market_n
            or not isinstance(payload.get("as_of"), str)
            or not isinstance(payload.get("instruments"), list)
        ):
            raise DataAPIError("universe 응답 형식이 올바르지 않습니다.")
        instruments: list[dict[str, str]] = []
        for item in payload["instruments"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("ticker"), str)
                or not isinstance(item.get("name"), str)
            ):
                raise DataAPIError("universe 종목 형식이 올바르지 않습니다.")
            normalize_ticker(item["ticker"])
            instruments.append({"ticker": item["ticker"], "name": item["name"]})
        if not instruments:
            raise DataAPIError("universe 종목이 비어 있습니다.")
        payload["instruments"] = instruments
        return payload

    def latest_market_index(self, index_name: str) -> dict[str, Any] | None:
        index_n = normalize_market_index(index_name)
        payload = self._get_json(
            f"/v1/shareable/market-indices/{quote(index_n, safe='')}/latest"
        )
        if payload is None:
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("index") != index_n
            or not isinstance(payload.get("as_of"), str)
            or not isinstance(payload.get("series"), dict)
        ):
            raise DataAPIError("market index 응답 형식이 올바르지 않습니다.")
        dates = payload["series"].get("date")
        closes = payload["series"].get("close")
        if (
            not isinstance(dates, list)
            or not isinstance(closes, list)
            or not dates
            or len(dates) != len(closes)
        ):
            raise DataAPIError("market index date/close 시계열이 올바르지 않습니다.")
        if any(not isinstance(value, str) or len(value) != 8 or not value.isdigit() for value in dates):
            raise DataAPIError("market index date 값이 올바르지 않습니다.")
        try:
            close_values = [float(value) for value in closes]
        except (TypeError, ValueError) as exc:
            raise DataAPIError("market index close 값이 숫자가 아닙니다.") from exc
        payload["series"] = {"date": dates, "close": close_values}
        return payload
