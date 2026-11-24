"""Small JSON-RPC client helper used by business agents to call MCP tools."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from mcp.mcp_server import MCPToolServer


async def call_mcp_tool(
    server: MCPToolServer,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke one registered MCP tool over the server's JSON-RPC interface."""
    response = await server.handle_jsonrpc({
        "jsonrpc": "2.0",
        "id": f"agent-{tool_name}-{uuid4().hex}",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments or {}},
    })
    if response.get("error"):
        error = response["error"]
        return {
            "success": False,
            "result": None,
            "error": error.get("message", "MCP 工具调用失败"),
        }

    result = response.get("result")
    if not isinstance(result, dict):
        return {"success": False, "result": None, "error": "MCP 工具返回格式无效"}
    return result
