#!/usr/bin/env python3
"""Read-only completion audit for the local Shareable MCP MVP."""
from __future__ import annotations

import argparse
import asyncio
import json
import tomllib
from pathlib import Path
from typing import Any

from shareable_mcp import MCP_TOOL_ALLOWLIST
from smoke_shareable_mcp import smoke


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
SERVER_NAME = "ultron-shareable"
EXPECTED_COMMAND = str(PROJECT / ".venv" / "bin" / "python")
EXPECTED_ARGS = [str(PROJECT / "scripts" / "shareable_mcp.py")]
FORBIDDEN_SURFACE = {
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


class MCPAuditError(RuntimeError):
    pass


def _same_file(actual: Any, expected: str) -> bool:
    if not isinstance(actual, str):
        return False
    try:
        return Path(actual).samefile(expected)
    except OSError:
        return False


def audit_codex_config(path: Path) -> dict[str, Any]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MCPAuditError("Codex config를 안전하게 읽을 수 없습니다.") from exc
    server = payload.get("mcp_servers", {}).get(SERVER_NAME)
    if not isinstance(server, dict):
        raise MCPAuditError("Codex MCP server 설정이 없습니다.")

    enabled_tools = server.get("enabled_tools")
    args = server.get("args")
    checks = {
        "command_same_file": _same_file(server.get("command"), EXPECTED_COMMAND),
        "args_same_file": (
            isinstance(args, list)
            and len(args) == 1
            and _same_file(args[0], EXPECTED_ARGS[0])
        ),
        "enabled_tools_exact": (
            isinstance(enabled_tools, list)
            and len(enabled_tools) == len(MCP_TOOL_ALLOWLIST)
            and set(enabled_tools) == MCP_TOOL_ALLOWLIST
        ),
        "approval_writes": server.get("default_tools_approval_mode") == "writes",
        "no_forwarded_env": not server.get("env") and not server.get("env_vars"),
        "stdio_only": "url" not in server,
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise MCPAuditError("Codex MCP 설정 drift: " + ", ".join(failed))
    return {
        "server": SERVER_NAME,
        "transport": "stdio",
        "enabled_tools": sorted(enabled_tools),
        "approval_mode": "writes",
        "checks": checks,
    }


def tool_scope_decision() -> dict[str, Any]:
    return {
        "accepted": {
            "get_latest_fundamentals": (
                "시장 전체 890/917종목 snapshot 대신 단일 ticker의 공개 "
                "BPS/PER/PBR/EPS/DPS/DIV·시가총액만 반환"
            )
        },
        "deferred": {
            "market_wide_factor_snapshot": "응답이 과대하며 단일 종목 도구로 충분",
            "private_data_tools": "Private 경계이며 영구 비노출",
            "rag_resources_or_prompts": "현재 Shareable MCP 사용 사례가 없고 개인 원문 노출 위험",
            "streamable_http": "로컬 stdio MVP 범위 밖; 인증·원격 위협모델 선행 필요",
        },
    }


async def audit(config_path: Path) -> dict[str, Any]:
    codex = audit_codex_config(config_path)
    runtime = await smoke()
    tools = set(runtime["tools"])
    if tools != MCP_TOOL_ALLOWLIST:
        raise MCPAuditError("runtime tool allowlist drift")
    if FORBIDDEN_SURFACE & {name.lower() for name in tools}:
        raise MCPAuditError("forbidden MCP surface detected")
    return {
        "ready": True,
        "scope": "local-stdio-mvp",
        "codex": codex,
        "runtime": runtime,
        "tool_scope_decision": tool_scope_decision(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-config",
        type=Path,
        default=DEFAULT_CODEX_CONFIG,
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(audit(args.codex_config.expanduser().resolve()))
    except MCPAuditError as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
