"""控制台单页：仪表盘 HTML 放在 dashboard.html，这里只负责读取与响应。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from infra import Html

HTML_PATH = Path(__file__).resolve().parent / "dashboard.html"
_cache: dict[str, Any] = {"mtime": None, "html": ""}


def _html() -> str:
    """按文件 mtime 热加载：改完 dashboard.html 不用重启中枢也能立即看到新前端。"""
    stat = HTML_PATH.stat()
    if _cache["mtime"] != stat.st_mtime_ns:
        _cache["html"] = HTML_PATH.read_text(encoding="utf-8")
        _cache["mtime"] = stat.st_mtime_ns
    return _cache["html"]


def handle_ui(_req: Any) -> Any:
    return 200, Html(_html())
