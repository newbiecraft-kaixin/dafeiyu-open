"""通用入参解析、身份路由与日志。"""
from __future__ import annotations

import time
from typing import Any, Dict, Tuple

from .state import config


def _log(*args: Any) -> None:
    print(f"[brain {time.strftime('%H:%M:%S')}]", *args, flush=True)


def _parts(payload: Dict[str, Any]) -> Dict[str, str]:
    """同时兼容扁平入参与 {actor, chat} 嵌套入参。"""
    actor = payload.get("actor")
    actor = actor if isinstance(actor, dict) else payload
    chat = payload.get("chat")
    chat = chat if isinstance(chat, dict) else {}

    out = {
        "platform": str(actor.get("platform") or payload.get("platform") or "").strip().lower(),
        "user_id": str(actor.get("user_id") or actor.get("id") or "").strip(),
        "name": str(actor.get("name") or actor.get("nickname") or "").strip(),
        "chat_id": str(chat.get("id") or payload.get("chat_id") or "").strip(),
        "chat_type": str(chat.get("type") or payload.get("chat_type") or "group").strip().lower(),
        "chat_name": str(chat.get("name") or payload.get("chat_name") or "").strip(),
    }
    return out


def _resolve_route(payload: Dict[str, Any]) -> Tuple[Any, Dict[str, str]]:
    parts = _parts(payload)
    if not parts["platform"]:
        raise ValueError("缺少 platform")
    if parts["chat_type"] not in ("group", "private", "channel"):
        raise ValueError(f"chat_type 非法: {parts['chat_type']}")
    route = config.resolve(
        platform=parts["platform"],
        user_id=parts["user_id"],
        name=parts["name"],
        chat_id=parts["chat_id"],
    )
    return route, parts


def _role_override(payload: Dict[str, Any]):  # type: ignore[no-untyped-def]
    name = str(payload.get("role") or "").strip()
    return config.role(name) if name else None
