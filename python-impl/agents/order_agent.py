"""Customer-facing order query agent backed by the MCP order tool."""

from __future__ import annotations

from typing import Any

from agents.intent_rules import extract_order_id
from mcp.mcp_server import MCPToolServer
from mcp.tool_client import call_mcp_tool
from tracing.otel_config import trace_agent_call


def _format_orders(result: dict[str, Any]) -> str:
    if not result.get("success"):
        return f"暂时无法查询订单：{result.get('error') or '工具调用失败'}"
    payload = result.get("result") or {}
    orders = payload.get("orders", []) if isinstance(payload, dict) else []
    if not orders:
        return "没有查到当前账户下的订单记录。请核对订单号，或确认当前登录账户是否正确。"

    lines = ["查询到以下订单："]
    for order in orders[:10]:
        line = (
            f"\n订单号：{order.get('order_id', '未知')}\n"
            f"产品：{order.get('product', '未记录')}\n"
            f"金额：{order.get('amount', '未记录')}\n"
            f"状态：{order.get('status', '未记录')}"
        )
        if order.get("created_at"):
            line += f"\n下单时间：{order['created_at']}"
        if order.get("shipping_company") or order.get("tracking_number"):
            line += (
                f"\n物流：{order.get('shipping_company') or ''} "
                f"{order.get('tracking_number') or ''}"
            ).rstrip()
        lines.append(line)
    return "".join(lines)


class OrderAgent:
    """Queries the current request user's orders through MCP."""

    def __init__(self, mcp_server: MCPToolServer):
        self.mcp_server = mcp_server

    @trace_agent_call("order_agent")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        messages = state.get("messages", [])
        if not messages:
            return state

        latest_text = str(messages[-1].content)
        result = await call_mcp_tool(
            self.mcp_server,
            "order_query",
            {
                "order_id": extract_order_id(latest_text) or "",
                # Use the request's user identity rather than model-generated
                # arguments so the model cannot switch the query scope.
                "user_id": state.get("user_id", "anonymous"),
            },
        )
        answer = _format_orders(result)
        return {
            **state,
            "current_agent": "order_agent",
            "rag_sources": [],
            "sub_results": {**state.get("sub_results", {}), "order_query": answer},
        }
