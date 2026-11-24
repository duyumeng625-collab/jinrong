"""
Supervisor编排Agent — 中央协调者
负责接收用户请求，根据意图路由到对应子Agent，汇总结果返回。
采用LangGraph StateGraph实现，支持并行调度和Human-in-the-Loop断点。
"""

from __future__ import annotations
import os
import re
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver

from agents.compliance_checker import ComplianceCheckerAgent
from agents.intent_rules import is_refund_application_request, is_refund_order_followup
from agents.knowledge_rag import KnowledgeRAGAgent
from agents.order_agent import OrderAgent
from agents.product_agent import ProductAgent
from agents.ticket_handler import TicketHandlerAgent
from memory.working_memory import WorkingMemory
from memory.short_term import ShortTermMemory
from memory.long_term import LongTermMemory
from memory.sql_store import SQLStore
from mcp.mcp_server import MCPToolServer, create_default_tools
from tracing.otel_config import trace_agent_call


# ─── 状态定义 ───

class AgentState(TypedDict):
    """Supervisor编排的全局状态"""
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    session_id: str
    kb_id: str | None
    rag_sources: list[dict[str, Any]]
    intent: str
    sub_results: dict[str, Any]
    compliance_passed: bool
    final_response: str
    current_agent: str
    retry_count: int


# ─── Supervisor节点 ───

SUPERVISOR_SYSTEM_PROMPT = """你是一个智能客服系统的Supervisor（主管编排Agent）。
你的职责是：
1. 分析用户意图，决定分发给哪个子Agent处理
2. 汇总子Agent的处理结果，生成最终回复
3. 金融业务回复进入合规审查；普通闲聊直接回答

可用路由：
- product_query: 查询产品目录中的具体产品名称、类别、起购金额、期限、风险等级和已登记利率信息
- knowledge_rag: 查询金融监管政策、合同条款、办理规则和需要文档依据的问题
- order_query: 查询订单号、订单状态、金额或物流信息
- ticket_handler: 创建或查询工单、安全事件和投诉
- general_chat: 问候、生活话题和其他普通闲聊

用户明确提出要退款、退钱或退费时，走 ticket_handler；询问退款流程、政策、条件或能否退款时，走 knowledge_rag。
用户问某个产品当前登记的具体信息时走 product_query；问监管政策、合同解释或业务办理规则时走 knowledge_rag。

日常生活问题必须路由到 general_chat，不能因为金融知识库中没有资料而路由到 knowledge_rag。
只输出一个路由名称，不要解释。
"""

GREETING_MESSAGES = {"你好", "你好呀", "您好", "您好呀", "嗨", "哈喽", "hello", "hi", "hey", "早上好", "上午好", "下午好", "晚上好", "在吗"}
THANKS_MESSAGES = {"谢谢", "谢谢你", "多谢", "辛苦了", "thanks", "thankyou"}
GOODBYE_MESSAGES = {"再见", "拜拜", "回见", "bye", "goodbye"}


