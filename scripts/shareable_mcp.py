#!/usr/bin/env python3
"""Shareable-only MCP server backed exclusively by the loopback Data API."""
from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from data_api_client import DataAPIError, DataAPIUnavailable, ShareableDataClient


MCP_SERVER_NAME = "ultron-shareable"
MCP_SERVER_VERSION = "0.1.0"
MCP_TOOL_ALLOWLIST = frozenset(
    {
        "get_instrument",
        "search_instruments",
        "get_latest_ohlcv",
        "get_latest_market_index",
        "get_latest_universe",
        "get_latest_fundamentals",
    }
)
MCP_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    openWorldHint=False,
)


def _required(payload: Any, *, label: str) -> Any:
    if payload is None:
        raise ToolError(f"{label} 데이터를 찾을 수 없습니다.")
    return payload


def _shareable_call(label: str, callback):
    try:
        return _required(callback(), label=label)
    except (DataAPIError, DataAPIUnavailable, ValueError) as exc:
        raise ToolError(f"{label} 조회가 거부되었거나 현재 사용할 수 없습니다.") from exc


def _public_instrument(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in ("ticker", "name", "corp_code", "as_of")
        if key in payload
    }


def _public_search(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: item[key]
            for key in ("ticker", "name", "as_of")
            if key in item
        }
        for item in items
    ]


def _public_ohlcv(payload: dict[str, Any]) -> dict[str, Any]:
    series = payload["series"]
    return {
        "ticker": payload["ticker"],
        "as_of": payload["as_of"],
        "series": {
            key: series[key]
            for key in ("date", "open", "high", "low", "close", "volume")
            if key in series
        },
    }


def _public_market_index(payload: dict[str, Any]) -> dict[str, Any]:
    series = payload["series"]
    return {
        "index": payload["index"],
        "as_of": payload["as_of"],
        "series": {key: series[key] for key in ("date", "close") if key in series},
    }


def _public_universe(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": payload["market"],
        "as_of": payload["as_of"],
        "instruments": [
            {"ticker": item["ticker"], "name": item["name"]}
            for item in payload["instruments"]
        ],
    }


def _public_fundamentals(payload: dict[str, Any]) -> dict[str, Any]:
    fundamentals = payload["fundamentals"]
    return {
        "market": payload["market"],
        "ticker": payload["ticker"],
        "as_of": payload["as_of"],
        "fundamentals": {
            key: fundamentals[key]
            for key in ("BPS", "PER", "PBR", "EPS", "DPS", "DIV")
            if key in fundamentals
        },
        "market_cap": payload.get("market_cap"),
    }


def build_mcp(client: ShareableDataClient | None = None) -> MCPServer:
    """Build an explicit, read-only tool registry with no Private imports or paths."""
    shareable = client or ShareableDataClient()
    server = MCPServer(
        MCP_SERVER_NAME,
        version=MCP_SERVER_VERSION,
        description="Read-only public Korean market data from Ultron Shareable API.",
        instructions=(
            "Only public market data is available. Private watchlists, schedules, "
            "portfolios, trades, RAG documents, files, SQL, and arbitrary paths are "
            "not exposed."
        ),
    )
    registered: set[str] = set()

    def expose(name: str):
        if name not in MCP_TOOL_ALLOWLIST or name in registered:
            raise RuntimeError("MCP tool registration is not allowlisted")
        registered.add(name)
        return server.tool(
            name=name,
            annotations=MCP_READ_ONLY_ANNOTATIONS,
            structured_output=True,
        )

    @expose("get_instrument")
    def get_instrument(ticker: str) -> dict[str, Any]:
        """Get one public instrument by six-digit Korean ticker."""
        return _public_instrument(
            _shareable_call(
                "instrument",
                lambda: shareable.get_instrument(ticker),
            )
        )

    @expose("search_instruments")
    def search_instruments(query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search public instruments by ticker or name; limit must be 1 to 50."""
        return _public_search(
            _shareable_call(
                "instrument search",
                lambda: shareable.search_instruments(query, limit=limit),
            )
        )

    @expose("get_latest_ohlcv")
    def get_latest_ohlcv(ticker: str) -> dict[str, Any]:
        """Get the latest cached public OHLCV series for a Korean ticker."""
        return _public_ohlcv(
            _shareable_call(
                "OHLCV",
                lambda: shareable.latest_ohlcv(ticker),
            )
        )

    @expose("get_latest_market_index")
    def get_latest_market_index(index_name: str) -> dict[str, Any]:
        """Get the latest allowlisted public market-index history."""
        return _public_market_index(
            _shareable_call(
                "market index",
                lambda: shareable.latest_market_index(index_name),
            )
        )

    @expose("get_latest_universe")
    def get_latest_universe(market: str) -> dict[str, Any]:
        """Get the latest public instrument universe for an allowlisted market."""
        return _public_universe(
            _shareable_call(
                "market universe",
                lambda: shareable.latest_universe(market),
            )
        )

    @expose("get_latest_fundamentals")
    def get_latest_fundamentals(
        market: str,
        ticker: str,
    ) -> dict[str, Any]:
        """Get one instrument's latest public fundamentals and market cap."""
        return _public_fundamentals(
            _shareable_call(
                "fundamentals",
                lambda: shareable.latest_fundamentals(market, ticker),
            )
        )

    if frozenset(registered) != MCP_TOOL_ALLOWLIST:
        raise RuntimeError("MCP tool registry drifted from the explicit allowlist")
    return server


mcp = build_mcp()


def main() -> int:
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
