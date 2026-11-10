"""command：指令体系（原 commands.py 拆分）。

- constants.py  平台 / 角色 / 菜单常量
- context.py    指令上下文与文本解析
- result.py     指令结果
- spec.py       指令定义与解析结果
- registry.py   注册、加载、查询与菜单渲染
"""
from __future__ import annotations

from .constants import (
    MENU_ADMIN,
    MENU_NORMAL,
    PLATFORM_ALL,
    ROLE_ADMIN,
    ROLE_USER,
)
from .context import CommandContext, parse_command
from .registry import CommandRegistry
from .result import CommandResult
from .spec import Command, Resolution

__all__ = [
    "PLATFORM_ALL",
    "ROLE_USER",
    "ROLE_ADMIN",
    "MENU_NORMAL",
    "MENU_ADMIN",
    "CommandContext",
    "parse_command",
    "CommandResult",
    "Command",
    "Resolution",
    "CommandRegistry",
]