class SupervisorNode:
    """Supervisor决策节点"""

    def __init__(self, llm: ChatOpenAI, working_memory: WorkingMemory):
        self.llm = llm
        self.working_memory = working_memory

    @trace_agent_call("supervisor")
    async def route_decision(self, state: AgentState) -> AgentState:
        """分析用户意图，决定路由"""
        messages = state["messages"]
        session_id = state.get("session_id", "default")

        latest_text = str(messages[-1].content) if messages else ""
        normalized = re.sub(r"[\s，。！？!?、,.~～]+", "", latest_text.lower())
        if normalized in GREETING_MESSAGES | THANKS_MESSAGES | GOODBYE_MESSAGES:
            self.working_memory.update(session_id, {"last_intent": "small_talk"})
            return {**state, "intent": "small_talk", "current_agent": "supervisor"}

        # A direct refund request is an application action, not a question for
        # the policy knowledge base. Route it consistently without relying on
        # an LLM classification guess.
        if is_refund_application_request(latest_text) or is_refund_order_followup(messages):
            self.working_memory.update(session_id, {"last_intent": "ticket_handler"})
            return {**state, "intent": "ticket_handler", "current_agent": "supervisor"}

        context = self.working_memory.get_context(session_id)

        routing_prompt = [
            SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
            SystemMessage(content=f"当前工作记忆上下文: {context}"),
            *messages,
            HumanMessage(content=(
                "请按用户最新消息的实际意图路由。具体产品目录字段、产品编号、风险等级、期限和已登记利率走 product_query；"
                "金融监管政策、合同条款、产品规则和业务流程走 knowledge_rag；"
                "订单状态、订单金额或物流查询走 order_query；"
                "用户明确要退款、退钱或退费走 ticket_handler；"
                "明确创建或查询工单，以及账户安全事件或投诉走 ticket_handler；"
                "问候、饮食、出行、娱乐、日常建议和普通知识走 general_chat。"
                "只返回以下之一: product_query, knowledge_rag, order_query, ticket_handler, general_chat"
            )),
        ]

        response = await self.llm.ainvoke(routing_prompt)
        valid_intents = {"product_query", "knowledge_rag", "order_query", "ticket_handler", "general_chat"}
        response_tokens = re.findall(r"[a-z_]+", str(response.content).lower())
        intent = next((token for token in response_tokens if token in valid_intents), "")
        if intent not in valid_intents:
            intent = "knowledge_rag"

        self.working_memory.update(session_id, {"last_intent": intent})

        return {
            **state,
            "intent": intent,
            "current_agent": "supervisor",
        }

    @trace_agent_call("supervisor_synthesize")
    async def synthesize_response(self, state: AgentState) -> AgentState:
        """汇总子Agent结果，生成最终回复"""
        sub_results = state.get("sub_results", {})
        compliance_passed = state.get("compliance_passed", True)

        if not compliance_passed:
            final_response = (
                "抱歉，您的请求涉及敏感内容，已转交人工客服处理。"
                "工单编号已自动生成，请留意后续通知。"
            )
        else:
            result_parts = []
            for result in sub_results.values():
                # Compliance stores structured audit metadata beside Agent replies.
                # Only user-facing text belongs in the final response.
                if isinstance(result, str) and result.strip():
                    result_parts.append(result)
            final_response = "\n\n".join(result_parts) if result_parts else "抱歉，暂时无法处理您的请求，请稍后重试。"

        return {
            **state,
            "final_response": final_response,
            "messages": [AIMessage(content=final_response)],
        }


async def handle_small_talk(state: AgentState) -> AgentState:
    """Respond to pure greetings/thanks without invoking RAG or an LLM."""
    messages = state.get("messages", [])
    text = str(messages[-1].content) if messages else ""
    normalized = re.sub(r"[\s，。！？!?、,.~～]+", "", text.lower())
    if normalized in THANKS_MESSAGES:
        reply = "不客气！如果还有业务问题，随时告诉我。"
    elif normalized in GOODBYE_MESSAGES:
        reply = "再见，祝你生活愉快！"
    else:
        reply = "你好！我是智能客服助手，可以帮你查询业务信息或提交工单。"
    return {
        **state,
        "current_agent": "small_talk",
        "sub_results": {**state.get("sub_results", {}), "small_talk": reply},
    }


class GeneralChatAgent:
    """Answer everyday topics directly without attaching knowledge-base results."""

    def __init__(self, llm: ChatOpenAI):
        self.llm = llm

    @trace_agent_call("general_chat")
    async def process(self, state: AgentState) -> AgentState:
        messages = state.get("messages", [])
        response = await self.llm.ainvoke([
            SystemMessage(content=(
                "你是友好、自然的中文聊天助手，可以回答生活、饮食、出行、娱乐和普通知识问题。"
                "直接回应用户，不要提及知识库、金融 PDF 或虚构引用。"
                "如果用户转而询问具体金融产品、账户、利率或业务条款，不要猜测；提示可以查询客服资料。"
            )),
            *messages,
        ])
        answer = str(response.content).strip()
        return {
            **state,
            "current_agent": "general_chat",
            "rag_sources": [],
            "sub_results": {
                **state.get("sub_results", {}),
                "general_chat": answer or "我暂时没组织好回答，可以换个方式问我吗？",
            },
        }


# ─── 路由函数 ───

