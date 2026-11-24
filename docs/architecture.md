# 架构设计文档

## 1. 系统总体架构

### 1.1 架构概览

本系统采用 **Supervisor编排模式**（中心化协调）：Supervisor 负责路由，产品、知识库、订单和工单 Agent 各自处理业务，通用对话独立回答；MCP Server 只提供工具协议，不是 Agent。

```
                          ┌─────────────────────────┐
                          │     用户 (Web/App/API)    │
                          └────────────┬────────────┘
                                       │ HTTP/SSE
                                       ▼
                          ┌─────────────────────────┐
                          │   API Gateway (FastAPI)   │
                          │   认证 | 限流 | 日志       │
                          └────────────┬────────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                   │
                    ▼                  ▼                   ▼
            ┌──────────┐     ┌──────────────┐    ┌──────────────┐
            │ 短期记忆   │     │  Supervisor  │    │ 全链路追踪    │
            │  (Redis)  │◄───►│   编排Agent   │    │  不采集调用链  │
            └──────────┘     └──────┬───────┘    └──────────────┘
                                    │
                          ▼
       ┌────────────┬────────────┬────────────┬────────────┐
       ▼            ▼            ▼            ▼
 ProductAgent  KnowledgeRAG  OrderAgent  TicketHandler
       │            │            │            │
       └────────────┴──────┬─────┴────────────┘
                           │ JSON-RPC tools/call
                           ▼
                     ┌────────────┐
                     │ MCP Server │
                     └─┬──┬──┬──┬─┘
                       │  │  │  └── ticket_create / ticket_query → SQL
                       │  │  └───── order_query → SQL
                       │  └──────── knowledge_search → FAISS/RRF
                       └─────────── product_query → SQL
                           │
                           ▼
                    合规审查 → 汇总
```

### 1.2 编排流程（Supervisor Pattern）

```
用户请求
    │
    ▼
[Supervisor: 识别意图并路由]
    ├── product_query ──► ProductAgent ───────────────┐
    ├── knowledge_rag ──► KnowledgeRAGAgent ─────────┤
    ├── order_query ────► OrderAgent ────────────────┤
    ├── ticket_handler ─► TicketHandlerAgent ────────┤
    ├── small_talk ─────► 固定问候回复 ──────────────┤──► Supervisor汇总
    └── general_chat ───► 通用聊天Agent ────────────┤
                                                      │
        四个业务Agent ──► 合规审查 ──────────────────┘
```

## 2. 核心组件设计

### 2.1 Agent设计原则

每个Agent遵循**单一职责原则**：

| Agent | 职责 | 输入 | 输出 |
|-------|------|------|------|
| Supervisor | 编排调度、结果汇总 | 用户消息 + 全局State | 路由决策 + 最终回复 |
| ProductAgent | 产品目录查询 | 产品名/编号/类别 | SQL产品信息 |
| KnowledgeRAGAgent | 政策、条款和规则问答 | 用户问题 | 基于 FAISS 文档的回答和来源 |
| OrderAgent | 查询当前用户订单 | 订单号或查询请求 | SQL订单信息 |
| TicketHandlerAgent | 工单创建和查询 | 用户需求/工单号/订单号 | 工单号、状态或列表 |
| 通用对话 | 问候和日常话题 | 用户消息 | 直接回复 |
| 合规审查 | 内容审查 | Agent回复内容 | 通过/不通过 + 违规项 |

### 2.2 State设计

```python
class AgentState(TypedDict):
    messages: list[BaseMessage]      # 对话消息列表
    user_id: str                      # 用户标识
    session_id: str                   # 会话标识
    intent: str                       # 识别到的意图
    sub_results: dict[str, Any]       # 各Agent的处理结果
    compliance_passed: bool           # 合规审查是否通过
    final_response: str               # 最终回复
    current_agent: str                # 当前执行的Agent
    retry_count: int                  # 重试次数
```

### 2.3 分层记忆架构

