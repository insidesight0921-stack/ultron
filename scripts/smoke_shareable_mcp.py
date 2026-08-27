#!/usr/bin/env python3
"""Smoke-test the Shareable MCP server over a real stdio subprocess."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT = Path(__file__).resolve().parents[1]
SERVER = PROJECT / "scripts" / "shareable_mcp.py"
EXPECTED_TOOLS = {
    "get_instrument",
    "search_instruments",
    "get_latest_ohlcv",
    "get_latest_market_index",
    "get_latest_universe",
    "get_latest_fundamentals",
}


async def smoke() -> dict[str, object]:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        cwd=str(PROJECT),
    )
    async with Client(stdio_client(params)) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        if names != EXPECTED_TOOLS:
            raise RuntimeError("Shareable MCP tool allowlist mismatch")
        annotations = {
            tool.name: tool.annotations.model_dump(
                by_alias=True,
                exclude_none=True,
            )
            if tool.annotations is not None
            else None
            for tool in tools.tools
        }
        expected_annotations = {
            name: {
                "readOnlyHint": True,
                "openWorldHint": False,
            }
            for name in EXPECTED_TOOLS
        }
        if annotations != expected_annotations:
            raise RuntimeError("Shareable MCP tool annotations mismatch")
        result = await client.call_tool("get_instrument", {"ticker": "005930"})
        payload = result.structured_content
        if result.is_error or not isinstance(payload, dict):
            raise RuntimeError("Shareable MCP instrument smoke failed")
        if payload.get("ticker") != "005930" or payload.get("name") != "삼성전자":
            raise RuntimeError("Shareable MCP instrument payload mismatch")
        fundamentals_result = await client.call_tool(
            "get_latest_fundamentals",
            {"market": "KOSPI", "ticker": "005930"},
        )
        fundamentals = fundamentals_result.structured_content
        expected_fundamental_fields = {
            "market",
            "ticker",
            "as_of",
            "fundamentals",
            "market_cap",
        }
        if (
            fundamentals_result.is_error
            or not isinstance(fundamentals, dict)
            or fundamentals.get("ticker") != "005930"
            or set(fundamentals) != expected_fundamental_fields
            or not isinstance(fundamentals.get("fundamentals"), dict)
            or set(fundamentals["fundamentals"])
            - {"BPS", "PER", "PBR", "EPS", "DPS", "DIV"}
        ):
            raise RuntimeError("Shareable MCP fundamentals payload mismatch")
        forbidden = {
            "private",
            "watchlist",
            "schedule",
            "portfolio",
            "trade",
            "rag",
            "file",
            "path",
            "sql",
        }
        if forbidden & {name.lower() for name in names}:
            raise RuntimeError("Shareable MCP exposed a forbidden tool")
        return {
            "protocol_version": client.protocol_version,
            "server": client.server_info.name if client.server_info else None,
            "tools": sorted(names),
            "annotations": annotations,
            "instrument": {
                key: payload[key]
                for key in ("ticker", "name", "corp_code", "as_of")
                if key in payload
            },
            "fundamentals": fundamentals,
        }


def main() -> int:
    print(asyncio.run(smoke()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
