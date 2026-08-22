#!/usr/bin/env python3
"""Loopback-only Shareable Data API for Phase 2 serverization."""
from __future__ import annotations

import argparse
import ipaddress

import uvicorn
from fastapi import FastAPI, HTTPException

from shareable_store import ShareableNotFound, ShareableStore, ShareableStoreError
from storage_paths import PATHS


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
API_VERSION = "v1"


def validate_bind_host(host: str) -> str:
    value = str(host or "").strip()
    if value == "localhost":
        return value
    try:
        if ipaddress.ip_address(value).is_loopback:
            return value
    except ValueError:
        pass
    raise ValueError("Data API는 loopback 주소에만 바인딩할 수 있습니다.")


def create_app(store: ShareableStore | None = None) -> FastAPI:
    shareable = store or ShareableStore()
    app = FastAPI(title="Ultron Shareable Data API", version=API_VERSION)

    @app.get("/health")
    async def health() -> dict[str, str]:
        if not shareable.ready():
            raise HTTPException(status_code=503, detail="shareable storage unavailable")
        return {
            "status": "ok",
            "service": "shareable_data_api",
            "version": API_VERSION,
            "storage_layout": PATHS.layout,
            "boundary": "shareable-only",
        }

    @app.get("/v1/shareable/instruments/{ticker}")
    async def instrument(ticker: str):
        try:
            return shareable.get_instrument(ticker)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ShareableNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ShareableStoreError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/v1/shareable/ohlcv/{ticker}/latest")
    async def latest_ohlcv(ticker: str):
        try:
            return shareable.latest_ohlcv(ticker)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ShareableNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ShareableStoreError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app


app = create_app()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    try:
        host = validate_bind_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.port <= 65535:
        parser.error("port는 1~65535 범위여야 합니다.")
    uvicorn.run(app, host=host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
