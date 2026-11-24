"""
FastAPI入口 — 提供REST API + SSE流式响应
"""

from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.supervisor import create_supervisor_graph
from memory.working_memory import WorkingMemory
from memory.short_term import ShortTermMemory
from memory.long_term import LongTermMemory
from memory.sql_store import SQLStore
from mcp.mcp_server import MCPToolServer, create_default_tools

load_dotenv()
# Disable LangSmith/LangChain auto tracing as well as the removed OpenTelemetry exporter.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"


def _redis_url_from_env() -> str:
    explicit_url = os.getenv("REDIS_URL", "").strip()
    if explicit_url:
        return explicit_url

    host = os.getenv("REDIS_HOST", "localhost").strip()
    port = os.getenv("REDIS_PORT", "6379").strip()
    database = os.getenv("REDIS_DB", "0").strip().lstrip("/") or "0"
    username = os.getenv("REDIS_USERNAME", "").strip()
    password = os.getenv("REDIS_PASSWORD", "")
    if password and username:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    elif password:
        auth = f":{quote(password, safe='')}@"
    elif username:
        auth = f"{quote(username, safe='')}@"
    else:
        auth = ""
    return f"redis://{auth}{host}:{port}/{database}"


working_memory = WorkingMemory()
short_term_memory = ShortTermMemory(redis_url=_redis_url_from_env())
sql_store = SQLStore()
rag_database_url = os.getenv("RAG_DATABASE_URL")
if not rag_database_url and sql_store.database_url.startswith("mysql"):
    rag_database_url = sql_store.database_url
long_term_memory = LongTermMemory(
    index_path=os.getenv("FAISS_INDEX_PATH", "./data/faiss"),
    db_path=os.getenv("RAG_DB_PATH", "./data/rag.sqlite3"),
    database_url=rag_database_url,
)
mcp_server = create_default_tools(
    MCPToolServer(), long_term_memory=long_term_memory, sql_store=sql_store
)
graph = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global graph

    graph = create_supervisor_graph(
        working_memory=working_memory,
        short_term_memory=short_term_memory,
        long_term_memory=long_term_memory,
        sql_store=sql_store,
        mcp_server=mcp_server,
        # The API hydrates each request from Redis/SQL, so an in-process
        # checkpointer would duplicate messages and lose state on restart.
        enable_checkpointing=False,
    )

    if not long_term_memory.list_knowledge_bases():
        long_term_memory.create_knowledge_base("默认知识库", "上传客服政策与产品文档后即可进行 RAG 问答")

    reference_pdfs = Path(__file__).resolve().parents[1] / "data" / "reference_pdfs"
    if reference_pdfs.exists() and any(reference_pdfs.rglob("*.pdf")):
        kb_name = "RAGEval 金融法规示例文档"
        reference_kb = next((item for item in long_term_memory.list_knowledge_bases() if item["name"] == kb_name), None)
        if reference_kb is None:
            reference_kb = long_term_memory.create_knowledge_base(
                kb_name, "从 qiugongisme/RAGEval-Chat 的 data/ 目录导入的法规与监管政策 PDF"
            )
        import_key = "rageval_chat_reference_pdfs_v1"
        if not long_term_memory.has_import(import_key, reference_kb["id"]):
            imported = long_term_memory.load_pdf_directory(reference_pdfs, reference_kb["id"])
            long_term_memory.mark_import(import_key, reference_kb["id"])
            print(f"Imported {imported} RAGEval-Chat reference PDFs into FAISS")

    yield


