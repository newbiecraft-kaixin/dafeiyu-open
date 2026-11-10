"""标准 MCP（streamable-http）通用入口：把本机所有服务收成 dsh 可直接调用的一组工具。

只有 hub 进程会启动它，端口默认 23410（HUB_MCP_PORT 覆盖），独立于 23408 的 REST 面板。

工具：
  fy_services  列出本机所有能力服务（名字 / 端口 / 在线 / 职责 / 端点）
  fy_call      通用透传：服务名 + 路径 + 方法，任意接口都能打（含 /manage）
  其余 12 个   与 /tools 目录一致，直接转调 HANDLERS（pet.* / voice.say / sys.exec 等）

护栏：fy_call 与 sys.exec 必须带 requester（发起指令的人），且必须在 AstrBot
管理员名单里 —— 即「只有主人/管理员可调度」，群里普通成员说一句调不动。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from ._admins import admins
from .catalog import TOOLS as TOOL_CATALOG
from .http_client import http_call
from .probe import port_alive
from .registry import _read_manifests, _tool_ready
from .tools import HANDLERS

LOG = logging.getLogger("hub.mcp")

MCP_PORT = int(os.environ.get("HUB_MCP_PORT") or 23410)
MCP_PATH = "/mcp"
TOKEN_FILE = Path.home() / ".dsh" / "hub-mcp-token.txt"
MAX_TEXT = 20000

# 清单之外的固定端口：NapCat 的网页/WS、DSH 桥接、qq_mcp 自己。
ALIASES: Dict[str, int] = {
    "napcat-webui": 6099,
    "napcat-ws": 6199,
    "dsh-bridge": 18081,
    "qq-mcp": 18083,
    "hub-mcp": MCP_PORT,
}

# catalog 里的工具名带点（pet.speak），MCP 客户端对工具名更挑剔，统一换成下划线。
_CATALOG_BY_NAME = {tool["name"].replace(".", "_"): tool for tool in TOOL_CATALOG}


# ---------- 通用工具 ----------
def _service_rows() -> List[Dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor

    entries: List[Dict[str, Any]] = []
    for manifest in _read_manifests():
        entries.append({
            "name": manifest.get("name"),
            "port": manifest.get("port"),
            "responsibility": manifest.get("responsibility", ""),
            "endpoints": manifest.get("endpoints", []),
        })
    for name, port in ALIASES.items():
        entries.append({
            "name": name,
            "port": port,
            "responsibility": "固定端口别名",
            "endpoints": [],
        })

    # 并行探活：串行十几个端口最坏要好几秒，dsh 等不起。
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(entries)))) as pool:
        flags = list(pool.map(lambda item: port_alive(item["port"]), entries))
    for item, online in zip(entries, flags):
        item["online"] = online
    return entries


def _resolve_port(service: str) -> int:
    name = str(service or "").strip()
    if not name:
        raise ValueError("service 不能为空：先用 fy_services 看有哪些服务")
    if name.isdigit():
        return int(name)
    if name in ALIASES:
        return ALIASES[name]
    for manifest in _read_manifests():
        if manifest.get("name") == name and isinstance(manifest.get("port"), int):
            return int(manifest["port"])
    raise ValueError(f"未知服务 {name}：先用 fy_services 看清单")


def _require_master(requester: str) -> None:
    uid = str(requester or "").strip()
    allowed = [str(item).strip() for item in admins()]
    if not allowed:
        raise ValueError("AstrBot 管理员名单为空，通用入口已锁死；先在「机器人 · 管理员」里加人")
    if not uid:
        raise ValueError("这条调用必须带 requester（发起指令的人的 QQ 号），只有主人/管理员能调度")
    if uid not in allowed:
        raise ValueError(f"{uid} 不是主人/管理员，拒绝执行")


def _dump(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "…（已截断）"
    return text


def _fy_services(_arguments: Dict[str, Any]) -> str:
    return _dump({"services": _service_rows()})


def _fy_call(arguments: Dict[str, Any]) -> str:
    _require_master(str(arguments.get("requester") or ""))
    service = str(arguments.get("service") or "").strip()
    port = _resolve_port(service)
    method = str(arguments.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "DELETE"}:
        raise ValueError(f"不支持的方法 {method}")
    path = str(arguments.get("path") or "/").strip() or "/"
    if not path.startswith("/"):
        path = "/" + path
    query = arguments.get("query")
    if isinstance(query, dict) and query:
        path += ("&" if "?" in path else "?") + urlencode(
            {str(key): value for key, value in query.items()}
        )
    try:
        timeout = max(3.0, min(180.0, float(arguments.get("timeout", 30))))
    except (TypeError, ValueError):
        timeout = 30.0
    body = arguments.get("body")
    url = f"http://127.0.0.1:{port}{path}"
    status, data = http_call(method, url, body, timeout=timeout)
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = text
    return _dump({"service": service, "url": url, "status": status, "body": data})


def _call_catalog(name: str, arguments: Dict[str, Any]) -> str:
    tool = _CATALOG_BY_NAME[name]
    if name == "sys_exec":
        _require_master(str(arguments.get("requester") or ""))
    handler = HANDLERS.get(tool["name"])
    if handler is None:
        raise ValueError(f"工具 {tool['name']} 没有实现")
    success, result = handler(arguments)
    return _dump({"tool": tool["name"], "ok": success, "result": result})


# ---------- MCP 装配 ----------
def _tool_defs() -> List[types.Tool]:
    defs = [
        types.Tool(
            name="fy_services",
            description=(
                "列出大肥鱼本机所有服务（名字 / 端口 / 是否在线 / 职责 / 端点）。"
                "调 fy_call 之前先看这里，确认服务名和路径。"
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="fy_call",
            description=(
                "通用接口透传：按「服务名 + 路径」直接调用本机任意 HTTP 接口，"
                "GET/POST/PUT/DELETE 都支持，包含 hub 自身的 /manage 启停。"
                "写操作与危险操作只有主人/管理员能发起，requester 必须填发起这条指令的人的 QQ 号，"
                "普通群友的指令一律拒绝。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "fy_services 里的 name，或直接写端口号"},
                    "path": {"type": "string", "description": "接口路径，如 /health、/bot/model、/manage"},
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "DELETE"],
                        "description": "HTTP 方法，默认 GET",
                    },
                    "body": {"type": "object", "description": "JSON 请求体（POST/PUT 用）"},
                    "query": {"type": "object", "description": "查询参数对象"},
                    "timeout": {"type": "number", "description": "超时秒数，默认 30，最大 180"},
                    "requester": {
                        "type": "string",
                        "description": "发起这条指令的人的 QQ 号，必须是在册管理员/主人",
                    },
                },
                "required": ["service", "requester"],
            },
        ),
    ]
    for tool in TOOL_CATALOG:
        if not _tool_ready(tool):
            continue
        schema = json.loads(json.dumps(tool["inputSchema"]))
        name = tool["name"].replace(".", "_")
        description = tool["description"] + f"（原能力 {tool['name']}）"
        if name == "sys_exec":
            properties = schema.setdefault("properties", {})
            properties["requester"] = {
                "type": "string",
                "description": "发起这条指令的人的 QQ 号，必须是在册管理员/主人",
            }
            schema["required"] = list(schema.get("required") or []) + ["requester"]
        defs.append(types.Tool(name=name, description=description, inputSchema=schema))
    return defs


def _build_server() -> Server:
    server = Server("dafeiyu-hub")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return _tool_defs()

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: Dict[str, Any]) -> list[types.TextContent]:
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            if name == "fy_services":
                text = await asyncio.to_thread(_fy_services, arguments)
            elif name == "fy_call":
                text = await asyncio.to_thread(_fy_call, arguments)
            elif name in _CATALOG_BY_NAME:
                text = await asyncio.to_thread(_call_catalog, name, arguments)
            else:
                text = json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)
        except Exception as error:  # noqa: BLE001 - 全部转成文本回给模型，别断会话
            text = json.dumps({"error": str(error)}, ensure_ascii=False)
        return [types.TextContent(type="text", text=text)]

    return server


class _StreamableApp:
    """把 StreamableHTTPSessionManager 包成一个 ASGI 端点。"""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope, receive, send) -> None:
        await self.manager.handle_request(scope, receive, send)


class _BearerAuth:
    """Bearer 鉴权：/health 免鉴权，其余请求必须带 token。"""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") in {"/health", "/mcp/health"}:
            body = b'{"ok":true}'
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        headers = {}
        for key, value in scope.get("headers") or []:
            try:
                headers[key.decode("latin-1").lower()] = value.decode("latin-1")
            except UnicodeDecodeError:
                continue
        if headers.get("authorization", "") != f"Bearer {self.token}":
            body = b'{"error":"unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def _load_token() -> str:
    env = os.environ.get("HUB_MCP_TOKEN", "").strip()
    if env:
        return env
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    except OSError as error:
        LOG.error("[mcp] 写 token 文件失败: %s", error)
        return ""
    return token


def _build_app(token: str):
    from starlette.applications import Starlette
    from starlette.routing import Route

    manager = StreamableHTTPSessionManager(app=_build_server(), json_response=True, stateless=True)
    app = Starlette(
        routes=[Route(MCP_PATH, endpoint=_StreamableApp(manager))],
        lifespan=lambda _app: manager.run(),
    )
    return _BearerAuth(app, token)


def start() -> Optional[threading.Thread]:
    """在后台线程启动 MCP；返回线程，失败返回 None。"""
    try:
        import uvicorn
    except ImportError as error:
        LOG.error("[mcp] 缺少 uvicorn，MCP 不启动: %s", error)
        return None
    if port_alive(MCP_PORT):
        LOG.warning("[mcp] 端口 %s 已被占用，本次不启动", MCP_PORT)
        return None
    token = _load_token()
    if not token:
        return None

    config = uvicorn.Config(
        _build_app(token),
        host="127.0.0.1",
        port=MCP_PORT,
        log_level="warning",
        access_log=False,
        lifespan="on",
        # pythonw 下 sys.stdout 是 None，uvicorn 默认日志配置会崩；交给 logging 自己来。
        log_config=None,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # 由主进程统一收尾

    def _run() -> None:
        try:
            server.run()
        except BaseException as error:  # noqa: BLE001 - 别让异常炸掉 hub 主进程
            LOG.error("[mcp] 服务退出: %s: %s", type(error).__name__, error)

    thread = threading.Thread(target=_run, name="hub-mcp", daemon=True)
    thread.start()
    LOG.info("[mcp] 通用入口已启动: http://127.0.0.1:%s%s（token 见 %s）",
             MCP_PORT, MCP_PATH, TOKEN_FILE)
    return thread
