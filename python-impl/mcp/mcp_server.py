"""
MCP工具协议服务端 — Model Context Protocol实现
遵循Anthropic MCP标准，通过JSON-RPC 2.0提供工具注册/发现/调用能力。
支持动态工具扩展，Agent通过统一接口调用外部系统。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
from datetime import datetime


@dataclass
class ToolDefinition:
    """MCP工具定义"""
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    category: str = "general"
    requires_auth: bool = False


@dataclass
class ToolCallResult:
    """工具调用结果"""
    tool_name: str
    success: bool
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class MCPToolServer:
    """
    MCP工具服务端。

    实现 Model Context Protocol 的核心功能：
    1. 工具注册 (Tool Registration)
    2. 工具发现 (Tool Discovery) - Agent可查询可用工具列表
    3. 工具调用 (Tool Invocation) - 通过JSON-RPC 2.0协议调用
    4. 结果返回 (Result Delivery)

    遵循MCP规范：
    - 使用JSON-RPC 2.0消息格式
    - 支持工具的inputSchema声明
    - 提供标准化的错误码
    """

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._call_log: list[ToolCallResult] = []

    def register_tool(self, tool: ToolDefinition) -> None:
        """注册一个MCP工具"""
        self._tools[tool.name] = tool

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        category: str = "general",
        requires_auth: bool = False,
    ) -> Callable:
        """工具注册装饰器"""
        def decorator(func: Callable[..., Awaitable[Any]]) -> Callable:
            tool = ToolDefinition(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=func,
                category=category,
                requires_auth=requires_auth,
            )
            self._tools[name] = tool
            return func
        return decorator

    def list_tools(self, category: str | None = None) -> list[dict]:
        """
        工具发现：列出所有可用工具。
        对应MCP的 tools/list 方法。
        """
        tools = []
        for tool in self._tools.values():
            if category and tool.category != category:
                continue
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "category": tool.category,
            })
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        """
        工具调用：执行指定工具。
        对应MCP的 tools/call 方法。
        """
        import time

        tool = self._tools.get(name)
        if tool is None:
            result = ToolCallResult(
                tool_name=name,
                success=False,
                error=f"Tool '{name}' not found. Available: {list(self._tools.keys())}",
            )
            self._call_log.append(result)
            return result

        start = time.time()
        try:
            output = await tool.handler(**arguments)
            duration_ms = (time.time() - start) * 1000

            result = ToolCallResult(
                tool_name=name,
                success=True,
                result=output,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start) * 1000
            result = ToolCallResult(
                tool_name=name,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )

        self._call_log.append(result)
        return result

    async def handle_jsonrpc(self, request: dict) -> dict:
        """
        处理JSON-RPC 2.0请求。
        MCP协议传输层实现。
        """
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id", 1)

        try:
            if method == "tools/list":
                result = self.list_tools(category=params.get("category"))
            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                call_result = await self.call_tool(tool_name, arguments)
                result = {
                    "success": call_result.success,
                    "result": call_result.result,
                    "error": call_result.error,
                }
            elif method == "ping":
                result = {"status": "ok"}
            else:
                return {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": req_id,
                }

            return {"jsonrpc": "2.0", "result": result, "id": req_id}

        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": str(e)},
                "id": req_id,
            }

    def get_call_log(self, last_n: int = 100) -> list[dict]:
        """获取最近的工具调用日志"""
        return [
            {
                "tool": r.tool_name,
                "success": r.success,
                "duration_ms": r.duration_ms,
                "timestamp": r.timestamp,
                "error": r.error,
            }
            for r in self._call_log[-last_n:]
        ]


def create_default_tools(
    server: MCPToolServer,
    long_term_memory=None,
    sql_store=None,
) -> MCPToolServer:
    """Register tools backed by this application's actual stores.

    Product, order, knowledge, and ticket tools use this application's SQL/FAISS stores.
    Risk tools remain unavailable until a real risk-system integration exists.
    """
    if long_term_memory is not None:
        @server.register(
            name="knowledge_search",
            description="在客服金融政策知识库中检索依据；回答监管政策、合同条款和办理规则前必须调用",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要检索的金融业务问题"},
                    "top_k": {"type": "integer", "description": "最多返回的片段数", "default": 5},
                },
                "required": ["query"],
            },
            category="knowledge",
        )
        async def knowledge_search(query: str, top_k: int = 5, kb_id: str | None = None) -> list[dict]:
            return long_term_memory.search(query, top_k=max(1, min(int(top_k), 10)), kb_id=kb_id)

    if sql_store is not None:
        @server.register(
            name="product_query",
            description="查询 SQL 产品目录中的在售产品信息；支持产品编号、关键词或类别筛选",
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "产品名称或描述关键词"},
                    "product_code": {"type": "string", "description": "产品编号"},
                    "category": {"type": "string", "description": "产品类别"},
                    "limit": {"type": "integer", "description": "最多返回条数，1-50", "default": 10},
                },
            },
            category="product",
        )
        async def product_query(
            keyword: str = "",
            product_code: str = "",
            category: str = "",
            limit: int = 10,
        ) -> dict:
            products = sql_store.get_products(
                keyword=keyword,
                product_code=product_code,
                category=category,
                limit=limit,
            )
            return {"found": bool(products), "products": products}

        @server.register(
            name="order_query",
            description="查询当前用户的订单状态、金额和物流信息；可以指定订单号，否则列出当前用户最近订单",
            input_schema={
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号；留空时查询当前用户最近订单"},
                },
            },
            category="order",
        )
        async def order_query(order_id: str = "", user_id: str = "anonymous") -> dict:
            orders = sql_store.get_orders(
                order_id=order_id.strip() or None,
                user_id=user_id or "anonymous",
                limit=10,
            )
            return {"found": bool(orders), "orders": orders}

        @server.register(
            name="ticket_create",
            description="将用户明确要求创建的客服工单写入 SQL 数据库",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "工单标题"},
                    "description": {"type": "string", "description": "工单详细描述"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
                    "order_id": {"type": "string", "description": "关联订单号；退款工单必填，且必须属于当前用户"},
                    "category": {
                        "type": "string",
                        "enum": ["refund", "claim", "account_open", "account_change", "complaint", "general"],
                    },
                },
                "required": ["title", "description", "priority", "category"],
            },
            category="ticket",
        )
        async def ticket_create(
            title: str,
            description: str,
            priority: str,
            category: str,
            order_id: str = "",
            user_id: str = "anonymous",
        ) -> dict:
            import uuid
            from datetime import datetime

            allowed_priorities = {"low", "medium", "high", "urgent"}
            allowed_categories = {"refund", "claim", "account_open", "account_change", "complaint", "general"}
            title = title.strip()
            description = description.strip()
            if not title or not description:
                raise ValueError("工单标题和描述不能为空")
            if priority not in allowed_priorities or category not in allowed_categories:
                raise ValueError("工单优先级或类型无效")
            order_id = order_id.strip().upper()
            if category == "refund" and not order_id:
                raise ValueError("退款工单必须关联订单，请先提供订单号")

            ticket_id = f"TK-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
            return sql_store.create_ticket(
                ticket_id=ticket_id,
                ticket_type=category,
                priority=priority,
                status="created",
                summary=title,
                details=description,
                user_id=user_id or "anonymous",
                order_id=order_id or None,
            )

        @server.register(
            name="ticket_query",
            description="按工单号查询工单，或按订单号查询该订单关联的当前用户工单",
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string", "description": "工单编号；与 order_id 二选一"},
                    "order_id": {"type": "string", "description": "订单号；用于查询订单关联工单"},
                    "all_for_current_user": {"type": "boolean", "description": "列出当前用户自己的工单"},
                },
            },
            category="ticket",
        )
        async def ticket_query(
            ticket_id: str = "",
            order_id: str = "",
            all_for_current_user: bool = False,
            user_id: str = "anonymous",
        ) -> dict:
            if all_for_current_user:
                tickets = sql_store.get_tickets_by_user(user_id or "anonymous")
                return {
                    "found": bool(tickets),
                    "tickets": tickets,
                    "message": "当前账户没有工单记录" if not tickets else "",
                }
            if ticket_id.strip():
                ticket = sql_store.get_ticket(ticket_id.strip())
                if ticket is None or ticket["user_id"] != (user_id or "anonymous"):
                    return {"found": False, "message": "未找到属于当前用户的该工单"}
                return {"found": True, "ticket": ticket}
            if order_id.strip():
                tickets = sql_store.get_tickets_by_order(
                    order_id=order_id.strip().upper(),
                    user_id=user_id or "anonymous",
                )
                return {
                    "found": bool(tickets),
                    "tickets": tickets,
                    "message": "未找到该订单关联的当前用户工单" if not tickets else "",
                }
            return {"found": False, "message": "请提供工单号或订单号"}

    return server