```
┌──────────────────────────────────────────────┐
│                  应用层                       │
├──────────────────────────────────────────────┤
│  工作记忆 (Working Memory)                    │
│  ├── 存储: 进程内存 (dict)                    │
│  ├── 生命周期: 单次请求                       │
│  ├── 延迟: < 1ms                             │
│  └── 用途: 当前推理状态、路由决策上下文         │
├──────────────────────────────────────────────┤
│  短期记忆 (Short-term Memory)                │
│  ├── 存储: Redis                             │
│  ├── 生命周期: TTL 30分钟                     │
│  ├── 延迟: 1-5ms                             │
│  ├── 容量: 最近20轮对话                       │
│  └── 用途: 多轮对话上下文                     │
├──────────────────────────────────────────────┤
│  长期记忆 (Long-term Memory)                 │
│  ├── 存储: SQLite元数据 + FAISS向量索引       │
│  ├── 生命周期: 永久                           │
│  ├── 延迟: 10-50ms                           │
│  └── 用途: 知识库、用户画像、历史工单          │
└──────────────────────────────────────────────┘
```

## 3. RAG检索流程

```
用户原始问题: "怎么退钱啊"
        │
        ▼
[Query改写] ──► "退款 政策 申请 流程 时限"
        │
        ▼
[文本切分] ──► SQLite 文档元数据 + FAISS 特征向量索引
        │
        ▼
[混合检索] ──► FAISS 向量召回 + 稀疏词项召回 ──► RRF Top-K
        │
        ▼
[上下文注入] ──► System Prompt + 文档内容 + 用户问题
        │
        ▼
[LLM生成] ──► 基于文档的回答 + 引用标注
        │
        ▼
[合规审查] ──► 检查回复合规性
        │
        ▼
最终回答 + 引用来源
```

## 4. MCP工具协议

```
┌─────────────┐    JSON-RPC 2.0    ┌─────────────────┐
│   Agent      │ ◄────────────────► │  MCP Tool Server │
│              │                    │                  │
│ tools/call   │ ──── 调用 ─────►  │  ┌─product_query │
│              │ ◄─── 结果 ──────  │  ├─order_query   │
│              │                    │  ├─knowledge_search│
│              │                    │  ├─ticket_create │
│              │                    │  └─ticket_query  │
└─────────────┘                    └─────────────────┘
```

工具注册示例：
```json
{
  "name": "knowledge_search",
  "description": "检索实际客服金融知识库",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {"type": "string"},
      "top_k": {"type": "integer"}
    },
    "required": ["query"]
  }
}
```

业务 Agent 直接通过 JSON-RPC `tools/call` 调用自己负责的工具：产品 Agent 调 `product_query`，订单 Agent 调 `order_query`，知识 Agent 调 `knowledge_search`，工单 Agent 调 `ticket_create`/`ticket_query`。MCP Server 负责注册和执行工具，但不是一个 Agent 节点。产品目录表 `products` 保存经审核的结构化产品资料；监管文件和条款继续使用 FAISS/RRF。`tickets.order_id` 可关联 `orders.order_id`，退款工单创建时校验订单归属。风控接口尚无真实后端，因此当前不注册为聊天工具。

## 5. 请求诊断

本项目不创建或导出 Agent 调用 Span，也不连接 Jaeger、OpenTelemetry 或 LangSmith。聊天响应只返回最终答案与 RAG 引用片段。

## 6. 技术选型对比

### 编排框架对比

| 维度 | LangGraph (Python) | Spring AI (Java) | Eino (Go) |
|------|-------------------|------------------|-----------|
| 编排模型 | 有向图StateGraph | Agent组合模式 | Graph/Workflow |
| 状态管理 | TypedDict + Checkpoint | POJO | struct |
| 并行能力 | asyncio | CompletableFuture | goroutine |
| 生态 | LangSmith/LangServe | Spring生态 | CloudWeGo |
| 适合团队 | AI/数据团队 | 企业级Java团队 | Go微服务团队 |
| 生产成熟度 | 高 | 中高 | 中 |

### 向量数据库对比

| 维度 | FAISS | Milvus | Pinecone |
|------|-------|--------|----------|
| 部署方式 | 嵌入式 | 分布式 | SaaS |
| 数据规模 | 千万级 | 百亿级 | 百亿级 |
| 查询延迟 | < 1ms | ~10ms | ~50ms |
| 运维复杂度 | 低 | 中 | 零 |
| 成本 | 免费 | 中等 | 高 |
| 适合阶段 | 开发/小规模 | 生产 | 快速上线 |
