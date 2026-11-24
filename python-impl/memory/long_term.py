"""Persistent knowledge bases with FAISS vector search and sparse RRF retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import faiss
import numpy as np
from sqlalchemy.engine import make_url


class _MySQLRow(dict):
    """Dict-like row that also supports the positional access used by SQLite rows."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _MySQLResult:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return _MySQLRow(row) if row is not None else None

    def fetchall(self):
        return [_MySQLRow(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                break
            yield row


class _MySQLConnection:
    """Small DB-API adapter for the existing SQLite-style query calls."""

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()

    @staticmethod
    def _mysql_sql(statement: str) -> str:
        statement = re.sub(r"INSERT\s+OR\s+IGNORE", "INSERT IGNORE", statement, flags=re.I)
        return statement.replace("?", "%s")

    def execute(self, statement: str, parameters=()):
        cursor = self.connection.cursor()
        try:
            cursor.execute(self._mysql_sql(statement), parameters)
        except Exception:
            cursor.close()
            raise
        return _MySQLResult(cursor)

    def executemany(self, statement: str, parameters):
        cursor = self.connection.cursor()
        try:
            cursor.executemany(self._mysql_sql(statement), parameters)
        except Exception:
            cursor.close()
            raise
        return _MySQLResult(cursor)


def _terms(text: str) -> list[str]:
    """Return word and character n-gram features for Chinese/English text."""
    terms: list[str] = []
    for part in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9_]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", part):
            terms.extend(part[i:i + 2] for i in range(max(1, len(part) - 1)))
            terms.extend(part)
        else:
            terms.append(part)
            terms.extend(part[i:i + 3] for i in range(max(1, len(part) - 2)))
    return [term for term in terms if term]


class LongTermMemory:
    """SQL document metadata + persisted FAISS indexes, fused with sparse ranking."""

    VECTOR_DIM = 8192
    INDEX_VERSION = 1

    def __init__(
        self,
        index_path: str = "./data/faiss",
        db_path: str = "./data/rag.sqlite3",
        database_url: str | None = None,
    ):
        self.index_dir = Path(index_path)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.database_url = database_url or os.getenv("RAG_DATABASE_URL")
        self._mysql_url = None
        self._fts_available = False
        if self.database_url:
            url = make_url(self.database_url)
            if url.get_backend_name() == "mysql":
                self._mysql_url = url
                self.db_path = None
            elif url.get_backend_name() == "sqlite":
                self.db_path = Path(url.database or db_path)
            else:
                raise ValueError("RAG_DATABASE_URL must use SQLite or MySQL")
        else:
            self.db_path = Path(db_path)
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._indices: dict[str, faiss.Index] = {}
        self._chunk_ids: dict[str, list[str]] = {}
        self._idfs: dict[str, dict[str, float]] = {}
        self._init_db()
        self._load_indexes()

    def _connect(self):
        if self._mysql_url is not None:
            import pymysql

            return _MySQLConnection(pymysql.connect(
                host=self._mysql_url.host,
                port=self._mysql_url.port or 3306,
                user=self._mysql_url.username,
                password=self._mysql_url.password,
                database=self._mysql_url.database,
                charset=self._mysql_url.query.get("charset", "utf8mb4"),
                connect_timeout=int(self._mysql_url.query.get("connect_timeout", 10)),
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
            ))
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            if self._mysql_url is not None:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS knowledge_bases (
                        id VARCHAR(64) PRIMARY KEY,
                        name VARCHAR(255) NOT NULL UNIQUE,
                        description TEXT NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS documents (
                        id VARCHAR(64) PRIMARY KEY,
                        kb_id VARCHAR(64) NOT NULL,
                        name VARCHAR(255) NOT NULL,
                        content_hash CHAR(64) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uq_documents_kb_hash (kb_id, content_hash),
                        CONSTRAINT fk_documents_kb FOREIGN KEY (kb_id)
                            REFERENCES knowledge_bases(id) ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS chunks (
                        id VARCHAR(64) PRIMARY KEY,
                        document_id VARCHAR(64) NOT NULL,
                        kb_id VARCHAR(64) NOT NULL,
                        source VARCHAR(1024) NOT NULL,
                        content TEXT NOT NULL,
                        terms TEXT NOT NULL,
                        KEY idx_chunks_kb (kb_id),
                        KEY idx_chunks_document (document_id),
                        CONSTRAINT fk_chunks_document FOREIGN KEY (document_id)
                            REFERENCES documents(id) ON DELETE CASCADE,
                        CONSTRAINT fk_chunks_kb FOREIGN KEY (kb_id)
                            REFERENCES knowledge_bases(id) ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS imports (
                        name VARCHAR(128) NOT NULL,
                        kb_id VARCHAR(64) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (name, kb_id),
                        CONSTRAINT fk_imports_kb FOREIGN KEY (kb_id)
                            REFERENCES knowledge_bases(id) ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                return

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    name TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(kb_id, content_hash)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    kb_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    source TEXT NOT NULL, content TEXT NOT NULL, terms TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS imports (
                    name TEXT NOT NULL, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(name,kb_id)
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_kb ON chunks(kb_id);
            """)
            try:
                conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED, kb_id UNINDEXED, terms, content, tokenize='unicode61'
                )""")
                self._fts_available = True
            except sqlite3.OperationalError:
                # FAISS remains available; sparse retrieval falls back to Python term overlap.
                pass

    def _index_file(self, kb_id: str) -> Path:
        return self.index_dir / f"{kb_id}.faiss"

    def _meta_file(self, kb_id: str) -> Path:
        return self.index_dir / f"{kb_id}.meta.json"

    def _load_indexes(self) -> None:
        with self._connect() as conn:
            kb_ids = [row[0] for row in conn.execute("SELECT id FROM knowledge_bases")]
        for kb_id in kb_ids:
            if not self._load_index(kb_id):
                self._rebuild_index(kb_id)

    def _load_index(self, kb_id: str) -> bool:
        index_file, meta_file = self._index_file(kb_id), self._meta_file(kb_id)
        if not index_file.exists() or not meta_file.exists():
            return False
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            with self._connect() as conn:
                rows = conn.execute("SELECT id FROM chunks WHERE kb_id=? ORDER BY id", (kb_id,)).fetchall()
            chunk_ids = [row["id"] for row in rows]
            index = faiss.read_index(str(index_file))
            if (meta.get("version") != self.INDEX_VERSION or meta.get("chunk_ids") != chunk_ids
                    or index.ntotal != len(chunk_ids) or index.d != self.VECTOR_DIM):
                return False
            self._indices[kb_id] = index
            self._chunk_ids[kb_id] = chunk_ids
            self._idfs[kb_id] = {key: float(value) for key, value in meta.get("idf", {}).items()}
            return True
        except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError):
            return False

    @staticmethod
    def _vector(text: str, idf: dict[str, float], dimensions: int) -> np.ndarray:
        counts = Counter(_terms(text))
        vector = np.zeros(dimensions, dtype=np.float32)
        for term, count in counts.items():
            bucket = int.from_bytes(hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest(), "little") % dimensions
            weight = (1.0 + math.log(count)) * idf.get(term, 1.0)
            vector[bucket] += weight
        norm = float(np.linalg.norm(vector))
        if norm:
            vector /= norm
        return vector

    def _rebuild_index(self, kb_id: str) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT id,content FROM chunks WHERE kb_id=? ORDER BY id", (kb_id,)).fetchall()
        chunk_ids = [row["id"] for row in rows]
        texts = [row["content"] for row in rows]

        document_terms = [set(_terms(text)) for text in texts]
        doc_freq: Counter[str] = Counter(term for terms in document_terms for term in terms)
        total_docs = len(texts)
        idf = {term: math.log((total_docs + 1) / (freq + 1)) + 1.0 for term, freq in doc_freq.items()}

        index = faiss.IndexFlatIP(self.VECTOR_DIM)
        if texts:
            matrix = np.vstack([self._vector(text, idf, self.VECTOR_DIM) for text in texts])
            index.add(matrix)

        index_file, meta_file = self._index_file(kb_id), self._meta_file(kb_id)
        faiss.write_index(index, str(index_file))
        meta_file.write_text(json.dumps({
            "version": self.INDEX_VERSION,
            "chunk_ids": chunk_ids,
            "idf": idf,
        }, ensure_ascii=False), encoding="utf-8")
        self._indices[kb_id] = index
        self._chunk_ids[kb_id] = chunk_ids
        self._idfs[kb_id] = idf

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT k.id, k.name, k.description, k.created_at,
                       (SELECT COUNT(*) FROM documents d WHERE d.kb_id=k.id) AS document_count,
                       (SELECT COUNT(*) FROM chunks c WHERE c.kb_id=k.id) AS chunk_count
                FROM knowledge_bases k
                ORDER BY k.created_at DESC
            """).fetchall()
        return [dict(row) for row in rows]

    def create_knowledge_base(self, name: str, description: str = "") -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("知识库名称不能为空")
        kb_id = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
        with self._connect() as conn:
            conn.execute("INSERT INTO knowledge_bases(id,name,description) VALUES(?,?,?)", (kb_id, name, description.strip()))
        self._rebuild_index(kb_id)
        return next(kb for kb in self.list_knowledge_bases() if kb["id"] == kb_id)

    def delete_knowledge_base(self, kb_id: str) -> bool:
        with self._connect() as conn:
            if self._fts_available:
                try:
                    conn.execute("DELETE FROM chunks_fts WHERE kb_id=?", (kb_id,))
                except sqlite3.OperationalError:
                    pass
            cur = conn.execute("DELETE FROM knowledge_bases WHERE id=?", (kb_id,))
        for path in (self._index_file(kb_id), self._meta_file(kb_id)):
            path.unlink(missing_ok=True)
        self._indices.pop(kb_id, None)
        self._chunk_ids.pop(kb_id, None)
        self._idfs.pop(kb_id, None)
        return cur.rowcount > 0

    def list_documents(self, kb_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT d.id, d.name, d.created_at, COUNT(c.id) AS chunk_count
                FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                WHERE d.kb_id=? GROUP BY d.id ORDER BY d.created_at DESC
            """, (kb_id,)).fetchall()
        return [dict(row) for row in rows]

    def has_source(self, kb_id: str, source: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM chunks WHERE kb_id=? AND source=? LIMIT 1", (kb_id, source)).fetchone() is not None

    def has_import(self, name: str, kb_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM imports WHERE name=? AND kb_id=?", (name, kb_id)).fetchone() is not None

    def mark_import(self, name: str, kb_id: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO imports(name,kb_id) VALUES(?,?)", (name, kb_id))

    def delete_document(self, document_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT kb_id FROM documents WHERE id=?", (document_id,)).fetchone()
            if row is None:
                return False
            kb_id = row["kb_id"]
            ids = [item[0] for item in conn.execute("SELECT id FROM chunks WHERE document_id=?", (document_id,))]
            if self._fts_available and ids:
                try:
                    conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", [(chunk_id,) for chunk_id in ids])
                except sqlite3.OperationalError:
                    pass
            conn.execute("DELETE FROM documents WHERE id=?", (document_id,))
        self._rebuild_index(kb_id)
        return True

    def add_document(self, content: str, source: str = "", metadata: dict | None = None,
                     kb_id: str | None = None, rebuild: bool = True) -> str:
        """Chunk and store one document, then refresh its FAISS index."""
        del metadata
        if not content.strip():
            raise ValueError("文档内容不能为空")
        if kb_id is None:
            bases = self.list_knowledge_bases()
            if not bases:
                kb = self.create_knowledge_base("默认知识库", "上传客服政策与产品文档后即可进行 RAG 问答")
                kb_id = kb["id"]
            else:
                kb_id = bases[0]["id"]
        source = (source or "未命名文档").replace("\\", "/").lstrip("./")
        name = source.rsplit("/", 1)[-1]
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        document_id = hashlib.sha256(f"{kb_id}:{content_hash}".encode()).hexdigest()[:20]
        was_added = False
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM knowledge_bases WHERE id=?", (kb_id,)).fetchone():
                raise ValueError("知识库不存在")
            conn.execute("INSERT OR IGNORE INTO documents(id,kb_id,name,content_hash) VALUES(?,?,?,?)",
                         (document_id, kb_id, name, content_hash))
            if not conn.execute("SELECT 1 FROM chunks WHERE document_id=? LIMIT 1", (document_id,)).fetchone():
                for i, chunk in enumerate(self._chunk_text(content)):
                    chunk_id = hashlib.sha256(f"{document_id}:{i}".encode()).hexdigest()[:24]
                    terms = " ".join(_terms(chunk))
                    conn.execute("INSERT INTO chunks(id,document_id,kb_id,source,content,terms) VALUES(?,?,?,?,?,?)",
                                 (chunk_id, document_id, kb_id, source, chunk, terms))
                    if self._fts_available:
                        try:
                            conn.execute("INSERT INTO chunks_fts(chunk_id,kb_id,terms,content) VALUES(?,?,?,?)",
                                         (chunk_id, kb_id, terms, chunk))
                        except sqlite3.OperationalError:
                            pass
                was_added = True
        if rebuild and was_added:
            self._rebuild_index(kb_id)
        return document_id

    def add_documents_batch(self, documents: list[dict]) -> list[str]:
        ids: list[str] = []
        affected: set[str] = set()
        for doc in documents:
            kb_id = doc.get("kb_id")
            if kb_id is None:
                bases = self.list_knowledge_bases()
                if not bases:
                    kb_id = self.create_knowledge_base("默认知识库")["id"]
                else:
                    kb_id = bases[0]["id"]
            ids.append(self.add_document(doc.get("content", ""), doc.get("source", ""),
                                         doc.get("metadata", {}), kb_id, rebuild=False))
            affected.add(kb_id)
        for kb_id in affected:
            self._rebuild_index(kb_id)
        return ids

    def search(self, query: str, top_k: int = 5, kb_id: str | None = None) -> list[dict]:
        query_terms = list(dict.fromkeys(_terms(query)))
        if not query_terms:
            return []
        with self._connect() as conn:
            if kb_id:
                kb_ids = [kb_id]
            else:
                kb_ids = [row[0] for row in conn.execute("SELECT id FROM knowledge_bases")]

            results: dict[str, dict] = {}
            rrf_k = 60
            match = " OR ".join('"' + term.replace('"', '') + '"' for term in query_terms[:64])
            for current_kb in kb_ids:
                ids = self._chunk_ids.get(current_kb, [])
                index = self._indices.get(current_kb)
                vector_rank: dict[str, int] = {}
                vector_scores: dict[str, float] = {}
                if index is not None and index.ntotal:
                    query_vector = self._vector(query, self._idfs.get(current_kb, {}), self.VECTOR_DIM).reshape(1, -1)
                    distances, positions = index.search(query_vector, min(index.ntotal, max(top_k * 8, 40)))
                    for rank, (similarity, position) in enumerate(zip(distances[0], positions[0]), 1):
                        if position >= 0:
                            chunk_id = ids[int(position)]
                            vector_rank[chunk_id] = rank
                            vector_scores[chunk_id] = float(similarity)

                sparse_rows = []
                if self._fts_available:
                    try:
                        sparse_rows = conn.execute("""
                            SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id=f.chunk_id
                            WHERE chunks_fts MATCH ? AND f.kb_id=? ORDER BY bm25(chunks_fts) LIMIT ?
                        """, (match, current_kb, max(top_k * 8, 40))).fetchall()
                    except sqlite3.OperationalError:
                        pass
                sparse_rank = {row["id"]: rank for rank, row in enumerate(sparse_rows, 1)}
                if not sparse_rank:
                    rows = conn.execute("SELECT id,terms FROM chunks WHERE kb_id=?", (current_kb,)).fetchall()
                    qset = set(query_terms)
                    lexical = [(len(qset & set(row["terms"].split())) / max(1, len(qset)), row["id"]) for row in rows]
                    lexical.sort(reverse=True)
                    sparse_rank = {chunk_id: rank for rank, (score, chunk_id) in enumerate(
                        (item for item in lexical if item[0] > 0), 1)}

                candidate_ids = set(vector_rank) | set(sparse_rank)
                if not candidate_ids:
                    continue
                placeholders = ",".join("?" for _ in candidate_ids)
                rows = conn.execute(
                    f"SELECT id,kb_id,source,content FROM chunks WHERE id IN ({placeholders})",
                    tuple(candidate_ids),
                ).fetchall()
                for row in rows:
                    chunk_id = row["id"]
                    rrf_score = (1 / (rrf_k + vector_rank[chunk_id]) if chunk_id in vector_rank else 0)
                    rrf_score += (1 / (rrf_k + sparse_rank[chunk_id]) if chunk_id in sparse_rank else 0)
                    item = dict(row)
                    item["score"] = rrf_score
                    item["vector_score"] = round(vector_scores.get(chunk_id, 0.0), 4)
                    results[chunk_id] = item

        ranked = sorted(results.values(), key=lambda item: item["score"], reverse=True)
        return ranked[:top_k]

    def save(self) -> None:
        for kb_id in list(self._indices):
            faiss.write_index(self._indices[kb_id], str(self._index_file(kb_id)))

    def load_knowledge_base(self, kb_dir: str) -> int:
        root = Path(kb_dir)
        if not root.exists():
            return 0
        documents = []
        for file_path in root.rglob("*.txt"):
            documents.append({"content": file_path.read_text(encoding="utf-8"),
                              "source": file_path.relative_to(root).as_posix()})
        self.add_documents_batch(documents)
        return sum(len(self._chunk_text(doc["content"])) for doc in documents)

    def load_pdf_directory(self, directory: str | Path, kb_id: str) -> int:
        """Import any missing, text-based PDF files from a directory into one KB."""
        from pypdf import PdfReader

        root = Path(directory)
        if not root.exists():
            return 0
        documents = []
        for file_path in sorted(root.rglob("*.pdf")):
            source = file_path.relative_to(root).as_posix()
            if self.has_source(kb_id, source):
                continue
            text = "\n".join(page.extract_text() or "" for page in PdfReader(str(file_path)).pages)
            if text.strip():
                documents.append({"content": text, "source": source, "kb_id": kb_id})
        self.add_documents_batch(documents)
        return len(documents)

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 700, overlap: int = 100) -> list[str]:
        text = re.sub(r"\r\n?", "\n", text).strip()
        if not text:
            return []
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + chunk_size)
            if end < len(text):
                boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end), text.rfind(".", start, end))
                if boundary > start + chunk_size // 2:
                    end = boundary + 1
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
        return chunks
