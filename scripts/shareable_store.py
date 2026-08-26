"""Read-only access to assets explicitly classified as Shareable.

The production root is fixed by ``storage_paths``.  Callers provide only a
validated ticker; arbitrary paths, filenames, and SQL are intentionally absent
from this interface.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from storage_paths import PATHS


_TICKER_RE = re.compile(r"^[0-9]{6}$")
_TICKER_MAP_RE = re.compile(r"^ticker_map_([0-9]{8})[.]json$")
_OHLCV_RE = re.compile(r"^([0-9]{6})_([0-9]{8})[.]json$")
_FACTOR_RE = re.compile(r"^factor_snapshot_(KOSPI|KOSDAQ)_([0-9]{8})[.]json$")
_VALID_MARKETS = {"KOSPI200", "KOSDAQ150", "KOSPI200+KOSDAQ150"}
_VALID_EXCHANGE_MARKETS = {"KOSPI", "KOSDAQ"}
_VALID_MARKET_INDICES = {"KOSPI", "VKOSPI"}
_SERIES_FIELDS = ("date", "dates", "open", "high", "low", "close", "volume")
_FACTOR_FIELDS = ("BPS", "PER", "PBR", "EPS", "DPS", "DIV")


class ShareableStoreError(RuntimeError):
    """Base error for the Shareable storage boundary."""


class ShareableNotFound(ShareableStoreError):
    """Requested public asset does not exist in the local cache."""


def normalize_ticker(ticker: str) -> str:
    value = str(ticker or "").strip()
    if not _TICKER_RE.fullmatch(value):
        raise ValueError("ticker는 6자리 숫자여야 합니다.")
    return value


def normalize_query(query: str) -> str:
    value = " ".join(str(query or "").split())
    if not value:
        raise ValueError("query는 비어 있을 수 없습니다.")
    if len(value) > 100:
        raise ValueError("query는 100자 이하여야 합니다.")
    return value


def normalize_market(market: str) -> str:
    value = str(market or "").strip().upper()
    if value not in _VALID_MARKETS:
        raise ValueError("지원하지 않는 universe market입니다.")
    return value


def normalize_market_index(index_name: str) -> str:
    value = str(index_name or "").strip().upper()
    if value not in _VALID_MARKET_INDICES:
        raise ValueError("지원하지 않는 market index입니다.")
    return value


def normalize_exchange_market(market: str) -> str:
    value = str(market or "").strip().upper()
    if value not in _VALID_EXCHANGE_MARKETS:
        raise ValueError("market은 KOSPI 또는 KOSDAQ이어야 합니다.")
    return value


class ShareableStore:
    def __init__(self, root: Path | str = PATHS.shareable_root) -> None:
        self.root = Path(root).resolve()
        self.cache_dir = self.root / "cache"

    def ready(self) -> bool:
        return self.root.is_dir() and self.cache_dir.is_dir()

    def _load_json(self, path: Path) -> Any:
        if path.is_symlink() or not path.is_file():
            raise ShareableStoreError("일반 Shareable 파일이 아닙니다.")
        try:
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ShareableStoreError("Shareable 루트를 벗어난 파일입니다.") from exc
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ShareableStoreError("Shareable JSON을 읽을 수 없습니다.") from exc

    def _latest_ticker_map(self) -> tuple[str | None, dict[str, str]]:
        candidates: list[tuple[str, Path]] = []
        if self.cache_dir.is_dir():
            for path in self.cache_dir.iterdir():
                match = _TICKER_MAP_RE.fullmatch(path.name)
                if match and path.is_file() and not path.is_symlink():
                    candidates.append((match.group(1), path))
        if not candidates:
            return None, {}
        as_of, path = max(candidates, key=lambda item: item[0])
        payload = self._load_json(path)
        if not isinstance(payload, dict):
            raise ShareableStoreError("ticker map 형식이 올바르지 않습니다.")
        mapping = {
            str(key): str(value)
            for key, value in payload.items()
            if _TICKER_RE.fullmatch(str(key)) and isinstance(value, str)
        }
        return as_of, mapping

    def get_instrument(self, ticker: str) -> dict[str, str | None]:
        ticker_n = normalize_ticker(ticker)
        as_of, ticker_map = self._latest_ticker_map()

        corp_payload: dict[str, Any] = {}
        corp_path = self.cache_dir / "corp_codes.json"
        if corp_path.is_file() and not corp_path.is_symlink():
            loaded = self._load_json(corp_path)
            if not isinstance(loaded, dict):
                raise ShareableStoreError("corp code 형식이 올바르지 않습니다.")
            candidate = loaded.get(ticker_n)
            if isinstance(candidate, dict):
                corp_payload = candidate

        name = ticker_map.get(ticker_n)
        if name is None and isinstance(corp_payload.get("corp_name"), str):
            name = corp_payload["corp_name"]
        if name is None:
            raise ShareableNotFound(f"종목을 찾을 수 없습니다: {ticker_n}")

        return {
            "ticker": ticker_n,
            "name": name,
            "corp_code": (
                str(corp_payload["corp_code"])
                if corp_payload.get("corp_code") is not None
                else None
            ),
            "as_of": as_of,
        }

    def search_instruments(self, query: str, *, limit: int = 20) -> list[dict[str, str | None]]:
        query_n = normalize_query(query)
        if not 1 <= limit <= 50:
            raise ValueError("limit은 1~50 범위여야 합니다.")
        as_of, ticker_map = self._latest_ticker_map()
        exact = [item for item in ticker_map.items() if item[1] == query_n]
        partial = [
            item
            for item in ticker_map.items()
            if query_n in item[1] and item[1] != query_n
        ]
        ordered = exact + sorted(partial, key=lambda item: (len(item[1]), item[1], item[0]))
        return [
            {"ticker": ticker, "name": name, "as_of": as_of}
            for ticker, name in ordered[:limit]
        ]

    def latest_universe(self, market: str) -> dict[str, Any]:
        market_n = normalize_market(market)
        pattern = re.compile(
            rf"^universe_{re.escape(market_n)}_([0-9]{{8}})[.]json$"
        )
        candidates: list[tuple[str, Path]] = []
        if self.cache_dir.is_dir():
            for path in self.cache_dir.iterdir():
                match = pattern.fullmatch(path.name)
                if match and path.is_file() and not path.is_symlink():
                    candidates.append((match.group(1), path))
        if not candidates:
            raise ShareableNotFound(f"universe 캐시를 찾을 수 없습니다: {market_n}")

        as_of, path = max(candidates, key=lambda item: item[0])
        payload = self._load_json(path)
        if not isinstance(payload, list):
            raise ShareableStoreError("universe 캐시 형식이 올바르지 않습니다.")
        instruments = []
        for item in payload:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and _TICKER_RE.fullmatch(str(item[0]))
                and isinstance(item[1], str)
                and item[1].strip()
            ):
                instruments.append({"ticker": str(item[0]), "name": item[1]})
        if not instruments:
            raise ShareableStoreError("유효한 universe 종목이 없습니다.")
        return {"market": market_n, "as_of": as_of, "instruments": instruments}

    def latest_market_index(self, index_name: str) -> dict[str, Any]:
        index_n = normalize_market_index(index_name)
        pattern = re.compile(
            rf"^market_index_{re.escape(index_n)}_([0-9]{{8}})[.]json$"
        )
        index_dir = self.cache_dir / "indices"
        candidates: list[tuple[str, Path]] = []
        if index_dir.is_dir() and not index_dir.is_symlink():
            for path in index_dir.iterdir():
                match = pattern.fullmatch(path.name)
                if match and path.is_file() and not path.is_symlink():
                    candidates.append((match.group(1), path))
        if not candidates:
            raise ShareableNotFound(f"market index 캐시를 찾을 수 없습니다: {index_n}")

        as_of, path = max(candidates, key=lambda item: item[0])
        payload = self._load_json(path)
        if not isinstance(payload, dict) or payload.get("index") != index_n:
            raise ShareableStoreError("market index 캐시 형식이 올바르지 않습니다.")
        series = payload.get("series")
        if not isinstance(series, dict):
            raise ShareableStoreError("market index 시계열이 없습니다.")
        dates = series.get("date")
        closes = series.get("close")
        if (
            not isinstance(dates, list)
            or not isinstance(closes, list)
            or not dates
            or len(dates) != len(closes)
        ):
            raise ShareableStoreError("market index date/close 형식이 올바르지 않습니다.")
        clean_dates: list[str] = []
        clean_closes: list[float] = []
        for date_value, close_value in zip(dates, closes):
            date_s = str(date_value)
            try:
                close_n = float(close_value)
            except (TypeError, ValueError):
                raise ShareableStoreError("market index close 값이 숫자가 아닙니다.")
            if len(date_s) != 8 or not date_s.isdigit():
                raise ShareableStoreError("market index date 형식이 올바르지 않습니다.")
            clean_dates.append(date_s)
            clean_closes.append(close_n)
        return {
            "index": index_n,
            "as_of": as_of,
            "series": {"date": clean_dates, "close": clean_closes},
        }

    def latest_ohlcv(self, ticker: str) -> dict[str, Any]:
        ticker_n = normalize_ticker(ticker)
        ohlcv_dir = self.cache_dir / "ohlcv"
        candidates: list[tuple[str, Path]] = []
        if ohlcv_dir.is_dir() and not ohlcv_dir.is_symlink():
            for path in ohlcv_dir.iterdir():
                match = _OHLCV_RE.fullmatch(path.name)
                if (
                    match
                    and match.group(1) == ticker_n
                    and path.is_file()
                    and not path.is_symlink()
                ):
                    candidates.append((match.group(2), path))
        if not candidates:
            raise ShareableNotFound(f"OHLCV 캐시를 찾을 수 없습니다: {ticker_n}")

        as_of, path = max(candidates, key=lambda item: item[0])
        payload = self._load_json(path)
        if not isinstance(payload, dict):
            raise ShareableStoreError("OHLCV 캐시 형식이 올바르지 않습니다.")
        raw_series = {
            field: payload[field]
            for field in _SERIES_FIELDS
            if field in payload and isinstance(payload[field], list)
        }
        if not raw_series or not raw_series.get("close"):
            raise ShareableStoreError("허용된 OHLCV 시계열이 없습니다.")
        series: dict[str, list[Any]] = {}
        for field in ("date", "dates"):
            values = raw_series.get(field)
            if values is None:
                continue
            dates = [str(value) for value in values]
            if any(len(value) != 8 or not value.isdigit() for value in dates):
                raise ShareableStoreError("OHLCV date 형식이 올바르지 않습니다.")
            series[field] = dates
        for field in ("open", "high", "low", "close", "volume"):
            values = raw_series.get(field)
            if values is None:
                continue
            try:
                numeric = [float(value) for value in values]
            except (TypeError, ValueError) as exc:
                raise ShareableStoreError(f"OHLCV {field} 값이 숫자가 아닙니다.") from exc
            if any(not math.isfinite(value) for value in numeric):
                raise ShareableStoreError(f"OHLCV {field} 값이 유한수가 아닙니다.")
            series[field] = numeric
        expected = len(series["close"])
        if any(len(values) != expected for values in series.values()):
            raise ShareableStoreError("OHLCV 시계열 길이가 일치하지 않습니다.")
        return {"ticker": ticker_n, "as_of": as_of, "series": series}

    def latest_factors(self, market: str) -> dict[str, Any]:
        market_n = normalize_exchange_market(market)
        factor_dir = self.cache_dir / "factors"
        candidates: list[tuple[str, Path]] = []
        if factor_dir.is_dir() and not factor_dir.is_symlink():
            for path in factor_dir.iterdir():
                match = _FACTOR_RE.fullmatch(path.name)
                if (
                    match
                    and match.group(1) == market_n
                    and path.is_file()
                    and not path.is_symlink()
                ):
                    candidates.append((match.group(2), path))
        if not candidates:
            raise ShareableNotFound(f"factor 캐시를 찾을 수 없습니다: {market_n}")

        as_of, path = max(candidates, key=lambda item: item[0])
        payload = self._load_json(path)
        if not isinstance(payload, dict):
            raise ShareableStoreError("factor 캐시 형식이 올바르지 않습니다.")

        fundamentals: dict[str, dict[str, float]] = {}
        raw_fundamentals = payload.get("fundamentals")
        if isinstance(raw_fundamentals, dict):
            for ticker, raw_values in raw_fundamentals.items():
                if not _TICKER_RE.fullmatch(str(ticker)) or not isinstance(
                    raw_values, dict
                ):
                    continue
                clean_values: dict[str, float] = {}
                for field in _FACTOR_FIELDS:
                    if field not in raw_values:
                        continue
                    try:
                        value = float(raw_values[field])
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(value):
                        clean_values[field] = value
                if clean_values:
                    fundamentals[str(ticker)] = clean_values

        market_caps: dict[str, float] = {}
        raw_caps = payload.get("market_caps")
        if isinstance(raw_caps, dict):
            for ticker, raw_value in raw_caps.items():
                if not _TICKER_RE.fullmatch(str(ticker)):
                    continue
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    market_caps[str(ticker)] = value

        if not fundamentals and not market_caps:
            raise ShareableStoreError("유효한 factor 데이터가 없습니다.")
        return {
            "market": market_n,
            "as_of": as_of,
            "fundamentals": fundamentals,
            "market_caps": market_caps,
        }