app = FastAPI(
    title="智能客服多Agent系统",
    description="基于LangGraph的Supervisor编排多Agent智能客服系统",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    user_id: str = "anonymous"
    session_id: str | None = None
    kb_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    compliance_passed: bool
    sources: list[dict] = []


class KnowledgeBaseRequest(BaseModel):
    name: str
    description: str = ""


def require_admin_key(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    expected = os.getenv("ADMIN_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="知识库管理未启用：请在服务端配置 ADMIN_API_KEY")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, expected):
        raise HTTPException(status_code=403, detail="需要管理员凭据")


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """主聊天接口"""
    if graph is None:
        raise HTTPException(status_code=503, detail="系统初始化中")

    session_id = request.session_id or str(uuid.uuid4())

    from langchain_core.messages import AIMessage, HumanMessage

    sql_history = sql_store.get_messages(session_id, limit=short_term_memory.max_messages)
    redis_history = await short_term_memory.get_history(
        session_id, last_n=short_term_memory.max_messages
    )
    if not redis_history:
        prior_history = sql_history
        if prior_history:
            await short_term_memory.replace_history(session_id, prior_history)
    elif sql_history and (
        len(redis_history) < len(sql_history)
        or redis_history[-1].get("role") != sql_history[-1].get("role")
        or redis_history[-1].get("content") != sql_history[-1].get("content")
    ):
        # SQL is the durable source of truth if Redis has a stale tail.
        prior_history = sql_history
        await short_term_memory.replace_history(session_id, prior_history)
    else:
        prior_history = redis_history

    user_entry = sql_store.append_message(
        session_id, request.user_id, "user", request.message
    )
    await short_term_memory.add_message(session_id, "user", request.message)

    chat_messages = []
    for item in [*prior_history, user_entry]:
        if item.get("role") == "assistant":
            chat_messages.append(AIMessage(content=item.get("content", "")))
        elif item.get("role") == "user":
            chat_messages.append(HumanMessage(content=item.get("content", "")))

    initial_state = {
        "messages": chat_messages,
        "user_id": request.user_id,
        "session_id": session_id,
        "kb_id": request.kb_id,
        "rag_sources": [],
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
    }

    config = {"configurable": {"thread_id": session_id}}

    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")

    final_response = result.get("final_response", "系统处理异常，请稍后重试")

    sql_store.append_message(session_id, request.user_id, "assistant", final_response)
    await short_term_memory.add_message(session_id, "assistant", final_response)

    return ChatResponse(
        response=final_response,
        session_id=session_id,
        intent=result.get("intent", "unknown"),
        compliance_passed=result.get("compliance_passed", True),
        sources=result.get("rag_sources", []),
    )


@app.get("/api/history/{session_id}")
async def get_history(session_id: str):
    """获取SQL中保存的完整对话历史；旧会话兼容Redis缓存。"""
    history = sql_store.get_messages(session_id)
    if not history:
        history = await short_term_memory.get_history(session_id)
    return {"session_id": session_id, "messages": history}


@app.get("/api/health/storage")
async def storage_health():
    """Expose whether durable SQL and Redis short-term memory are reachable."""
    try:
        sql_store.ping()
        sql_status = "connected"
    except Exception as exc:
        sql_status = f"unavailable ({type(exc).__name__})"

    try:
        long_term_memory.list_knowledge_bases()
        rag_sql_status = "connected"
    except Exception as exc:
        rag_sql_status = f"unavailable ({type(exc).__name__})"

    redis_ok = await short_term_memory.health_check()
    redis_status = "connected" if redis_ok else "unavailable; SQL history remains enabled"
    return {
        "status": "healthy" if sql_status == "connected" and rag_sql_status == "connected" and redis_ok else "degraded",
        "sql": sql_status,
        "rag_sql": rag_sql_status,
        "redis": redis_status,
        "conversation_history": "SQL",
        "ticket_storage": "SQL",
        "short_term_memory": (
            "Redis recent-context cache" if redis_ok else "SQL history restore; Redis cache offline"
        ),
    }


@app.get("/api/tools")
async def list_tools(_: None = Depends(require_admin_key)):
    """MCP工具发现接口"""
    return {"tools": mcp_server.list_tools()}


@app.post("/api/tools/call")
async def call_tool(request: dict, _: None = Depends(require_admin_key)):
    """MCP工具调用接口"""
    result = await mcp_server.call_tool(
        name=request.get("name", ""),
        arguments=request.get("arguments", {}),
    )
    return {
        "success": result.success,
        "result": result.result,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


@app.get("/api/knowledge-bases")
async def list_knowledge_bases(_: None = Depends(require_admin_key)):
    return {"items": long_term_memory.list_knowledge_bases()}


@app.post("/api/knowledge-bases")
async def create_knowledge_base(request: KnowledgeBaseRequest, _: None = Depends(require_admin_key)):
    try:
        return long_term_memory.create_knowledge_base(request.name, request.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(status_code=409, detail="知识库名称已存在")
        raise


@app.delete("/api/knowledge-bases/{kb_id}")
async def delete_knowledge_base(kb_id: str, _: None = Depends(require_admin_key)):
    if not long_term_memory.delete_knowledge_base(kb_id):
        raise HTTPException(status_code=404, detail="知识库不存在")
    return {"deleted": True}


@app.get("/api/knowledge-bases/{kb_id}/documents")
async def list_documents(kb_id: str, _: None = Depends(require_admin_key)):
    if not any(kb["id"] == kb_id for kb in long_term_memory.list_knowledge_bases()):
        raise HTTPException(status_code=404, detail="知识库不存在")
    return {"items": long_term_memory.list_documents(kb_id)}


def _extract_document(filename: str, payload: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".json", ".html"}:
        return payload.decode("utf-8-sig")
    if suffix == ".pdf":
        from pypdf import PdfReader
        import io
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(payload)).pages)
    if suffix == ".docx":
        from docx import Document
        import io
        return "\n".join(paragraph.text for paragraph in Document(io.BytesIO(payload)).paragraphs)
    raise ValueError("支持的格式：TXT、Markdown、CSV、JSON、HTML、PDF、DOCX")


@app.post("/api/knowledge-bases/{kb_id}/documents")
async def upload_document(kb_id: str, file: UploadFile = File(...), _: None = Depends(require_admin_key)):
    if not any(kb["id"] == kb_id for kb in long_term_memory.list_knowledge_bases()):
        raise HTTPException(status_code=404, detail="知识库不存在")
    payload = await file.read()
    if len(payload) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="单个文件不能超过 15 MB")
    try:
        content = _extract_document(file.filename or "document.txt", payload)
        if not content.strip():
            raise ValueError("文件中没有可读取的文本")
        doc_id = long_term_memory.add_document(content, file.filename or "document", kb_id=kb_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"文档解析失败：{exc}")
    return {"id": doc_id, "name": file.filename, "chunks": len(long_term_memory._chunk_text(content))}


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str, _: None = Depends(require_admin_key)):
    if not long_term_memory.delete_document(document_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"deleted": True}


@app.get("/api/knowledge-bases/{kb_id}/search")
async def search_knowledge_base(kb_id: str, q: str, top_k: int = 5, _: None = Depends(require_admin_key)):
    return {"items": long_term_memory.search(q, top_k=max(1, min(top_k, 20)), kb_id=kb_id)}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


WEB_DIR = Path(__file__).resolve().parents[1] / "web"
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


@app.get("/")
async def frontend():
    return FileResponse(WEB_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
