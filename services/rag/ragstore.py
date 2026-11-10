"""知识库存储（SQLite）与检索（BM25）。

资料按「文档」入库，文档切块后建立内存索引；检索用纯标准库实现的 BM25，
中文按二元组切词，英文/数字按单词。数据落在 services/rag/data/rag.db，
进程启动时从库里重建索引，写入/删除后立即重建。

用法：
    from ragstore import RagStore
    db = RagStore()
    db.save_document("安装说明", "第一步……")
    db.search("怎么安装", top_k=5)
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "rag.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  title   TEXT    NOT NULL,
  tags    TEXT    DEFAULT '',
  source  TEXT    DEFAULT '',
  scope   TEXT    NOT NULL DEFAULT '',
  text    TEXT    NOT NULL,
  created REAL    NOT NULL,
  updated REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id INTEGER NOT NULL,
  idx    INTEGER NOT NULL,
  text   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, idx);
CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated DESC);
"""

MAX_CHUNK = 600   # 单块最多字符数
OVERLAP = 80      # 相邻块重叠，避免答案被切断
TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.I)


def tokenize(text: str) -> List[str]:
    """中英混合切词：英文/数字按单词，中文按二元组（单字兜底）。"""
    tokens: List[str] = []
    for match in TOKEN_RE.finditer(str(text or "").lower()):
        piece = match.group(0)
        if piece[0].isascii():
            if len(piece) > 1:
                tokens.append(piece)
            continue
        if len(piece) == 1:
            tokens.append(piece)
        else:
            tokens.extend(piece[i:i + 2] for i in range(len(piece) - 1))
    return tokens


def split_chunks(text: str) -> List[str]:
    """按段落聚合切块；超长段落硬切并保留少量重叠。"""
    body = re.sub(r"\r\n?", "\n", str(text or "")).strip()
    if not body:
        return []
    paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
    chunks: List[str] = []
    buf = ""
    for para in paragraphs:
        while len(para) > MAX_CHUNK:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[:MAX_CHUNK])
            para = para[MAX_CHUNK - OVERLAP:]
        if not buf:
            buf = para
        elif len(buf) + 1 + len(para) <= MAX_CHUNK:
            buf = f"{buf}\n{para}"
        else:
            chunks.append(buf)
            tail = buf[-OVERLAP:]
            buf = f"{tail}\n{para}" if len(tail) + 1 + len(para) <= MAX_CHUNK else para
    if buf:
        chunks.append(buf)
    return chunks


def _auto_title(text: str) -> str:
    """没写标题时取正文首行，去掉 Markdown 记号。"""
    for line in str(text or "").splitlines():
        clean = line.strip().lstrip("#").strip()
        if clean:
            return clean[:40]
    return "未命名资料"


def _snippet(text: str, terms: List[str], width: int = 160) -> str:
    """截取包含查询词的一小段，给列表做预览。"""
    low = text.lower()
    pos = -1
    for term in terms:
        found = low.find(term)
        if found >= 0 and (pos < 0 or found < pos):
            pos = found
    if pos < 0:
        pos = 0
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    piece = text[start:end].replace("\n", " ").strip()
    return ("…" if start else "") + piece + ("…" if end < len(text) else "")


