"""统一响应体：ok / fail。"""
from __future__ import annotations

from typing import Any, Tuple


def ok(data: Any = None, status: int = 200) -> Tuple[int, Any]:
    return status, {"ok": True, "data": data}


def fail(message: str, status: int = 400, **extra: Any) -> Tuple[int, Any]:
    body = {"ok": False, "error": str(message)}
    body.update(extra)
    return status, body
