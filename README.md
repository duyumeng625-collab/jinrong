# 🤖 智能客服多Agent系统

> **企业级面试项目全攻略** — 面向金融/电商场景，包含 Python / Java / Go 三语言完整实现 + 配套面试材料，从零到面试一站搞定。

[![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)](https://www.python.org/)
[![Java](https://img.shields.io/badge/Java-17+-orange?logo=openjdk)](https://openjdk.org/)
[![Go](https://img.shields.io/badge/Go-1.22+-00ADD8?logo=go)](https://golang.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2+-green)](https://github.com/langchain-ai/langgraph)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

---

## 📋 目录

- [项目简介](#-项目简介)
- [系统架构](#-系统架构)
- [核心功能](#-核心功能)
- [技术栈](#-技术栈)
- [三语言实现对比](#-三语言实现对比)
- [快速开始](#-快速开始)
- [项目结构](#-项目结构)
- [核心代码解析](#-核心代码解析)
- [面试准备材料](#-面试准备材料)
- [参考项目](#-参考项目)
- [安全说明](#-安全说明)

---

## 🎯 项目简介

本项目是一个**企业级多Agent智能客服系统**，模拟真实金融/电商公司的客服场景。系统由多个专业AI Agent协同工作，自动处理用户咨询、工单创建、知识检索等任务。

**这个项目能帮你做什么？**

- ✅ **面试加分项**：拥有一个真实完整的多Agent项目，不再只是CRUD
- ✅ **三语言实现**：Python/Java/Go均有完整代码，适配不同岗位需求
- ✅ **面试材料齐全**：简历模板、STAR话术、八股文题库一应俱全
- ✅ **学习参考**：代码有详细注释，架构文档有图文说明

**适合人群：**
- 准备AI/后端岗位面试的同学
- 想了解多Agent系统架构的开发者
- 对LangGraph/Spring AI/Eino感兴趣的工程师

---

## 🏗️ 系统架构

### 整体架构图

```
用户 (Web/App/API)
        │  HTTP/SSE
        ▼
┌──────────────────────┐
│   API Gateway        │  ← 认证、限流、日志
│   (FastAPI / Spring) │
└──────────┬───────────┘
           │
           ▼
┌────────────────────────────────────┐
│       Supervisor 编排与意图路由      │
│       分层记忆：工作 / Redis / SQL   │
└───┬────────┬────────┬────────┬──────┘
    │        │        │        │
    ▼        ▼        ▼        ▼
产品 Agent 知识 Agent 订单 Agent 工单 Agent
    │        │        │        │
    └────────┴───┬────┴────────┘
                 │ JSON-RPC tools/call
                 ▼
        ┌─────────────────────┐
        │ MCP Server（工具层）  │
        │ 产品SQL / 订单SQL    │
        │ FAISS搜索 / 工单SQL  │
        └─────────────────────┘
                 │
                 ▼
        合规审查 → 汇总回复
```

### 请求处理流程

```
① 用户发送消息："查询一下某个产品 / 订单 / 退款政策"
        ↓
② Supervisor 分析意图 → 路由决策
        ↓
③ 路由到 ProductAgent、OrderAgent、KnowledgeRAGAgent 或 TicketHandlerAgent
        ↓
④ 对应业务 Agent 通过 MCP tools/call 调用自己的工具
        ↓
⑤ Agent 依据 SQL / FAISS 工具结果回答 → 合规审查
        ↓
⑥ Supervisor 汇总结果 → 返回最终回复
```

产品目录、订单和工单信息从 SQL 查询；监管政策、条款与办理规则通过 RAG 检索；日常问题由通用对话回答，不调用 MCP。

---

## ✨ 核心功能

### 1. Supervisor 编排模式
**什么是Supervisor？** 就像一个项目经理，接到需求后分配给不同专家处理，最后汇总结果。

| 特性 | 说明 |
|------|------|
| 中央协调 | 由Supervisor统一调度，子Agent只做专业工作 |
| 并行调度 | 多个Agent可同时工作，提升处理速度 |
| Human-in-the-Loop | 敏感问题自动暂停，等待人工确认 |
| 多轮会话恢复 | 按 session_id 从 Redis 取近期上下文，Redis 缺失时从 SQL 历史恢复 |

### 2. 分层记忆系统
**为什么需要三层记忆？** 类似人类记忆：工作桌(工作记忆) + 笔记本(短期) + 大脑长期记忆。

| 记忆层 | 存储位置 | 生命周期 | 延迟 | 用途 |
|--------|----------|----------|------|------|
| **工作记忆** | 进程内存 (dict) | 单次请求 | <1ms | 当前推理状态、路由决策上下文 |
| **短期记忆** | Redis | TTL 30分钟 | 1-5ms | 多轮对话上下文（保留最近20轮，按消息滑动刷新TTL） |
| **长期记忆** | SQLite/MySQL 元数据 + FAISS | 永久 | 本地向量召回 | 多知识库、文档和文本片段 |

工单和完整聊天记录保存在 SQL 中。退款工单通过 `tickets.order_id` 关联 `orders.order_id`，一个订单可对应多张工单；历史工单只有在文本中能唯一识别订单且订单属于同一用户时才会自动回填关联。默认使用 `python-impl/data/app.sqlite3`，也可通过 `APP_DATABASE_URL` 配置 MySQL；Redis 只承担近期会话上下文缓存。

### 3. MCP 工具协议
**什么是MCP？** Model Context Protocol，AI模型调用外部工具的标准协议，类似HTTP规范了Web通信。

业务 Agent 通过 MCP 协议调用的工具：
- `product_query` — 查询 SQL 产品目录中的在售产品信息
- `order_query` — 按当前用户查询订单状态、金额和物流
- `knowledge_search` — 查询实际 FAISS/RRF 知识库
- `ticket_create` — 将工单写入配置的 SQL 数据库
- `ticket_query` — 查询当前用户自己的 SQL 工单

Supervisor 负责意图路由，不作为 MCP 工具调用者，也没有单独的 MCPToolAgent 节点。ProductAgent、OrderAgent、KnowledgeRAGAgent 和 TicketHandlerAgent 各自通过 JSON-RPC `tools/call` 调用产品、订单、知识库和工单工具，并根据结构化结果回复。明确的退款申请会先要求订单号，再由 TicketHandlerAgent 调用 `ticket_create` 创建关联订单的工单；退款政策和流程问题仍走 RAG。工单可按工单号、订单号或当前用户查询。风控工具暂未接入真实系统，因此不会作为可调用工具提供。`/api/tools` 与 `/api/tools/call` 是受管理员密钥保护的管理/调试接口。

产品目录表为 `products`，应用初始化时会在 `APP_DATABASE_URL` 配置的数据库中自动创建。代码不会填造产品条款或收益数据；应将经审核的正式产品资料导入该表。MySQL DDL 见 `python-impl/data/product_catalog.sql`。

MySQL `jinrong.orders` 已加入 3 条标注为演示的虚构订单。可在聊天页试问“查我的最近订单”或“查询订单 ORD-DEMO-20261028-001”。

### 4. RAG 知识检索
**什么是RAG？** Retrieval-Augmented Generation，先从知识库检索相关内容，再让AI生成回答，避免AI"瞎编"。

```
用户问题: "怎么退款？"
    ↓ Supervisor 路由到 knowledge_rag
    ↓ KnowledgeRAGAgent 调用 MCP knowledge_search
    ↓ FAISS 向量召回 + 稀疏词项检索
    ↓ RRF 融合排序，返回Top-K片段
    ↓ 检索结果和问题交给 KnowledgeRAGAgent
    ↓ LLM生成（基于文档的准确回答）
最终回答 + 引用来源标注
```

### 5. RAG 知识库与引用
客户前端只提供聊天和回答引用；知识库管理 API 需要管理员密钥。SQL 保存知识库、文档与切片元数据，FAISS 持久化检索向量；检索融合 FAISS 向量召回与稀疏词项检索。默认使用 SQLite；配置 MySQL URL 后，RAG 元数据和工单、完整聊天记录可写入 MySQL。RAGEval-Chat 仓库提供的 42 份法规/监管政策 PDF 会自动导入独立知识库。系统不采集 OpenTelemetry、Jaeger、LangSmith 或 LangChain 全链路追踪。

### 6. 合规审查
专为金融场景设计：
- **敏感词检测**：自动识别违规词汇
- **PII保护**：过滤身份证、银行卡等隐私信息
- **越权访问治理**：防止用户绕过权限限制
- **双重审查**：规则引擎（快，<2ms）+ LLM审查（准，~600ms）

---

## 🛠️ 技术栈

| 层次 | 技术选型 | 说明 |
|------|----------|------|
| **AI框架** | LangGraph / Spring AI / Eino | 多Agent编排 |
| **LLM** | GPT-4o / Claude 3.5 | 大语言模型 |
| **持久化检索** | FAISS + SQLite + RRF | 本地向量召回、元数据和关键词融合 |
| **缓存** | Redis | 短期记忆、会话管理 |

| **API** | FastAPI / Spring Boot / Gin | REST接口 |
| **容器** | Docker + Docker Compose | 一键部署 |
| **协议** | MCP (Model Context Protocol) | 工具调用标准 |

---

## 🔀 三语言实现对比

| 维度 | Python (LangGraph) | Java (Spring AI) | Go (Eino) |
|------|-------------------|------------------|-----------|
| **目录** | [`python-impl/`](./python-impl/) | [`java-impl/`](./java-impl/) | [`go-impl/`](./go-impl/) |
| **编排框架** | LangGraph StateGraph | Spring AI Agent | Eino Graph/Workflow |
| **状态管理** | TypedDict + Redis/SQL记忆 | POJO | struct |
| **并行方式** | asyncio | CompletableFuture | goroutine |
| **生态** | LangSmith / LangServe | Spring 全家桶 | CloudWeGo |
| **适合场景** | AI原型、数据科学团队 | 企业级金融/银行 | 高并发云原生微服务 |
| **生产成熟度** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ |
| **面试亮点** | 最主流AI开发栈 | 大厂Java面试必考 | 字节/腾讯Go岗位 |

---

## 🚀 快速开始

本仓库当前提供 Python 实现及内置 Web 前端，Web 工作台由 FastAPI 一并提供。

### 前置条件
- 一个 OpenAI API Key（或其他LLM的Key）
- Docker（可选，用于一键启动）

### 方式一：Docker 启动 API

```bash
# 1. 克隆项目
git clone https://github.com/bcefghj/smart-cs-multi-agent.git
cd smart-cs-multi-agent

# 2. 配置API Key
cp python-impl/.env.example python-impl/.env
# 编辑 .env 文件，填入你的 OPENAI_API_KEY

# 3. 启动 API 容器；Redis 和 MySQL 连接宿主机上已有的服务
docker-compose up -d

# 4. 访问接口
# API文档: http://localhost:8000/docs
# 前端: http://localhost:8000
```

### 方式二：Python 版本（直接运行）

```powershell
# 在仓库根目录执行
cd python-impl
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 若没有 .env：Copy-Item .env.example .env；然后填写模型 API 配置
# 确认 .env 中 Redis 和 MySQL 的地址、端口、数据库和密码正确
..\.venv\Scripts\python.exe -m api.main
```

打开 `http://localhost:8000` 使用客户聊天前端。`python-impl/data/reference_pdfs/` 内的 42 份法规/政策 PDF 会在首次启动时导入 FAISS 知识库；客户页面不展示 PDF 原件或管理功能。未配置 MySQL 时，RAG 元数据位于 `python-impl/data/rag.sqlite3`，工单与完整聊天记录位于 `python-impl/data/app.sqlite3`；配置 `SQL_DB_HOST`、`SQL_DB_PORT`、`SQL_DB_USER`、`SQL_DB_PASSWORD`、`SQL_DB_NAME` 后，工单、完整聊天记录和 RAG 文档/切片元数据都使用 MySQL。也可以用 `APP_DATABASE_URL` 直接提供 MySQL URL，或用 `RAG_DATABASE_URL` 单独指定 RAG 数据库。FAISS 索引目录由 `FAISS_INDEX_PATH` 指定。Redis 使用 `REDIS_HOST`、`REDIS_PORT`、`REDIS_DB` 和 `REDIS_PASSWORD` 配置；访问 `http://localhost:8000/api/health/storage` 可查看 SQL 和 Redis 连接状态。如需内部管理文档，在服务端 `.env` 设置 `ADMIN_API_KEY`，然后用 `X-Admin-Key` 调用知识库管理 API。系统不需要 Milvus；全链路追踪已关闭。
### 方式三：Java 版本

```bash
cd java-impl

# 需要 Java 17+ 和 Maven 3.8+
mvn clean package -DskipTests

# 运行
java -jar target/smart-cs-agent-1.0.0.jar

# 或者直接用Maven运行
mvn spring-boot:run
```

### 方式四：Go 版本

```bash
cd go-impl

# 需要 Go 1.22+
go mod tidy

# 运行
go run main.go

# 或者编译后运行
go build -o smart-cs-agent .
./smart-cs-agent
```

---

## 📁 项目结构

```
smart-cs-multi-agent/
│
├── 📄 README.md                    ← 你现在看的这个文件
├── 📄 docker-compose.yml           ← 一键启动所有服务
├── 📄 LICENSE                      ← MIT开源协议
│
├── 📂 docs/                        ← 文档目录
│   ├── 📄 architecture.md          ← 架构设计文档（含流程图）
│   ├── 📄 code-walkthrough.md      ← 核心代码逐行解析
│   ├── 📄 deployment.md            ← 生产环境部署指南
│   └── 📂 interview/               ← 面试准备材料
│       ├── 📄 resume-template.md   ← 简历模板（STAR格式）
│       ├── 📄 star-method.md       ← 面试话术（含多场景）
│       ├── 📄 baguwen.md           ← 30+八股文题库+详细答案
│       └── 📄 project-qa.md        ← 20+项目深度追问+应对策略
│
├── 📂 python-impl/                 ← Python实现 (LangGraph + FastAPI)
│   ├── 📄 requirements.txt         ← Python依赖包
│   ├── 📄 Dockerfile
│   ├── 📂 agents/                  ← 核心Agent代码
│   │   ├── supervisor.py           ← Supervisor编排Agent（核心）
│   │   ├── intent_router.py        ← 意图识别Agent
│   │   ├── product_agent.py        ← SQL产品目录查询Agent
│   │   ├── order_agent.py          ← MCP订单查询Agent
│   │   ├── knowledge_rag.py        ← RAG知识检索Agent
│   │   ├── ticket_handler.py       ← 工单处理Agent
│   │   └── compliance_checker.py   ← 合规审查Agent
│   ├── 📂 memory/                  ← 三层记忆系统
│   ├── 📂 mcp/                     ← MCP工具协议实现
│   ├── 📂 tracing/                 ← OpenTelemetry追踪
│   └── 📂 api/                     ← FastAPI接口层
│
├── 📂 java-impl/                   ← Java实现 (Spring AI + Spring Boot)
│   ├── 📄 pom.xml                  ← Maven依赖配置
│   ├── 📄 Dockerfile
│   └── 📂 src/main/java/com/smartcs/
│
└── 📂 go-impl/                     ← Go实现 (Eino框架)
    ├── 📄 go.mod                   ← Go依赖管理
    ├── 📄 main.go                  ← 程序入口
    ├── 📄 Dockerfile
    ├── 📂 agent/                   ← Agent实现
    ├── 📂 memory/                  ← 记忆系统
    ├── 📂 mcp/                     ← MCP协议
    └── 📂 tracing/                 ← 链路追踪
```

---

## 💻 核心代码解析

### Supervisor 编排核心逻辑（Python）

这是整个系统最重要的部分，理解了这个代码，面试时就能讲清楚多Agent协作原理：

```python
# python-impl/agents/supervisor.py

# 1. 定义全局状态（类似"黑板"，所有Agent共享）
class AgentState(TypedDict):
    messages: list[BaseMessage]    # 对话历史
    user_id: str                   # 用户ID
    intent: str                    # 识别到的意图
    sub_results: dict[str, Any]    # 各Agent处理结果
    compliance_passed: bool        # 合规是否通过
    final_response: str            # 最终回复

# 2. 构建有向图（定义Agent之间的流转关系）
def create_supervisor_graph():
    graph = StateGraph(AgentState)
    
    # 添加节点（每个Agent是一个节点）
    graph.add_node("supervisor_route", supervisor.route_decision)
    graph.add_node("product_agent", product_agent.process)
    graph.add_node("knowledge_rag", knowledge_agent.process)
    graph.add_node("order_agent", order_agent.process)
    graph.add_node("ticket_handler", ticket_agent.process)
    graph.add_node("small_talk", handle_small_talk)
    graph.add_node("general_chat", general_chat_agent.process)
    graph.add_node("compliance_check", compliance_agent.process)
    graph.add_node("synthesize", supervisor.synthesize_response)
    
    # 设置入口
    graph.set_entry_point("supervisor_route")
    
    # 条件路由（根据意图决定走哪条路）
    graph.add_conditional_edges(
        "supervisor_route",
        route_to_agent,              # 路由函数
        {
            "product_agent": "product_agent",
            "knowledge_rag": "knowledge_rag",
            "order_agent": "order_agent",
            "ticket_handler": "ticket_handler",
            "small_talk": "small_talk",
            "general_chat": "general_chat",
        }
    )
    
    # 各业务Agent调用自己的MCP工具后进入合规审查
    graph.add_edge("product_agent", "compliance_check")
    graph.add_edge("knowledge_rag", "compliance_check")
    graph.add_edge("order_agent", "compliance_check")
    graph.add_edge("ticket_handler", "compliance_check")
    graph.add_edge("small_talk", "synthesize")
    graph.add_edge("general_chat", "synthesize")
    graph.add_edge("compliance_check", "synthesize")
    graph.add_edge("synthesize", END)
    
    return graph.compile(checkpointer=MemorySaver())
```

**面试时怎么讲这段代码？**
> "我们用LangGraph的StateGraph构建了一个有向图，Supervisor按意图把日常对话交给通用模型，把金融知识和工单操作交给对应流程。业务回答经过合规审查，日常对话不检索金融知识库。"

### MCP工具协议实现

```python
# python-impl/mcp/mcp_server.py

# MCP工具的核心：描述工具能力，让AI知道什么时候用这个工具
knowledge_search_tool = {
    "name": "knowledge_search",
    "description": "查询金融客服知识库中的政策和业务依据",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要检索的问题"},
            "top_k": {"type": "integer", "description": "返回片段数"}
        },
        "required": ["query"]
    }
}
```

---

## 📚 面试准备材料

配套完整面试资料，帮你从"能看懂代码"到"面试时能流畅讲清楚"：

| 文档 | 内容说明 | 链接 |
|------|----------|------|
| **简历模板** | STAR法则项目经历写法，覆盖Python/Java/Go不同岗位角度 | [查看](./docs/interview/resume-template.md) |
| **STAR面试话术** | "请介绍你的项目"等高频问题的标准回答模板 | [查看](./docs/interview/star-method.md) |
| **八股文题库** | 30+高频面试题 + 详细答案 + 追问应对策略 | [查看](./docs/interview/baguwen.md) |
| **项目深度追问** | 面试官最爱问的20+深度问题 + 踩坑分享 | [查看](./docs/interview/project-qa.md) |
| **架构设计文档** | 完整流程图、时序图、技术选型对比分析 | [查看](./docs/architecture.md) |
| **代码讲解文档** | 核心模块逐行解析，设计模式说明 | [查看](./docs/code-walkthrough.md) |
| **部署指南** | Docker一键部署、生产环境配置、监控告警 | [查看](./docs/deployment.md) |

### 常见面试问题预览

**Q: 为什么用Supervisor模式而不是让Agent直接互相调用？**
> A: Supervisor模式的优势在于集中控制，便于追踪和调试；避免Agent之间形成循环依赖；Supervisor可以做全局优化，比如并行调度多个Agent；出错时有统一的错误处理和回退机制。

**Q: 三层记忆的设计原则是什么？**
> A: 工作记忆维护当前请求的路由和中间结果；短期记忆用Redis保存最近20轮对话并设置30分钟滑动TTL；完整对话历史和工单保存在SQL，知识文档元数据保存在SQLite，FAISS负责向量检索。

**Q: MCP协议相比直接调用函数有什么优势？**
> A: 工具用统一的名称、描述和JSON Schema注册，模型据此生成工具调用，应用再经JSON-RPC分派给handler。具体权限和参数校验仍由应用实现；本项目把知识库搜索和SQL工单操作接入了聊天流程。

---

## 📖 参考项目

本项目设计参考了以下企业级开源项目，建议结合阅读：

| 项目 | Stars | 参考内容 |
|------|-------|----------|
| [AWS Agent Squad](https://github.com/awslabs/agent-squad) | 7,500+ | 智能意图分类 + SupervisorAgent 设计 |
| [LangGraph Supervisor](https://github.com/langchain-ai/langgraph-supervisor-py) | — | Supervisor模式预构建库，官方最佳实践 |
| [Spring AI Alibaba](https://github.com/alibaba/spring-ai-alibaba) | 9,000+ | Java多Agent编排，阿里巴巴生产实践 |
| [Eino (CloudWeGo)](https://github.com/cloudwego/eino) | 10,300+ | 字节跳动Go企业级Agent框架 |
| [Multi-Agent Enterprise CRM](https://github.com/Mrgig7/Multi-Agent-Enterprise-CRM) | — | LangGraph + Kafka 生产级客服方案 |

---

## 🔒 安全说明

- 本项目**不包含任何真实的API Key、Token或密码**
- 所有敏感配置通过环境变量注入（见 `.env.example`）
- `.env.example` 仅提供占位符示例，**不要直接使用**
- 请勿将含有真实凭据的 `.env` 文件提交到版本控制

---

## 📄 License

[MIT License](./LICENSE) — 自由使用、修改、分发，保留原始版权声明即可。

---

<div align="center">

**如果这个项目对你有帮助，欢迎 ⭐ Star 支持一下！**

</div>
