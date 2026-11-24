# 部署指南

## 1. 本地开发环境

### Python 版本（含内置 Web 前端）

```bash
cd python-impl
```

Windows PowerShell (仓库已有 `.venv` 时)：

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 仅当 .env 不存在时复制模板，随后填写模型 API 配置
..\.venv\Scripts\python.exe -m api.main
```

访问 `http://localhost:8000` 使用客户聊天前端。首次启动会把 `python-impl/data/reference_pdfs/` 中的 PDF 导入独立知识库；原件不作为前端静态文件提供。SQLite 保存元数据，FAISS 索引写入 `FAISS_INDEX_PATH` 指定目录。

知识库管理和 MCP 工具接口要求服务端设置 `ADMIN_API_KEY`，请求携带 `X-Admin-Key`。未设置密钥时这些接口会拒绝请求。客户页面不包含知识库上传、删除或文档列表。

### Java版本

```bash
cd java-impl
mvn clean package -DskipTests
java -jar target/smart-cs-agent-1.0.0.jar
```

访问: http://localhost:8080/api/health

### Go版本

```bash
cd go-impl
go mod tidy
go run main.go
```

访问: http://localhost:8090/health

## 2. Docker部署

### 单服务启动

```bash
# Python
cd python-impl
docker build -t smart-cs-python .
docker run -p 8000:8000 --env-file .env smart-cs-python

# Java
cd java-impl
mvn clean package -DskipTests
docker build -t smart-cs-java .
docker run -p 8080:8080 -e OPENAI_API_KEY=xxx smart-cs-java

# Go
cd go-impl
docker build -t smart-cs-go .
docker run -p 8090:8090 smart-cs-go
```

### Docker Compose 一键启动

```bash
docker-compose up -d
```

## 3. API接口说明

所有三个版本提供统一的REST API：

### POST /api/chat — 聊天接口

```json
// Request
{
  "message": "我想了解一下理财产品A",
  "user_id": "user_001",
  "session_id": "optional-session-id"
}

// Response
{
  "response": "关于理财产品A...",
  "session_id": "xxx",
  "intent": "knowledge_rag",
  "compliance_passed": true
}
```

### GET /api/history/{session_id} — 对话历史

### GET /api/tools — MCP工具列表

客户聊天由 ProductAgent、OrderAgent、KnowledgeRAGAgent 和 TicketHandlerAgent 分别通过 MCP `tools/call` 调用 SQL 产品目录、SQL订单、FAISS知识库或 SQL 工单工具。MCP Server 是工具协议层，不是 Agent。产品表 `products` 会在应用启动时创建，正式产品数据需经审核后导入；DDL 见 `python-impl/data/product_catalog.sql`。日常话题不调用 RAG/MCP。`/api/tools` 与 `/api/tools/call` 是管理员手动发现/调试工具的接口，仍需 `ADMIN_API_KEY`。

### POST /api/tools/call — 管理员手动调用 MCP工具

### GET /health — 健康检查

## 4. 环境变量说明

| 变量 | 说明 | 默认值 |
|------|------|--------|
| OPENAI_API_KEY | LLM API密钥 | 无 |
| OPENAI_BASE_URL | API端点 | https://api.openai.com/v1 |
| MODEL_NAME | 模型名称 | gpt-4o |
| REDIS_URL | Redis地址 | redis://localhost:6379/0 |
| RAG_DB_PATH | SQLite RAG 数据库文件 | ./data/rag.sqlite3 |
| FAISS_INDEX_PATH | FAISS 索引目录 | ./data/faiss |
| ADMIN_API_KEY | 内部知识库/MCP 管理密钥 | 未设置时管理接口关闭 |

系统不采集 OpenTelemetry、Jaeger、LangSmith 或 LangChain 全链路追踪。