class RagStore:
    def __init__(self, path: Optional[str | Path] = None) -> None:
        raw = os.environ.get("RAG_DB") or ""
        self.path = Path(path or raw or DEFAULT_DB)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._index_lock = threading.Lock()
        self._chunks: List[Dict[str, Any]] = []
        self._df: Counter = Counter()
        self._total_len = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn()
        self._refresh()

    # ---------------------------------------------------------------- 连接
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        # 老库迁移：scope 为空表示公共资料，老数据升级后自然成为公共
        cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        if "scope" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_scope ON documents(scope)")
        conn.commit()
        self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------------------------------------------------------------- 索引
    def _refresh(self) -> None:
        """从库里重建内存索引；文档量在本机场景下很小，重建成本可忽略。"""
        rows = self._conn().execute(
            "SELECT c.id, c.doc_id, c.idx, c.text, d.title, d.tags, d.scope"
            " FROM chunks c JOIN documents d ON d.id = c.doc_id"
            " ORDER BY c.doc_id, c.idx"
        ).fetchall()
        chunks: List[Dict[str, Any]] = []
        df: Counter = Counter()
        total = 0
        for row in rows:
            # 标题与标签一起参与打分：只写关键词做标题的情况也能命中
            tokens = tokenize(f"{row['title']} {row['tags']} {row['text']}")
            tf = Counter(tokens)
            chunks.append({
                "id": row["id"],
                "doc_id": row["doc_id"],
                "idx": row["idx"],
                "text": row["text"],
                "title": row["title"],
                "scope": row["scope"] or "",
                "tf": tf,
                "length": len(tokens),
            })
            df.update(tf.keys())
            total += len(tokens)
        with self._index_lock:
            self._chunks = chunks
            self._df = df
            self._total_len = total

    # ---------------------------------------------------------------- 文档
    def save_document(
        self,
        title: str,
        text: str,
        *,
        tags: str = "",
        source: str = "",
        scope: str = "",
        doc_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        body = str(text or "").strip()
        if not body:
            raise ValueError("资料正文不能为空")
        name = str(title or "").strip() or _auto_title(body)
        owner = str(scope or "").strip()
        pieces = split_chunks(body)
        now = time.time()
        with self._write_lock:
            conn = self._conn()
            if doc_id:
                row = conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,)).fetchone()
                if row is None:
                    raise KeyError(f"文档 {doc_id} 不存在")
                conn.execute(
                    "UPDATE documents SET title = ?, tags = ?, source = ?, scope = ?, text = ?, updated = ?"
                    " WHERE id = ?",
                    (name, str(tags or ""), str(source or ""), owner, body, now, doc_id),
                )
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            else:
                cur = conn.execute(
                    "INSERT INTO documents(title, tags, source, scope, text, created, updated)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (name, str(tags or ""), str(source or ""), owner, body, now, now),
                )
                doc_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO chunks(doc_id, idx, text) VALUES(?, ?, ?)",
                [(doc_id, i, piece) for i, piece in enumerate(pieces)],
            )
            conn.commit()
            self._refresh()
        return {"id": doc_id, "title": name, "scope": owner, "chunks": len(pieces), "chars": len(body)}

    def delete_document(self, doc_id: int) -> bool:
        with self._write_lock:
            conn = self._conn()
            row = conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            conn.commit()
            self._refresh()
        return True

    def list_documents(self, limit: int = 300, scope: Optional[str] = None) -> List[Dict[str, Any]]:
        size = max(1, min(int(limit), 1000))
        conn = self._conn()
        base = (
            "SELECT d.id, d.title, d.tags, d.source, d.scope, LENGTH(d.text) AS chars,"
            " d.created, d.updated,"
            " (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS chunks"
            " FROM documents d"
        )
        if scope is None:
            rows = conn.execute(
                base + " ORDER BY d.updated DESC LIMIT ?", (size,)
            ).fetchall()
        else:
            rows = conn.execute(
                base + " WHERE d.scope = ? ORDER BY d.updated DESC LIMIT ?",
                (str(scope).strip(), size),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_document(self, doc_id: int) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        row = conn.execute(
            "SELECT id, title, tags, source, scope, text, created, updated"
            " FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            return None
        pieces = conn.execute(
            "SELECT idx, text FROM chunks WHERE doc_id = ? ORDER BY idx", (doc_id,)
        ).fetchall()
        data = dict(row)
        data["chunks"] = [dict(piece) for piece in pieces]
        return data

    # ---------------------------------------------------------------- 检索
    def search(
        self,
        query: str,
        top_k: int = 5,
        scope: Optional[str] = None,
        include_shared: bool = True,
    ) -> List[Dict[str, Any]]:
        """检索。scope 为空/不传 = 搜全部；传了 = 只搜该会话的资料（默认带公共）。

        scope 就是会话 umo（如 `aiocqhttp:GroupMessage:123456`）或群号/QQ，
        每个群、每个好友各自一份；公共资料（scope 为空）默认对所有会话可见。
        """
        terms = tokenize(query)
        if not terms:
            return []
        try:
            size = max(1, min(int(top_k), 20))
        except (TypeError, ValueError):
            size = 5
        qtf = Counter(terms)
        owner = None if scope is None else str(scope).strip()
        with self._index_lock:
            chunks = self._chunks
            df = self._df
            total = self._total_len
        if owner is not None:
            chunks = [
                chunk for chunk in chunks
                if chunk["scope"] == owner or (include_shared and not chunk["scope"])
            ]
            total = sum(chunk["length"] for chunk in chunks)
        if not chunks:
            return []
        count = len(chunks)
        avg_len = (total / count) or 1.0
        k1, b = 1.5, 0.75
        scored: List[tuple] = []
        for chunk in chunks:
            score = 0.0
            for term, weight in qtf.items():
                tf = chunk["tf"].get(term)
                if not tf:
                    continue
                idf = math.log(1 + (count - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
                denom = tf + k1 * (1 - b + b * chunk["length"] / avg_len)
                score += weight * idf * (tf * (k1 + 1)) / denom
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "doc_id": chunk["doc_id"],
                "title": chunk["title"],
                "scope": chunk["scope"],
                "chunk": chunk["idx"],
                "score": round(score, 3),
                "snippet": _snippet(chunk["text"], terms),
                "text": chunk["text"],
            }
            for score, chunk in scored[:size]
        ]

    # ---------------------------------------------------------------- 概况
    def stats(self) -> Dict[str, Any]:
        conn = self._conn()
        docs = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(LENGTH(text)), 0) AS chars FROM documents"
        ).fetchone()
        chunks = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()
        with self._index_lock:
            terms = len(self._df)
        return {
            "documents": docs["c"],
            "chunks": chunks["c"],
            "chars": docs["chars"],
            "terms": terms,
        }