def route_to_agent(state: AgentState) -> str:
    """根据意图路由到对应Agent节点"""
    intent = state.get("intent", "knowledge_rag")
    route_map = {
        "product_query": "product_agent",
        "knowledge_rag": "knowledge_rag",
        "order_query": "order_agent",
        "ticket_handler": "ticket_handler",
        "small_talk": "small_talk",
        "general_chat": "general_chat",
    }
    return route_map.get(intent, "knowledge_rag")


def should_check_compliance(state: AgentState) -> str:
    """所有回复都需经过合规审查"""
    return "compliance_check"


# ─── 构建Graph ───

def create_supervisor_graph(
    llm: ChatOpenAI | None = None,
    working_memory: WorkingMemory | None = None,
    short_term_memory: ShortTermMemory | None = None,
    long_term_memory: LongTermMemory | None = None,
    enable_checkpointing: bool = True,
    sql_store: SQLStore | None = None,
    mcp_server: MCPToolServer | None = None,
) -> StateGraph:
    """
    构建Supervisor编排的多Agent StateGraph。

    这是整个系统的核心入口：Supervisor负责路由，4个业务Agent分别调用
    各自的MCP工具，通用对话独立处理，业务回复经合规审查后汇总。

    Args:
        llm: 语言模型实例
        working_memory: 工作记忆
        short_term_memory: 短期记忆
        long_term_memory: 长期记忆
        sql_store: 工单与完整会话历史的SQL存储
        enable_checkpointing: 是否启用检查点（支持断点恢复）
    """
    if llm is None:
        # llm = ChatOpenAI(model="gpt-4o", temperature=0)
        llm = ChatOpenAI(
            model=os.getenv("MODEL_NAME", "deepseek-flash"),
            temperature=0,
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com"),
        )
    if working_memory is None:
        working_memory = WorkingMemory()
    if long_term_memory is None:
        long_term_memory = LongTermMemory()
    if sql_store is None:
        sql_store = SQLStore()
    if mcp_server is None:
        mcp_server = create_default_tools(
            MCPToolServer(), long_term_memory=long_term_memory, sql_store=sql_store
        )

    supervisor = SupervisorNode(llm, working_memory)
    product_agent = ProductAgent(mcp_server)
    knowledge_agent = KnowledgeRAGAgent(llm, mcp_server)
    order_agent = OrderAgent(mcp_server)
    ticket_agent = TicketHandlerAgent(llm, mcp_server)
    general_chat_agent = GeneralChatAgent(llm)
    compliance_agent = ComplianceCheckerAgent(llm)

    graph = StateGraph(AgentState)

    graph.add_node("supervisor_route", supervisor.route_decision)
    graph.add_node("product_agent", product_agent.process)
    graph.add_node("knowledge_rag", knowledge_agent.process)
    graph.add_node("order_agent", order_agent.process)
    graph.add_node("ticket_handler", ticket_agent.process)
    graph.add_node("small_talk", handle_small_talk)
    graph.add_node("general_chat", general_chat_agent.process)
    graph.add_node("compliance_check", compliance_agent.process)
    graph.add_node("synthesize", supervisor.synthesize_response)

    graph.set_entry_point("supervisor_route")

    graph.add_conditional_edges(
        "supervisor_route",
        route_to_agent,
        {
            "product_agent": "product_agent",
            "knowledge_rag": "knowledge_rag",
            "order_agent": "order_agent",
            "ticket_handler": "ticket_handler",
            "small_talk": "small_talk",
            "general_chat": "general_chat",
        },
    )

    graph.add_edge("product_agent", "compliance_check")
    graph.add_edge("knowledge_rag", "compliance_check")
    graph.add_edge("order_agent", "compliance_check")
    graph.add_edge("ticket_handler", "compliance_check")
    # Fixed, non-sensitive greetings need no retrieval, generation, or LLM review.
    graph.add_edge("small_talk", "synthesize")
    graph.add_edge("general_chat", "synthesize")
    graph.add_edge("compliance_check", "synthesize")
    graph.add_edge("synthesize", END)

    checkpointer = MemorySaver() if enable_checkpointing else None
    compiled = graph.compile(checkpointer=checkpointer)

    return compiled
