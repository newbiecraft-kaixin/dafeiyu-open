"""store（统一记忆）：SQLite 落地 + 本机 HTTP 接口。

端口：23407（infra.port_of("store")，可用 STORE_PORT 覆盖）

接口
  GET  /messages?scope=&limit=      最近消息（按时间正序）
  POST /messages                    追加一条消息
  GET  /sessions?scope=             会话指针
  POST /sessions                    写入会话指针
  DELETE /sessions?scope=           删除会话指针
  GET  /facts?scope=&q=&limit=      长期事实
  POST /facts                       写入/覆盖长期事实
  DELETE /facts?scope=&key=         删除长期事实
  GET  /status                      库概况

所有角色、平台、桌宠都走这里，避免各自维护一份 JSON 记忆。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import (  # noqa: E402
    Stats,
    fail,
    ok,
    port_of,
    run,
)

from store import MemoryStore  # noqa: E402

LOG = logging.getLogger("store")
PORT = port_of("store")
db = MemoryStore()
counts = Stats()


def _log(*args: Any) -> None:
    print(f"[store {time.strftime('%H:%M:%S')}]", *args, flush=True)


def _payload(req) -> Dict[str, Any]:  # noqa: ANN001
    return req.payload if isinstance(req.payload, dict) else {}


def handle_messages_get(req) -> Any:  # noqa: ANN001
    scope = str(req.q("scope") or "").strip()
    if not scope:
        return fail("缺少 scope", 400)
    limit = int(req.q("limit") or 12)
    counts.inc("messages_read")
    return ok(db.recent_messages(scope, limit))


def handle_messages_post(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    scope = str(body.get("scope") or "").strip()
    text = str(body.get("text") or "").strip()
    if not scope or not text:
        return fail("scope 与 text 不能为空", 400)
    rowid = db.append_message(
        scope,
        str(body.get("kind") or "user"),
        text,
        role=str(body.get("role") or ""),
        platform=str(body.get("platform") or ""),
        chat_id=str(body.get("chat_id") or ""),
        chat_type=str(body.get("chat_type") or ""),
        sender_id=str(body.get("sender_id") or ""),
        sender_name=str(body.get("sender_name") or ""),
        trace_id=str(body.get("trace_id") or ""),
    )
    counts.inc("messages_write")
    return ok({"id": rowid})


def handle_sessions_get(req) -> Any:  # noqa: ANN001
    scope = str(req.q("scope") or "").strip()
    if not scope:
        return fail("缺少 scope", 400)
    return ok(db.get_session(scope))


def handle_sessions_post(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    scope = str(body.get("scope") or "").strip()
    sid = str(body.get("session_id") or "").strip()
    if not scope or not sid:
        return fail("scope 与 session_id 不能为空", 400)
    db.set_session(
        scope,
        sid,
        role=str(body.get("role") or ""),
        platform=str(body.get("platform") or ""),
        chat_id=str(body.get("chat_id") or ""),
        chat_type=str(body.get("chat_type") or ""),
        provider=str(body.get("provider") or ""),
        model=str(body.get("model") or ""),
    )
    counts.inc("sessions_write")
    return ok({"scope": scope, "session_id": sid})


def handle_sessions_delete(req) -> Any:  # noqa: ANN001
    scope = str(req.q("scope") or "").strip()
    if not scope:
        return fail("缺少 scope", 400)
    db.drop_session(scope)
    return ok({"dropped": scope})


def handle_facts_get(req) -> Any:  # noqa: ANN001
    scope = str(req.q("scope") or "").strip()
    if not scope:
        return fail("缺少 scope", 400)
    limit = int(req.q("limit") or 8)
    counts.inc("facts_read")
    return ok(db.recall(scope, str(req.q("q") or ""), limit))


def handle_facts_post(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    scope = str(body.get("scope") or "").strip()
    key = str(body.get("key") or "").strip()
    value = str(body.get("value") or "").strip()
    if not scope or not key or not value:
        return fail("scope、key、value 不能为空", 400)
    try:
        row = db.remember(scope, key, value, float(body.get("importance") or 1.0))
    except ValueError as exc:
        return fail(str(exc), 400)
    counts.inc("facts_write")
    return ok(row)


def handle_facts_delete(req) -> Any:  # noqa: ANN001
    scope = str(req.q("scope") or "").strip()
    key = str(req.q("key") or "").strip()
    if not scope or not key:
        return fail("缺少 scope 或 key", 400)
    return ok({"deleted": db.forget(scope, key)})


def handle_status(req) -> Any:  # noqa: ANN001
    return ok(db.stats())


routes = {
    "GET /messages": handle_messages_get,
    "POST /messages": handle_messages_post,
    "GET /sessions": handle_sessions_get,
    "POST /sessions": handle_sessions_post,
    "DELETE /sessions": handle_sessions_delete,
    "GET /facts": handle_facts_get,
    "POST /facts": handle_facts_post,
    "DELETE /facts": handle_facts_delete,
    "GET /status": handle_status,
}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _log(f"starting on 127.0.0.1:{PORT}; db={db.path}")
    run(PORT, routes, service="store", version="0.1.0", stats=counts)


if __name__ == "__main__":
    main()
