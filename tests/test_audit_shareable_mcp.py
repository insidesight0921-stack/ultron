from __future__ import annotations

from pathlib import Path

import pytest

from audit_shareable_mcp import (
    EXPECTED_ARGS,
    EXPECTED_COMMAND,
    MCPAuditError,
    MCP_TOOL_ALLOWLIST,
    audit_codex_config,
    tool_scope_decision,
)


def _write_config(path: Path, *, tools=None, approval="writes") -> None:
    enabled = sorted(MCP_TOOL_ALLOWLIST if tools is None else tools)
    rendered_tools = ", ".join(f'"{name}"' for name in enabled)
    path.write_text(
        "\n".join(
            (
                "[mcp_servers.ultron-shareable]",
                f'command = "{EXPECTED_COMMAND}"',
                f'args = ["{EXPECTED_ARGS[0]}"]',
                f"enabled_tools = [{rendered_tools}]",
                f'default_tools_approval_mode = "{approval}"',
                "",
            )
        ),
        encoding="utf-8",
    )


def test_codex_config_audit_accepts_exact_six_tool_boundary(tmp_path):
    config = tmp_path / "config.toml"
    _write_config(config)
    report = audit_codex_config(config)
    assert report["enabled_tools"] == sorted(MCP_TOOL_ALLOWLIST)
    assert report["approval_mode"] == "writes"
    assert all(report["checks"].values())


@pytest.mark.parametrize(
    ("tools", "approval"),
    (
        (MCP_TOOL_ALLOWLIST | {"read_file"}, "writes"),
        (MCP_TOOL_ALLOWLIST - {"get_instrument"}, "writes"),
        (MCP_TOOL_ALLOWLIST, "approve"),
    ),
)
def test_codex_config_audit_rejects_allowlist_or_approval_drift(
    tmp_path,
    tools,
    approval,
):
    config = tmp_path / "config.toml"
    _write_config(config, tools=tools, approval=approval)
    with pytest.raises(MCPAuditError):
        audit_codex_config(config)


def test_codex_config_audit_rejects_different_command_file(tmp_path):
    config = tmp_path / "config.toml"
    _write_config(config)
    rendered = config.read_text(encoding="utf-8").replace(
        EXPECTED_COMMAND,
        "/usr/bin/python3",
    )
    config.write_text(rendered, encoding="utf-8")
    with pytest.raises(MCPAuditError):
        audit_codex_config(config)


def test_tool_scope_decision_keeps_bulk_private_rag_and_remote_deferred():
    decision = tool_scope_decision()
    assert set(decision["accepted"]) == {"get_latest_fundamentals"}
    assert set(decision["deferred"]) == {
        "market_wide_factor_snapshot",
        "private_data_tools",
        "rag_resources_or_prompts",
        "streamable_http",
    }
