"""Customer-facing agent for the SQL product catalog."""

from __future__ import annotations

import re
from typing import Any

from mcp.mcp_server import MCPToolServer
from mcp.tool_client import call_mcp_tool
from tracing.otel_config import trace_agent_call


_PRODUCT_CODE = re.compile(r"\b(?:PROD|PRD)-[A-Za-z0-9][A-Za-z0-9-]*\b", re.IGNORECASE)
_CATEGORY_TERMS = (
    "理财", "基金", "存款", "保险", "贷款", "信用卡", "贵金属", "信托",
)
_FILLER_PATTERNS = (
    r"请问", r"麻烦", r"帮我", r"给我", r"我想(?:了解|查看|查询)?", r"我需要",
    r"查询一下", r"查一下", r"查看一下", r"介绍一下", r"介绍下", r"介绍", r"查询", r"查看",
    r"帮我看看", r"可以看看", r"看看", r"有哪些", r"哪些", r"有什么", r"列出", r"推荐",
    r"告诉我", r"我想知道", r"详情", r"信息", r"产品",
    r"查", r"请介绍", r"能否", r"可以",
    r"一下", r"请", r"呢", r"吗", r"？", r"\?", r"。", r"！", r"!",
)
_BROAD_LIST_TERMS = (
    "有哪些", "哪些", "有什么", "有没有", "列出", "全部产品", "所有产品", "产品列表", "产品目录",
)


def _product_query_args(text: str) -> dict[str, Any]:
    code_match = _PRODUCT_CODE.search(text or "")
    if code_match:
        return {"product_code": code_match.group().upper(), "limit": 10}

    category = next((term for term in _CATEGORY_TERMS if term in (text or "")), "")
    if any(term in (text or "") for term in _BROAD_LIST_TERMS):
        return {"category": category, "limit": 20}

    keyword = text or ""
    for pattern in _FILLER_PATTERNS:
        keyword = re.sub(pattern, "", keyword, flags=re.IGNORECASE)
    keyword = re.sub(r"\s+", "", keyword)
    if keyword == category:
        keyword = ""
    return {"keyword": keyword, "category": category, "limit": 10}


def _format_products(result: dict[str, Any]) -> str:
    if not result.get("success"):
        return f"暂时无法查询产品目录：{result.get('error') or '工具调用失败'}"
    payload = result.get("result") or {}
    products = payload.get("products", []) if isinstance(payload, dict) else []
    if not products:
        return "产品目录中没有找到匹配的在售产品。请核对产品名称或编号，或联系人工客服确认。"

    lines = ["产品目录查询结果（以下信息来自当前 SQL 产品目录）："]
    for product in products[:20]:
        lines.append(f"\n**{product.get('name', '未命名产品')}**（编号：{product.get('product_code', '无')}）")
        if product.get("category"):
            lines.append(f"类别：{product['category']}")
        if product.get("issuer"):
            lines.append(f"发行方：{product['issuer']}")
        if product.get("risk_level"):
            lines.append(f"风险等级：{product['risk_level']}")
        if product.get("min_purchase") is not None:
            lines.append(f"起购金额：{product['currency']} {product['min_purchase']}")
        if product.get("term_days") is not None:
            lines.append(f"期限：{product['term_days']} 天")
        if product.get("rate_info"):
            lines.append(f"利率/收益说明：{product['rate_info']}")
        if product.get("description"):
            lines.append(f"产品说明：{product['description']}")
        if product.get("official_source"):
            lines.append(f"信息来源：{product['official_source']}")
        if product.get("updated_at"):
            lines.append(f"目录更新时间：{product['updated_at']}")
    return "\n".join(lines) + "\n\n以上是产品目录当前记录；具体内容以发行机构最新正式文件为准，不构成投资建议。"


class ProductAgent:
    """Queries product facts from the SQL-backed catalog through MCP."""

    def __init__(self, mcp_server: MCPToolServer):
        self.mcp_server = mcp_server

    @trace_agent_call("product_agent")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        messages = state.get("messages", [])
        if not messages:
            return state

        query = str(messages[-1].content)
        query_args = _product_query_args(query)
        result = await call_mcp_tool(
            self.mcp_server,
            "product_query",
            query_args,
        )
        payload = result.get("result") if result.get("success") else None
        if (
            isinstance(payload, dict)
            and not payload.get("products")
            and query_args.get("keyword")
            and query_args.get("category")
        ):
            # A natural-language phrase may not exactly match a catalog name;
            # fall back to the requested category instead of inventing a match.
            result = await call_mcp_tool(
                self.mcp_server,
                "product_query",
                {"category": query_args["category"], "limit": 20},
            )
        answer = _format_products(result)
        return {
            **state,
            "current_agent": "product_agent",
            "rag_sources": [],
            "sub_results": {**state.get("sub_results", {}), "product_query": answer},
        }
