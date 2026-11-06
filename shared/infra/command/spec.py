"""指令定义与解析结果。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .constants import MENU_NORMAL, PLATFORM_ALL, ROLE_ADMIN, ROLE_USER
from .context import CommandContext
from .result import CommandResult


@dataclass
class Command:
    name: str
    handler: Callable[[CommandContext], CommandResult]
    help: str = ""
    usage: str = ""
    aliases: Tuple[str, ...] = ()
    scope: str = PLATFORM_ALL
    role: str = ROLE_USER
    menu: str = MENU_NORMAL
    order: int = 100
    hidden: bool = False

    def allows(self, platform: str, is_admin: bool) -> Tuple[bool, str]:
        if self.scope not in (PLATFORM_ALL, platform):
            return False, f"该指令只在 {self.scope} 可用"
        if self.role == ROLE_ADMIN and not is_admin:
            return False, "需要管理员权限"
        return True, ""


@dataclass
class Resolution:
    ok: bool
    args: str = ""
    command: Optional[Command] = None
    reason: str = ""

    @property
    def argv(self) -> List[str]:
        return self.args.split()
