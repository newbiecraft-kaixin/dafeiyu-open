"""roles 配置解析用的小工具。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

from .errors import RolesError


def _s(value: Any) -> str:
    return str(value or "").strip()


def _texts(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    raise RolesError(f"期望字符串或数组，实际 {type(value).__name__}")


def _resolve_path(value: Any, root: Path) -> Path:
    p = Path(_s(value)).expanduser()
    return p if p.is_absolute() else (root / p)
