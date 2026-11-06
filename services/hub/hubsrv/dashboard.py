"""控制台单页：仪表盘 HTML 放在 dashboard.html，这里只负责读取与响应。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from infra import Html

DASHBOARD_HTML = (Path(__file__).resolve().parent / "dashboard.html").read_text(encoding="utf-8")


def handle_ui(_req: Any) -> Any:
    return 200, Html(DASHBOARD_HTML)
