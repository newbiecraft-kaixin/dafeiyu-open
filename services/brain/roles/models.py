"""角色、凭据、身份绑定与解析结果的数据类型。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .constants import CREDENTIAL_ACCOUNT


@dataclass(frozen=True)
class Credential:
    mode: str = CREDENTIAL_ACCOUNT
    # api_key 模式下要读取的环境变量名
    env: str = ""

    def as_public(self) -> Dict[str, str]:
        return {"mode": self.mode, "env": self.env}


@dataclass(frozen=True)
class Role:
    name: str
    display: str
    profile: str
    credential: Credential
    model: str
    reasoning_effort: str
    persona_files: Tuple[Path, ...]
    workspace: Path

    def as_public(self, root: Path) -> Dict[str, Any]:
        def rel(p: Path) -> str:
            try:
                return str(p.relative_to(root))
            except ValueError:
                return str(p)

        return {
            "name": self.name,
            "display": self.display,
            "profile": self.profile,
            "credential": self.credential.as_public(),
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "persona_files": [rel(p) for p in self.persona_files],
            "workspace": rel(self.workspace),
        }


@dataclass(frozen=True)
class Identity:
    role: str
    platform: str = "*"
    user_id: str = ""
    name: str = ""
    chat_id: str = ""
    note: str = ""

    def matches(self, platform: str, user_id: str, name: str, chat_id: str) -> int:
        """返回匹配分（0 表示不匹配），分数越高优先级越高。"""
        if self.platform not in ("*", platform):
            return 0
        if self.chat_id and self.chat_id != chat_id:
            return 0
        if self.user_id and self.user_id == user_id:
            return 4 if self.chat_id else 3
        if not self.user_id and self.name and self.name == name:
            return 2 if self.chat_id else 1
        return 0


@dataclass(frozen=True)
class Route:
    role: Role
    reason: str
    is_admin: bool = False
    identity: Optional[Identity] = None

    def as_public(self, root: Path) -> Dict[str, Any]:
        return {
            "role": self.role.name,
            "display": self.role.display,
            "profile": self.role.profile,
            "reason": self.reason,
            "is_admin": self.is_admin,
        }
