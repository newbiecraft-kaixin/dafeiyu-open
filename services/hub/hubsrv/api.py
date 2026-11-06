"""对外 HTTP 接口：控制台、能力清单、工具列表、MCP 调用、健康检查。"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from infra import fail, ok

from . import botctl, manage, petctl, windows
from .catalog import TOOLS
from .dashboard import handle_ui
from .registry import _capabilities, _tool_ready
from .tools import HANDLERS
from .views import handle_view


def handle_capabilities(_req: Any) -> Any:
    return ok(_capabilities())


def handle_tools(_req: Any) -> Any:
    ready = [tool for tool in TOOLS if _tool_ready(tool)]
    return ok(ready)


def handle_mcp(req: Any) -> Any:
    body = req.payload if isinstance(req.payload, dict) else {}
    method = body.get("method") or body.get("type")
    request_id = body.get("id")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}

    def reply(result: Any = None, error: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error:
            message["error"] = error
        else:
            message["result"] = result
        return 200, message

    if method in {"initialize", "init"}:
        return reply({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "dafeiyu-hub", "version": "1.0"},
        })
    if method in {"ping"}:
        return reply({})
    if method in {"tools/list", "list_tools"}:
        return reply({"tools": [tool for tool in TOOLS if _tool_ready(tool)]})
    if method in {"tools/call", "call_tool"}:
        name = str(params.get("name") or "")
        handler = HANDLERS.get(name)
        if handler is None:
            return reply(error={"code": -32601, "message": f"未知工具 {name}"})
        if not _tool_ready(next((tool for tool in TOOLS if tool["name"] == name), {})):
            return reply(error={"code": -32603, "message": f"{name} 依赖的能力不在线"})
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            success, result = handler(arguments)
        except Exception as error:  # noqa: BLE001
            return reply(error={"code": -32603, "message": str(error)})
        if not success:
            return reply(error={"code": -32602, "message": str(result)})
        return reply({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False})
    return reply(error={"code": -32601, "message": f"未知方法 {method}"})


def handle_manage(req: Any) -> Any:
    """启动 / 停止 / 重启本机服务：POST /manage {"name": "...", "action": "start|stop|restart"}"""
    body = req.payload if isinstance(req.payload, dict) else {}
    name = str(body.get("name") or "").strip()
    action = str(body.get("action") or "").strip()
    result = manage.run(name, action)
    if not result.get("ok"):
        return fail(str(result.get("message") or "操作失败"), 400)
    return ok(result)


def handle_health(_req: Any) -> Any:
    caps = _capabilities()
    return ok({
        "service": "hub",
        "online": [row["name"] for row in caps["services"] if row["online"]],
        "optional_missing": caps["optional_missing"],
        "tools": [tool["name"] for tool in TOOLS if _tool_ready(tool)],
    })


routes = {
    "GET /": handle_ui,
    "GET /ui": handle_ui,
    "GET /view": handle_view,
    "GET /capabilities": handle_capabilities,
    "GET /tools": handle_tools,
    "GET /health": handle_health,
    "GET /bot/config": botctl.handle_get,
    "POST /bot/config": botctl.handle_save,
    "GET /bot/commands": botctl.handle_commands,
    "GET /bot/groups": botctl.handle_groups,
    "GET /bot/group/tools": botctl.handle_group_tools,
    "POST /bot/group/tools": botctl.handle_group_tools_save,
    "GET /bot/tools/access": botctl.handle_tool_access,
    "POST /bot/tools/access": botctl.handle_tool_access_save,
    "POST /bot/tools/level": botctl.handle_tool_level,
    "GET /bot/human_sim": botctl.handle_human_sim,
    "POST /bot/human_sim": botctl.handle_human_sim_save,
    "POST /bot/admin/profile": botctl.handle_admin_profile,
    "POST /bot/admin/remove": botctl.handle_admin_remove,
    "GET /bot/friends": botctl.handle_friends,
    "POST /bot/group/leave": botctl.handle_leave,
    "POST /bot/friend/delete": botctl.handle_friend_delete,
    "POST /bot/persona": botctl.handle_persona,
    "POST /bot/model": botctl.handle_model,
    "GET /bot/voices": botctl.handle_voices,
    "GET /bot/voices/audio": botctl.handle_voice_audio,
    "POST /bot/voices": botctl.handle_voice,
    "POST /bot/tts": botctl.handle_tts,
    "GET /pet/config": petctl.handle_get,
    "POST /pet/config": petctl.handle_save,
    "GET /pet/relay": petctl.handle_relay_get,
    "POST /pet/relay": petctl.handle_relay_save,
    "POST /pet/control": petctl.handle_control,
    "GET /pet/power": petctl.handle_power_get,
    "POST /pet/power": petctl.handle_power,
    "GET /windows": windows.handle_list,
    "POST /windows/start": windows.handle_start,
    "POST /windows/stop": windows.handle_stop,
    "POST /mcp": handle_mcp,
    "POST /manage": handle_manage,
}
