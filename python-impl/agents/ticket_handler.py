"""Ticket workflow agent; ticket data is accessed through MCP tools."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agents.intent_rules import (
    extract_order_id,
    is_refund_application_request,
    is_refund_order_followup,
)
from mcp.mcp_server import MCPToolServer
from mcp.tool_client import call_mcp_tool
from tracing.otel_config import trace_agent_call


TICKET_SYSTEM_PROMPT = """你是客服工单处理Agent。只为用户明确要求提交的业务申请、投诉或安全事件整理工单。
将请求整理为 JSON，字段为：
{
  "action": "create|query|list|unknown",
  "ticket_type": "refund|claim|account_open|account_change|complaint|general",
  "priority": "low|medium|high|urgent",
  "title": "简短标题",
  "description": "工单描述",
  "ticket_id": "如用户提供则填写",
  "order_id": "如用户提供则填写"
}
退款、理赔、开户和账户变更等明确办理请求可创建工单；询问政策、流程、条件或能否办理不创建工单。
不得编造用户身份、订单号或工单号。"""

_TICKET_ID = re.compile(r"\bTK-[A-Za-z0-9-]+\b", re.IGNORECASE)
_QUERY_MARKERS = ("查工单", "查询工单", "工单状态", "我的工单", "工单记录", "查一下工单")
_LIST_MARKERS = ("我的工单", "工单列表", "有哪些工单", "所有工单", "工单记录")
_VALID_TYPES = {"refund", "claim", "account_open", "account_change", "complaint", "general"}
_VALID_PRIORITIES = {"low", "medium", "high", "urgent"}


class TicketHandlerAgent:
    """Decides ticket actions and invokes ticket MCP tools itself."""

    def __init__(self, llm: ChatOpenAI, mcp_server: MCPToolServer):
        self.llm = llm
        self.mcp_server = mcp_server

    @trace_agent_call("ticket_analyze")
    async def analyze_request(self, user_message: str) -> dict[str, Any]:
        response = await self.llm.ainvoke([
            SystemMessage(content=TICKET_SYSTEM_PROMPT),
            HumanMessage(content=f"用户消息：{user_message}"),
        ])
        try:
            content = response.content
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            parsed = json.loads(str(content))
            return parsed if isinstance(parsed, dict) else {"action": "unknown"}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {"action": "unknown"}

    async def _create_ticket(
        self,
        state: dict[str, Any],
        *,
        title: str,
        description: str,
        category: str,
        priority: str = "medium",
        order_id: str = "",
    ) -> dict[str, Any]:
        return await call_mcp_tool(
            self.mcp_server,
            "ticket_create",
            {
                "title": title,
                "description": description,
                "category": category if category in _VALID_TYPES else "general",
                "priority": priority if priority in _VALID_PRIORITIES else "medium",
                "order_id": order_id,
                "user_id": state.get("user_id", "anonymous"),
            },
        )

    async def _query_ticket(
        self,
        state: dict[str, Any],
        *,
        ticket_id: str = "",
        order_id: str = "",
        all_for_current_user: bool = False,
    ) -> dict[str, Any]:
        return await call_mcp_tool(
            self.mcp_server,
            "ticket_query",
            {
                "ticket_id": ticket_id,
                "order_id": order_id,
                "all_for_current_user": all_for_current_user,
                "user_id": state.get("user_id", "anonymous"),
            },
        )

    @staticmethod
    def _format_create_result(result: dict[str, Any]) -> str:
        if not result.get("success"):
            return f"工单暂时无法提交：{result.get('error') or '工具调用失败'}"
        ticket = result.get("result") or {}
        if not isinstance(ticket, dict) or not ticket.get("ticket_id"):
            return "工单工具没有返回有效工单号，请稍后查询或联系人工客服。"
        order_ref = f"，关联订单：{ticket['order_id']}" if ticket.get("order_id") else ""
        return (
            f"工单已登记，工单号：{ticket['ticket_id']}{order_ref}。"
            f"类型：{ticket.get('type', '客服')}；状态：{ticket.get('status', 'created')}。"
        )

    @staticmethod
    def _format_query_result(result: dict[str, Any]) -> str:
        if not result.get("success"):
            return f"暂时无法查询工单：{result.get('error') or '工具调用失败'}"
        data = result.get("result") or {}
        if not isinstance(data, dict) or not data.get("found"):
            return data.get("message", "没有找到匹配的工单。") if isinstance(data, dict) else "没有找到匹配的工单。"

        tickets = data.get("tickets") or []
        if tickets:
            return "\n".join(
                f"工单 {ticket.get('ticket_id', '未知')}：类型 {ticket.get('type', '未知')}，"
                f"状态 {ticket.get('status', '未知')}，摘要：{ticket.get('summary', '无')}。"
                for ticket in tickets
            )
        ticket = data.get("ticket") or {}
        return (
            f"工单 {ticket.get('ticket_id', '未知')}：类型 {ticket.get('type', '未知')}，"
            f"状态 {ticket.get('status', '未知')}，摘要：{ticket.get('summary', '无')}，"
            f"关联订单：{ticket.get('order_id') or '无'}。"
        )

    @trace_agent_call("ticket_handler_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        messages = state.get("messages", [])
        if not messages:
            return state

        latest_text = str(messages[-1].content)
        order_id = extract_order_id(latest_text) or ""
        ticket_match = _TICKET_ID.search(latest_text)
        answer: str

        # A refund is a deterministic action. Ask for an order before writing;
        # the MCP server validates that it belongs to the current request user.
        if is_refund_application_request(latest_text) or is_refund_order_followup(messages):
            if not order_id:
                answer = "请提供退款订单号（例如 ORD-DEMO-20261028-001）；收到后我会创建关联该订单的退款工单。"
            else:
                request_context = "\n".join(
                    str(message.content) for message in messages[-4:]
                    if getattr(message, "type", "") in {"human", "ai"}
                )
                created = await self._create_ticket(
                    state,
                    title="退款申请",
                    description=f"用户原始申请及补充信息：\n{request_context}",
                    category="refund",
                    order_id=order_id,
                )
                answer = self._format_create_result(created)
        elif ticket_match or any(marker in latest_text for marker in _QUERY_MARKERS):
            all_mine = any(marker in latest_text for marker in _LIST_MARKERS)
            if not ticket_match and not order_id and not all_mine:
                answer = "请提供工单号或订单号；如果要查看自己的工单列表，可以说“查询我的工单”。"
            else:
                queried = await self._query_ticket(
                    state,
                    ticket_id=ticket_match.group().upper() if ticket_match else "",
                    order_id=order_id,
                    all_for_current_user=all_mine and not ticket_match and not order_id,
                )
                answer = self._format_query_result(queried)
        else:
            info = await self.analyze_request(latest_text)
            action = str(info.get("action", "unknown")).lower()
            if action == "query":
                queried = await self._query_ticket(
                    state,
                    ticket_id=str(info.get("ticket_id") or "").upper(),
                    order_id=str(info.get("order_id") or "").upper(),
                    all_for_current_user=not info.get("ticket_id") and not info.get("order_id"),
                )
                answer = self._format_query_result(queried)
            elif action == "list":
                queried = await self._query_ticket(state, all_for_current_user=True)
                answer = self._format_query_result(queried)
            elif action == "create":
                category = str(info.get("ticket_type", "general")).lower()
                order_id = str(info.get("order_id") or extract_order_id(latest_text) or "").upper()
                if category == "refund" and not order_id:
                    answer = "请提供退款订单号后再提交退款申请。"
                else:
                    created = await self._create_ticket(
                        state,
                        title=str(info.get("title") or info.get("summary") or "客服工单")[:255],
                        description=str(info.get("description") or info.get("details") or latest_text),
                        category=category,
                        priority=str(info.get("priority", "medium")).lower(),
                        order_id=order_id,
                    )
                    answer = self._format_create_result(created)
            else:
                answer = "我可以帮你提交或查询工单。请说明要办理的事项，或提供工单号/订单号。"

        return {
            **state,
            "current_agent": "ticket_handler",
            "rag_sources": [],
            "sub_results": {**state.get("sub_results", {}), "ticket_handler": answer},
        }
