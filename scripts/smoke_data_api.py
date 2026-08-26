#!/usr/bin/env python3
"""Start, verify, and stop the Shareable Data API without installing it."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from data_api import DEFAULT_HOST, DEFAULT_PORT
from data_api_client import ShareableDataClient
from shareable_store import ShareableStore


PROJECT = Path(__file__).resolve().parent.parent
BASE_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


class SmokeError(RuntimeError):
    pass


def _fetch_json(url: str, *, timeout: float = 0.5) -> Any:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _probe_health() -> dict[str, Any] | None:
    try:
        payload = _fetch_json(f"{BASE_URL}/health")
    except (HTTPError, URLError, OSError, TimeoutError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _validate_health(payload: dict[str, Any]) -> None:
    expected = {
        "status": "ok",
        "service": "shareable_data_api",
        "boundary": "shareable-only",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise SmokeError(f"health 필드 불일치: {key}")


def _wait_for_health(
    process: Any,
    *,
    timeout: float = 10.0,
    probe: Callable[[], dict[str, Any] | None] = _probe_health,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise SmokeError(f"Data API가 시작 전에 종료됨: {output[-500:]}")
        payload = probe()
        if payload is not None:
            _validate_health(payload)
            return payload
        sleeper(0.1)
    raise SmokeError("Data API readiness timeout")


def _stop_owned_process(process: Any) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        process.terminate()
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _assert_private_route_absent() -> None:
    try:
        urlopen(f"{BASE_URL}/v1/private/watchlist", timeout=0.5)
    except HTTPError as exc:
        if exc.code == 404:
            return
        raise SmokeError(f"Private 경로 상태 코드가 404가 아님: {exc.code}") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise SmokeError("Private 경로 차단 확인 중 API 연결 실패") from exc
    raise SmokeError("Private 경로가 노출됨")


def _wait_until_stopped(*, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _probe_health() is None:
            return
        time.sleep(0.1)
    raise SmokeError("Data API 종료 후에도 health가 응답함")


def run_smoke(*, instrument_ticker: str, ohlcv_ticker: str) -> dict[str, Any]:
    if _probe_health() is not None:
        raise SmokeError("8090 Data API가 이미 실행 중이므로 소유권을 확인할 수 없어 중단")

    store = ShareableStore()
    instrument_expected = store.get_instrument(instrument_ticker)
    ohlcv_expected = store.latest_ohlcv(ohlcv_ticker)
    as_of = str(ohlcv_expected["as_of"])
    expected_rows = len(ohlcv_expected["series"].get("close", []))
    if expected_rows == 0:
        raise SmokeError("검증용 OHLCV close 캐시가 비어 있음")

    env = os.environ.copy()
    env.pop("AI_AGENT_DATA_API_ENABLED", None)
    process = subprocess.Popen(
        [sys.executable, str(PROJECT / "scripts" / "data_api.py")],
        cwd=str(PROJECT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    old_flag = os.environ.get("AI_AGENT_DATA_API_ENABLED")
    api_rows = 0
    kium_api_rows = 0
    kospi_api_rows = 0
    fallback_rows = 0
    try:
        try:
            health = _wait_for_health(process)
            _assert_private_route_absent()
            client = ShareableDataClient(timeout=0.5, failure_cooldown=0)
            instrument = client.get_instrument(instrument_ticker)
            if instrument is None or instrument["name"] != instrument_expected["name"]:
                raise SmokeError("instrument API 결과 불일치")

            os.environ["AI_AGENT_DATA_API_ENABLED"] = "1"
            import invest_bot
            import kium_bot
            import quant_bot

            invest_bot._DATA_API_CLIENT = client
            resolved = invest_bot.resolve_ticker(instrument_expected["name"])
            expected_resolved = (instrument_ticker, instrument_expected["name"])
            if resolved != expected_resolved:
                raise SmokeError(f"invest_bot API 해석 불일치: {resolved}")

            quant_bot._DATA_API_CLIENT = client
            api_frame = quant_bot._load_ohlcv_cache(ohlcv_ticker, as_of)
            api_rows = len(api_frame) if api_frame is not None else 0
            if api_rows != expected_rows:
                raise SmokeError("quant_bot API OHLCV 행 수 불일치")

            kium_bot._DATA_API_CLIENT = client
            raw_fetch = kium_bot._fetch_ohlcv_raw
            try:
                kium_bot._fetch_ohlcv_raw = lambda *args: (_ for _ in ()).throw(
                    SmokeError("kium_bot이 API 대신 pykrx 폴백을 사용함")
                )
                kium_frame = kium_bot._load_ohlcv(
                    ohlcv_ticker, "20000101", as_of
                )
                kium_api_rows = len(kium_frame) if kium_frame is not None else 0
            finally:
                kium_bot._fetch_ohlcv_raw = raw_fetch
            if kium_api_rows != expected_rows:
                raise SmokeError("kium_bot API OHLCV 행 수 불일치")

            market_index = client.latest_market_index("KOSPI")
            if market_index is None:
                raise SmokeError("KOSPI market index API 캐시 없음")
            collect_index = kium_bot._collect_market_index
            try:
                kium_bot._collect_market_index = lambda *args, **kwargs: (
                    _ for _ in ()
                ).throw(SmokeError("kium_bot이 KOSPI API 대신 collector를 사용함"))
                kospi_close = kium_bot.fetch_kospi_close()
                kospi_api_rows = len(kospi_close) if kospi_close is not None else 0
            finally:
                kium_bot._collect_market_index = collect_index
            if kospi_api_rows != len(market_index["series"]["close"]):
                raise SmokeError("kium_bot KOSPI API 행 수 불일치")
        finally:
            _stop_owned_process(process)
            _wait_until_stopped()

        import quant_bot

        quant_bot._DATA_API_CLIENT = ShareableDataClient(
            timeout=0.2, failure_cooldown=30
        )
        fallback_frame = quant_bot._load_ohlcv_cache(ohlcv_ticker, as_of)
        fallback_rows = len(fallback_frame) if fallback_frame is not None else 0
        if fallback_rows != expected_rows:
            raise SmokeError("Data API 종료 후 OHLCV 디스크 폴백 불일치")
    finally:
        if old_flag is None:
            os.environ.pop("AI_AGENT_DATA_API_ENABLED", None)
        else:
            os.environ["AI_AGENT_DATA_API_ENABLED"] = old_flag

    return {
        "health": health,
        "instrument": instrument_ticker,
        "instrument_name": instrument_expected["name"],
        "ohlcv_ticker": ohlcv_ticker,
        "as_of": as_of,
        "api_rows": api_rows,
        "kium_api_rows": kium_api_rows,
        "kospi_api_rows": kospi_api_rows,
        "fallback_rows": fallback_rows,
        "private_route": 404,
        "server_stopped": _probe_health() is None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument-ticker", default="005930")
    parser.add_argument("--ohlcv-ticker", default="000660")
    args = parser.parse_args()
    try:
        result = run_smoke(
            instrument_ticker=args.instrument_ticker,
            ohlcv_ticker=args.ohlcv_ticker,
        )
    except (SmokeError, ValueError) as exc:
        parser.exit(2, f"오류: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
