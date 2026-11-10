"""会话工具档位（tool_gate 的 access.json，群/好友会话生效）。"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ._admins import DATA, _LOCK, _as_list, _read_json, _write_json

ACCESS_PATH = DATA / "local_tools" / "access.json"
TOOL_NAMES_PATH = DATA / "local_tools" / "tool_names.json"

_TOOL_LEVELS = ("full", "standard", "lite", "media", "admin", "info", "guest", "none", "custom")
_TOOL_LEVEL_LABEL = {
    "full": "全量",
    "standard": "标准",
    "lite": "精简",
    "media": "表情语音",
    "admin": "群管通知",
    "info": "只读查询",
    "guest": "陌生人",
    "none": "禁用",
    "custom": "自定义",
}
_TOOL_LEVEL_DESC = {
    "full": "全量工具，适合 DeepSeek 等云端强模型",
    "standard": "日常聊天：人设白名单，没设白名单时收敛到常用工具",
    "lite": "小模型精简：表情/语音/时间/联网/天气/头像/记忆",
    "media": "只要活跃：表情包/超级表情/语音/唱歌/贴表情/+1",
    "admin": "群管通知：提醒/入退群通知/群成员/跨群发送/私聊",
    "info": "只读查询：时间/天气/联网/头像/长期记忆/空间动态",
    "guest": "陌生人：只聊天，仅时间/天气/头像",
    "none": "禁用所有工具",
    "custom": "自定义白名单，按勾选下发工具",
}
_TOOL_ROLES = ("group", "friend", "stranger")
_LEVEL_ID_RE = re.compile(r"[a-z0-9_-]{2,24}")


def _custom_levels(access: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """页面新建的档位：access.json 的 levels = {ID: {label, desc, tools}}。"""
    raw = access.get("levels")
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, item in raw.items():
        level = str(key or "").strip()
        if not level or level in _TOOL_LEVELS or not isinstance(item, dict):
            continue
        out[level] = {
            "label": str(item.get("label") or level).strip()[:24] or level,
            "desc": str(item.get("desc") or "").strip()[:160],
            "tools": _as_list(item.get("tools")),
        }
    return out


def _known_levels(access: Dict[str, Any]) -> set:
    return set(_TOOL_LEVELS) | set(_custom_levels(access))


def _tool_levels_meta(access: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = [
        {"id": key, "label": _TOOL_LEVEL_LABEL[key], "desc": _TOOL_LEVEL_DESC[key]}
        for key in _TOOL_LEVELS
    ]
    for key, item in _custom_levels(access).items():
        rows.append(
            {
                "id": key,
                "label": item["label"],
                "desc": item["desc"] or f"自定义档位 · {len(item['tools'])} 个工具",
                "custom": True,
                "tools": item["tools"],
            }
        )
    return rows


def tool_access() -> Dict[str, Any]:
    """读取全局档位、角色档位、各会话配置和可用工具名。"""
    access = _read_json(ACCESS_PATH)
    groups = access.get("groups") if isinstance(access.get("groups"), dict) else {}
    roles = access.get("roles") if isinstance(access.get("roles"), dict) else {}
    try:
        names = [
            str(item).strip()
            for item in (_read_json(TOOL_NAMES_PATH).get("tools") or [])
            if str(item).strip()
        ]
    except ValueError:
        names = []
    return {
        "groups": groups,
        "tools": names,
        "default_level": str(access.get("default_level") or "standard"),
        "roles": {
            role: str(roles.get(role) or ("guest" if role == "stranger" else "standard"))
            for role in _TOOL_ROLES
        },
        "levels": _tool_levels_meta(access),
    }


def group_tools() -> Dict[str, Any]:
    """兼容旧接口：返回与 tool_access 相同的数据。"""
    return tool_access()


def save_tool_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """写入全局默认档位与角色档位（群聊 / 好友私聊 / 陌生人私聊）。"""
    default_level = payload.get("default_level")
    roles = payload.get("roles") if isinstance(payload.get("roles"), dict) else {}
    with _LOCK:
        access = _read_json(ACCESS_PATH)
        known = _known_levels(access)
        if default_level is not None:
            level = str(default_level).strip()
            if level not in known:
                raise ValueError("默认档位非法: " + level)
            access["default_level"] = level
        current = access.get("roles") if isinstance(access.get("roles"), dict) else {}
        for role in _TOOL_ROLES:
            if role not in roles:
                continue
            level = str(roles.get(role) or "").strip()
            if level not in known:
                raise ValueError("角色档位非法: " + role + "=" + level)
            current[role] = level
        access["roles"] = current
        access.setdefault("default_level", "standard")
        _write_json(ACCESS_PATH, access)
    return tool_access()


def save_group_tools(payload: Dict[str, Any]) -> Dict[str, Any]:
    """按会话写入工具档位；level 为空 = 跟随全局默认。

    group_id 兼容群号和好友 QQ，优先取 group_id，其次 session_id。
    """
    gid = str(payload.get("group_id") or payload.get("session_id") or "").strip()
    if not gid:
        raise ValueError("会话 ID 不能为空")
    level = str(payload.get("level") or "").strip()
    tools = _as_list(payload.get("tools"))
    with _LOCK:
        access = _read_json(ACCESS_PATH)
        if level and level not in _known_levels(access):
            raise ValueError("工具档位非法: " + level)
        groups = access.get("groups") if isinstance(access.get("groups"), dict) else {}
        entry = groups.get(gid)
        if not isinstance(entry, dict):
            entry = {}
        name = str(payload.get("name") or "").strip()
        if name:
            entry["name"] = name
        if level:
            entry["level"] = level
        else:
            entry.pop("level", None)
        if level == "custom":
            entry["tools"] = tools
        else:
            entry.pop("tools", None)
        groups[gid] = entry
        access["groups"] = groups
        access.setdefault("default_level", "standard")
        _write_json(ACCESS_PATH, access)
    return tool_access()


def save_tool_level(payload: Dict[str, Any]) -> Dict[str, Any]:
    """新建 / 修改 / 删除页面自定义档位，写在 access.json 的 levels 里（热生效）。"""
    action = str(payload.get("action") or "save").strip().lower()
    level = str(payload.get("id") or "").strip().lower()
    if not _LEVEL_ID_RE.fullmatch(level):
        raise ValueError("档位 ID 只能用 2-24 位小写字母、数字、- 或 _")
    if level in _TOOL_LEVELS:
        raise ValueError("内置档位不能改名或删除：" + level)
    with _LOCK:
        access = _read_json(ACCESS_PATH)
        current = _custom_levels(access)
        if action == "delete":
            if level not in current:
                raise ValueError("档位不存在: " + level)
            used = [
                str(key)
                for key, item in (access.get("groups") or {}).items()
                if isinstance(item, dict) and str(item.get("level") or "") == level
            ]
            roles = access.get("roles") if isinstance(access.get("roles"), dict) else {}
            used += [role for role, value in roles.items() if str(value or "") == level]
            if str(access.get("default_level") or "") == level:
                used.append("默认档位")
            if used:
                raise ValueError("档位仍在使用，先改成别的档位：" + "、".join(used[:6]))
            current.pop(level, None)
        else:
            label = str(payload.get("label") or "").strip()[:24] or level
            desc = str(payload.get("desc") or "").strip()[:160]
            tools = _as_list(payload.get("tools"))
            known = set(
                str(item).strip()
                for item in (_read_json(TOOL_NAMES_PATH).get("tools") or [])
                if str(item).strip()
            )
            if known:
                tools = [tool for tool in tools if tool in known]
            current[level] = {"label": label, "desc": desc, "tools": tools}
        if current:
            access["levels"] = current
        else:
            access.pop("levels", None)
        access.setdefault("default_level", "standard")
        _write_json(ACCESS_PATH, access)
    return tool_access()


# ---------- 接口处理 ----------
def handle_tool_level(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(save_tool_level(payload))
    except ValueError as error:
        return fail(str(error), 400)
