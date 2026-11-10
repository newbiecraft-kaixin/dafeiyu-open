"""统一记忆存储（SQLite）：会话、消息、长期事实。

所有角色 / 平台 / 桌宠共用这一套读写接口，不再各自维护 JSON 文件。
线程安全：每个线程持有独立连接；WAL 模式允许并发读。

用法：
    from store import MemoryStore
    db = MemoryStore()                      # 默认 services/store/data/memory.db
    db.append_message("大肥鱼|qq|group|g1", "user", "在吗")
    db.set_session("大肥鱼|qq|group|g1", "s-1")
    db.remember("大肥鱼", "主人喜欢", "晚上十点后不想被打扰")
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL    NOT NULL,
  scope       TEXT    NOT NULL,
  role        TEXT    DEFAULT '',
  platform    TEXT    DEFAULT '',
  chat_id     TEXT    DEFAULT '',
  chat_type   TEXT    DEFAULT '',
  sender_id   TEXT    DEFAULT '',
  sender_name TEXT    DEFAULT '',
  kind        TEXT    NOT NULL,           -- user | assistant | notice | system
  text        TEXT    NOT NULL,
  trace_id    TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_scope_ts ON messages(scope, id DESC);

CREATE TABLE IF NOT EXISTS sessions (
  scope      TEXT PRIMARY KEY,
  role       TEXT DEFAULT '',
  platform   TEXT DEFAULT '',
  chat_id    TEXT DEFAULT '',
  chat_type  TEXT DEFAULT '',
  session_id TEXT NOT NULL,
  provider   TEXT DEFAULT '',
  model      TEXT DEFAULT '',
  updated    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scope      TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,
  importance REAL NOT NULL DEFAULT 1,
  created    REAL NOT NULL,
  updated    REAL NOT NULL,
  UNIQUE(scope, key)
);
CREATE INDEX IF NOT EXISTS idx_facts_scope ON facts(scope, importance DESC, updated DESC);
"""


def _now() -> float:
    return time.time()


class MemoryStore:
    def __init__(self, path: str | Path | None = None) -> None:
        raw = os.environ.get("DSH_DAFEIYU_STORE_DB") or ""
        self.path = Path(path or raw or DEFAULT_DB)
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn()

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
        conn.commit()
        self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------------------------------------------------------------- 消息
    def append_message(
        self,
        scope: str,
        kind: str,
        text: str,
        *,
        role: str = "",
        platform: str = "",
        chat_id: str = "",
        chat_type: str = "",
        sender_id: str = "",
        sender_name: str = "",
        trace_id: str = "",
        ts: Optional[float] = None,
    ) -> int:
        body = str(text or "").strip()
        if not body:
            return 0
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO messages"
            "(ts,scope,role,platform,chat_id,chat_type,sender_id,sender_name,kind,text,trace_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                float(ts if ts is not None else _now()),
                str(scope),
                str(role or ""),
                str(platform or ""),
                str(chat_id or ""),
                str(chat_type or ""),
                str(sender_id or ""),
                str(sender_name or ""),
                str(kind or "user"),
                body,
                str(trace_id or ""),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)

    def recent_messages(self, scope: str, limit: int = 12) -> List[Dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM messages WHERE scope=? ORDER BY id DESC LIMIT ?",
            (str(scope), limit),
        ).fetchall()
        items = [dict(r) for r in reversed(rows)]
        self._fill_sender_names(conn, scope, items)
        return items

    def _fill_sender_names(
        self, conn: sqlite3.Connection, scope: str, items: List[Dict[str, Any]]
    ) -> None:
        """返回时补昵称：存储以 sender_id 为主，展示优先用昵称。

        消息写入时可能只有 sender_id（桥接层没拿到昵称），返回前用同一
        scope 内该 sender 最近一次已知昵称回填，避免把 ID 直接展示出去。
        """
        missing = {
            str(item.get("sender_id") or "")
            for item in items
            if str(item.get("sender_id") or "") and not str(item.get("sender_name") or "").strip()
        }
        if not missing:
            return
        known: Dict[str, str] = {}
        for sid in missing:
            row = conn.execute(
                "SELECT sender_name FROM messages"
                " WHERE scope=? AND sender_id=? AND sender_name<>''"
                " ORDER BY id DESC LIMIT 1",
                (str(scope), sid),
            ).fetchone()
            if row is not None and str(row[0] or "").strip():
                known[sid] = str(row[0]).strip()
        for item in items:
            if str(item.get("sender_name") or "").strip():
                continue
            sid = str(item.get("sender_id") or "")
            if sid in known:
                item["sender_name"] = known[sid]

    # ---------------------------------------------------------------- 会话
    def set_session(
        self,
        scope: str,
        session_id: str,
        *,
        role: str = "",
        platform: str = "",
        chat_id: str = "",
        chat_type: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        conn = self._conn()
        conn.execute(
            "INSERT INTO sessions(scope,role,platform,chat_id,chat_type,session_id,provider,model,updated)"
            " VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(scope) DO UPDATE SET"
            " session_id=excluded.session_id, provider=excluded.provider,"
            " model=excluded.model, updated=excluded.updated",
            (
                str(scope),
                str(role or ""),
                str(platform or ""),
                str(chat_id or ""),
                str(chat_type or ""),
                sid,
                str(provider or ""),
                str(model or ""),
                _now(),
            ),
        )
        conn.commit()

    def get_session(self, scope: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM sessions WHERE scope=?", (str(scope),)
        ).fetchone()
        return dict(row) if row is not None else None

    def drop_session(self, scope: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM sessions WHERE scope=?", (str(scope),))
        conn.commit()

    # ---------------------------------------------------------------- 长期事实
    def remember(self, scope: str, key: str, value: str, importance: float = 1.0) -> Dict[str, Any]:
        k, v = str(key or "").strip(), str(value or "").strip()
        if not k or not v:
            raise ValueError("key 与 value 都不能为空")
        conn = self._conn()
        now = _now()
        conn.execute(
            "INSERT INTO facts(scope,key,value,importance,created,updated) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(scope,key) DO UPDATE SET"
            " value=excluded.value, importance=excluded.importance, updated=excluded.updated",
            (str(scope), k, v, float(importance or 1.0), now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM facts WHERE scope=? AND key=?", (str(scope), k)
        ).fetchone()
        return dict(row) if row is not None else {}

    def recall(self, scope: str, query: str = "", limit: int = 8) -> List[Dict[str, Any]]:
        limit = max(1, min(50, int(limit)))
        conn = self._conn()
        q = str(query or "").strip()
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                "SELECT * FROM facts WHERE scope=? AND (key LIKE ? OR value LIKE ?)"
                " ORDER BY importance DESC, updated DESC LIMIT ?",
                (str(scope), like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM facts WHERE scope=? ORDER BY importance DESC, updated DESC LIMIT ?",
                (str(scope), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def forget(self, scope: str, key: str) -> bool:
        conn = self._conn()
        cur = conn.execute("DELETE FROM facts WHERE scope=? AND key=?", (str(scope), str(key)))
        conn.commit()
        return bool(cur.rowcount)

    # ---------------------------------------------------------------- 概况
    def stats(self) -> Dict[str, Any]:
        conn = self._conn()
        return {
            "db": str(self.path),
            "messages": int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]),
            "sessions": int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]),
            "facts": int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]),
        }
