"""指令上下文与文本解析。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .constants import _PREFIXES


@dataclass
class CommandContext:
    platform: str
    text: str
    chat_id: str = ""
    chat_type: str = "group"
    chat_name: str = ""
    sender_id: str = ""
    sender_name: str = ""
    is_admin: bool = False
    args: str = ""
    argv: List[str] = field(default_factory=list)
    envelope: Optional[Dict[str, Any]] = None


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    """'/管理' -> ('管理', '')；不是指令返回 None。"""
    s = (text or "").strip()
    if not s or s[0] not in _PREFIXES:
        return None
    body = s[1:].strip()
    if not body:
        return None
    parts = body.split(None, 1)
    name = parts[0].strip()
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args
