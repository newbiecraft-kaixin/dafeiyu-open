"""rag：知识库微服务（资料入库 + 分块索引 + BM25 检索）。

端口：23409（infra.port_of("rag")，可用 RAG_PORT 覆盖）

远程访问
  RAG_HOST=0.0.0.0   监听所有网卡（默认 127.0.0.1 只给本机）
  RAG_TOKEN=xxx      开启令牌校验：/documents 与 /search 需要
                     Authorization: Bearer xxx（也可用 ?token=xxx）
                     / 与 /health 不校验，方便探活

页面
  GET /             知识库管理页：粘贴/导入资料、查看文档、检索测试

接口
  GET  /health                 概况（文档数 / 分块数 / 字数）
  GET  /documents?limit=&scope= 文档列表（scope 为空表示只看公共资料）
  GET  /documents/<id>         单篇文档与分块
  POST /documents              {id?, title, text, tags?, source?, scope?} 新增或整篇更新
  DELETE /documents?id=        删除文档及其分块
  POST /search                 {query, top_k?, scope?, include_shared?} 检索

按会话隔离
  scope 填会话 umo（如 aiocqhttp:GroupMessage:123456）或群号 / QQ 号；
  写入时带上 scope，检索时带上同一个 scope，就只会搜到本群/本好友的资料。
  scope 为空的资料是公共资料，默认对所有会话可见（include_shared=false 可关掉）。
  不传 scope 时检索全部资料，保持老接口行为。

资料落 services/rag/data/rag.db（SQLite，已在 .gitignore 排除）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import (  # noqa: E402
    Html,
    Stats,
    fail,
    ok,
    port_of,
    run,
)

from ragstore import RagStore  # noqa: E402

LOG = logging.getLogger("rag")
PORT = port_of("rag", 23409)
HOST = (os.environ.get("RAG_HOST") or "127.0.0.1").strip()
TOKEN = (os.environ.get("RAG_TOKEN") or "").strip()
# 远端知识库：设了就把所有数据接口转过去，本机只做统一入口（页面/端口不变）
UPSTREAM = (os.environ.get("RAG_UPSTREAM_BASE_URL") or "").strip().rstrip("/")
UPSTREAM_TOKEN = (os.environ.get("RAG_UPSTREAM_TOKEN") or "").strip()
UI_DIR = _HERE.parent / "ui"
db = RagStore()
counts = Stats()


def _log(*args: Any) -> None:
    print(f"[rag {time.strftime('%H:%M:%S')}]", *args, flush=True)


def _payload(req) -> Dict[str, Any]:  # noqa: ANN001
    return req.payload if isinstance(req.payload, dict) else {}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bearer(req) -> str:  # noqa: ANN001
    raw = ""
    for key, value in (req.headers or {}).items():
        if str(key).lower() == "authorization":
            raw = str(value).strip()
            break
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return str(req.q("token") or "").strip()


def _authorized(req) -> bool:  # noqa: ANN001
    """没设 RAG_TOKEN 就零门槛；设了就要求 Bearer 或 ?token=。"""
    if not TOKEN:
        return True
    return _bearer(req) == TOKEN


def _protected(handler):  # noqa: ANN001, ANN201
    """远程写/读资料的接口在开了令牌后才放行。"""

    def guarded(req):  # noqa: ANN001
        if not _authorized(req):
            return fail("未授权：请带 Authorization: Bearer <RAG_TOKEN>", 401)
        return handler(req)

    return guarded


def _proxy(req) -> Any:  # noqa: ANN001
    """把请求原样转给远端知识库（路径、查询串、JSON 体、令牌）。"""
    import urllib.error
    import urllib.parse
    import urllib.request

    qs = urllib.parse.urlencode(req.query) if req.query else ""
    url = UPSTREAM + req.path + (f"?{qs}" if qs else "")
    raw = None
    if req.payload not in (None, {}, ""):
        raw = json.dumps(req.payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = UPSTREAM_TOKEN or _bearer(req)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    call = urllib.request.Request(url, data=raw, method=req.method, headers=headers)
    try:
        with urllib.request.urlopen(call, timeout=30) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        return fail(f"远端知识库不可用：{type(exc).__name__}: {exc}", 502)
    try:
        return status, json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return fail("远端知识库返回了非 JSON 内容", 502)


def handle_page(_req) -> Any:  # noqa: ANN001
    path = UI_DIR / "index.html"
    if not path.exists():
        return 404, Html('<h1 style="font:16px system-ui;color:#ffc2cd">页面缺失：ui/index.html</h1>')
    return 200, Html(path.read_text(encoding="utf-8"))


def handle_documents_get(req) -> Any:  # noqa: ANN001
    limit = _int(req.q("limit") or 300)
    scope = req.q("scope")
    counts.inc("documents_list")
    return ok(db.list_documents(limit or 300, scope=None if scope is None else str(scope)))


def handle_document_get(req) -> Any:  # noqa: ANN001
    tail = req.path.rsplit("/", 1)[-1]
    doc_id = _int(tail)
    if doc_id <= 0:
        return fail("文档编号不合法", 400)
    row = db.get_document(doc_id)
    if row is None:
        return fail(f"文档 {doc_id} 不存在", 404)
    counts.inc("documents_read")
    return ok(row)


def handle_documents_post(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    text = str(body.get("text") or "").strip()
    if not text:
        return fail("text 不能为空", 400)
    doc_id = _int(body.get("id")) or None
    try:
        row = db.save_document(
            str(body.get("title") or ""),
            text,
            tags=str(body.get("tags") or ""),
            source=str(body.get("source") or ""),
            scope=str(body.get("scope") or ""),
            doc_id=doc_id,
        )
    except KeyError as exc:
        return fail(str(exc).strip("'\""), 404)
    except ValueError as exc:
        return fail(str(exc), 400)
    counts.inc("documents_write")
    _log("保存文档", row["id"], row["title"], f"{row['chunks']} 块")
    return ok(row)


def handle_documents_delete(req) -> Any:  # noqa: ANN001
    doc_id = _int(req.q("id"))
    if doc_id <= 0:
        return fail("缺少 id", 400)
    deleted = db.delete_document(doc_id)
    if not deleted:
        return fail(f"文档 {doc_id} 不存在", 404)
    counts.inc("documents_delete")
    _log("删除文档", doc_id)
    return ok({"deleted": doc_id})


def handle_search(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    query = str(body.get("query") or body.get("q") or req.q("q") or "").strip()
    if not query:
        return fail("query 不能为空", 400)
    top_k = _int(body.get("top_k") or req.q("top_k") or 5)
    scope = body.get("scope")
    if scope is None:
        scope = req.q("scope")
    shared = body.get("include_shared")
    if shared is None:
        shared = req.q("include_shared")
    include_shared = True if shared is None else str(shared).strip().lower() not in {
        "0", "false", "no", "off",
    }
    results = db.search(
        query, top_k or 5,
        scope=None if scope is None else str(scope),
        include_shared=include_shared,
    )
    counts.inc("search")
    return ok({
        "query": query,
        "scope": None if scope is None else str(scope),
        "count": len(results),
        "results": results,
    })


def handle_health(req) -> Any:  # noqa: ANN001
    # 平铺结构：中枢服务卡片会把 stats 里的数字直接展示出来
    data = {
        "ok": True,
        "service": "rag",
        "port": PORT,
        "host": HOST,
        "auth": bool(TOKEN),
        "upstream": UPSTREAM,
        "stats": db.stats(),
    }
    if UPSTREAM:
        status, remote = _proxy(req)
        if not isinstance(remote, dict):
            remote = {}
        data["remote"] = remote.get("stats") or remote
        data["ok"] = status == 200 and bool(remote.get("ok", True))
        if not data["ok"]:
            data["error"] = remote.get("error") or f"远端返回 HTTP {status}"
    return 200, data


if UPSTREAM:
    # 远端模式：数据接口全部转到远端，本机只保留页面和统一端口
    routes = {
        "GET /": handle_page,

        "GET /documents": _protected(_proxy),
        "GET /documents/*": _protected(_proxy),
        "POST /documents": _protected(_proxy),
        "DELETE /documents": _protected(_proxy),
        "POST /search": _protected(_proxy),
        "GET /health": handle_health,
    }
else:
    routes = {
        "GET /": handle_page,

        "GET /documents": _protected(handle_documents_get),
        "GET /documents/*": _protected(handle_document_get),
        "POST /documents": _protected(handle_documents_post),
        "DELETE /documents": _protected(handle_documents_delete),
        "POST /search": _protected(handle_search),
        "GET /health": handle_health,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _log(
        f"starting on {HOST}:{PORT}; db={db.path}; "
        f"auth={'on' if TOKEN else 'off'}; upstream={UPSTREAM or 'local'}"
    )
    run(PORT, routes, service="rag", version="0.1.0", host=HOST, stats=counts)


if __name__ == "__main__":
    main()
