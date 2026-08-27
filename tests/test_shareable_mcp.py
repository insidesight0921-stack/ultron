from __future__ import annotations

import asyncio

from mcp import Client

from shareable_mcp import MCP_TOOL_ALLOWLIST, build_mcp


class FakeShareableClient:
    def __init__(self):
        self.calls = []

    def get_instrument(self, ticker):
        self.calls.append(("get_instrument", ticker))
        return {
            "ticker": ticker,
            "name": "삼성전자",
            "corp_code": "00126380",
            "as_of": "20260827",
            "private": "must-not-leak",
        }

    def search_instruments(self, query, *, limit=20):
        self.calls.append(("search_instruments", query, limit))
        return [
            {
                "ticker": "005930",
                "name": query,
                "as_of": "20260827",
                "chat_id": "must-not-leak",
            }
        ]

    def latest_ohlcv(self, ticker):
        self.calls.append(("latest_ohlcv", ticker))
        return {
            "ticker": ticker,
            "as_of": "20260827",
            "series": {
                "date": ["20260827"],
                "close": [100.0],
                "token": "must-not-leak",
            },
            "portfolio": "must-not-leak",
        }

    def latest_market_index(self, index_name):
        self.calls.append(("latest_market_index", index_name))
        return {
            "index": index_name,
            "as_of": "20260827",
            "series": {
                "date": ["20260827"],
                "close": [3000.0],
                "secret": "must-not-leak",
            },
        }

    def latest_universe(self, market):
        self.calls.append(("latest_universe", market))
        return {
            "market": market,
            "as_of": "20260827",
            "instruments": [
                {
                    "ticker": "005930",
                    "name": "삼성전자",
                    "watchlist": "must-not-leak",
                }
            ],
            "path": "must-not-leak",
        }

    def latest_fundamentals(self, market, ticker):
        self.calls.append(("latest_fundamentals", market, ticker))
        return {
            "market": market,
            "ticker": ticker,
            "as_of": "20260827",
            "fundamentals": {
                "BPS": 50000.0,
                "PBR": 1.2,
                "secret": "must-not-leak",
            },
            "market_cap": 4e15,
            "portfolio": "must-not-leak",
        }


def test_mcp_tool_allowlist_is_exact_and_has_no_private_or_generic_surface():
    async def scenario():
        async with Client(build_mcp(FakeShareableClient())) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == MCP_TOOL_ALLOWLIST
            rendered = " ".join(names).lower()
            for forbidden in (
                "private",
                "watchlist",
                "schedule",
                "portfolio",
                "trade",
                "rag",
                "file",
                "path",
                "sql",
            ):
                assert forbidden not in rendered

    asyncio.run(scenario())


def test_mcp_tools_are_explicitly_annotated_read_only_and_closed_world():
    async def scenario():
        async with Client(build_mcp(FakeShareableClient())) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == MCP_TOOL_ALLOWLIST
            assert {
                tool.name: tool.annotations.model_dump(
                    by_alias=True,
                    exclude_none=True,
                )
                for tool in tools.tools
            } == {
                name: {
                    "readOnlyHint": True,
                    "openWorldHint": False,
                }
                for name in MCP_TOOL_ALLOWLIST
            }

    asyncio.run(scenario())


def test_mcp_tools_delegate_only_to_typed_shareable_client_methods():
    async def scenario():
        fake = FakeShareableClient()
        async with Client(
            build_mcp(fake), raise_exceptions=True
        ) as client:
            instrument = await client.call_tool(
                "get_instrument", {"ticker": "005930"}
            )
            search = await client.call_tool(
                "search_instruments", {"query": "삼성전자", "limit": 3}
            )
            ohlcv = await client.call_tool(
                "get_latest_ohlcv", {"ticker": "005930"}
            )
            market_index = await client.call_tool(
                "get_latest_market_index", {"index_name": "KOSPI"}
            )
            universe = await client.call_tool(
                "get_latest_universe", {"market": "KOSPI200"}
            )
            fundamentals = await client.call_tool(
                "get_latest_fundamentals",
                {"market": "KOSPI", "ticker": "005930"},
            )

            assert instrument.structured_content["ticker"] == "005930"
            assert search.structured_content["result"][0]["name"] == "삼성전자"
            assert ohlcv.structured_content["ticker"] == "005930"
            assert market_index.structured_content["index"] == "KOSPI"
            assert universe.structured_content["market"] == "KOSPI200"
            assert fundamentals.structured_content == {
                "market": "KOSPI",
                "ticker": "005930",
                "as_of": "20260827",
                "fundamentals": {"BPS": 50000.0, "PBR": 1.2},
                "market_cap": 4e15,
            }
            assert fake.calls == [
                ("get_instrument", "005930"),
                ("search_instruments", "삼성전자", 3),
                ("latest_ohlcv", "005930"),
                ("latest_market_index", "KOSPI"),
                ("latest_universe", "KOSPI200"),
                ("latest_fundamentals", "KOSPI", "005930"),
            ]
            rendered = " ".join(
                str(result.structured_content)
                for result in (
                    instrument,
                    search,
                    ohlcv,
                    market_index,
                    universe,
                    fundamentals,
                )
            )
            assert "must-not-leak" not in rendered

    asyncio.run(scenario())


def test_mcp_input_schema_contains_no_path_sql_or_private_arguments():
    async def scenario():
        async with Client(build_mcp(FakeShareableClient())) as client:
            tools = await client.list_tools()
            properties = {
                tool.name: set(tool.input_schema.get("properties", {}))
                for tool in tools.tools
            }
            assert properties == {
                "get_instrument": {"ticker"},
                "search_instruments": {"query", "limit"},
                "get_latest_ohlcv": {"ticker"},
                "get_latest_market_index": {"index_name"},
                "get_latest_universe": {"market"},
                "get_latest_fundamentals": {"market", "ticker"},
            }

    asyncio.run(scenario())
