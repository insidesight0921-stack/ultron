"""Read-only access to assets explicitly classified as Shareable.

The production root is fixed by ``storage_paths``.  Callers provide only a
validated ticker; arbitrary paths, filenames, and SQL are intentionally absent
from this interface.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from storage_paths import PATHS


_TICKER_RE = re.compile(r"^[0-9]{6}$")
_TICKER_MAP_RE = re.compile(r"^ticker_map_([0-9]{8})[.]json$")
_OHLCV_RE = re.compile(r"^([0-9]{6})_([0-9]{8})[.]json$")
_SERIES_FIELDS = ("date", "dates", "open", "high", "low", "close", "volume")


class ShareableStoreError(RuntimeError):
    """Base error for the Shareable storage boundary."""


class ShareableNotFound(ShareableStoreError):
    """Requested public asset does not exist in the local cache."""


def normalize_ticker(ticker: str) -> str:
    value = str(ticker or "").strip()
    if not _TICKER_RE.fullmatch(value):
        raise ValueError("ticker는 6자리 숫자여야 합니다.")
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
        series = {
            field: payload[field]
            for field in _SERIES_FIELDS
            if field in payload and isinstance(payload[field], list)
        }
        if not series:
            raise ShareableStoreError("허용된 OHLCV 시계열이 없습니다.")
        return {"ticker": ticker_n, "as_of": as_of, "series": series}
