"""hubsrv：hub 服务的实现包（原 service.py 按职责拆分）。

- config.py       端口 / 分层 / 音色 / 路径常量
- http_client.py  复用长连接的服务间 HTTP 调用
- registry.py     服务清单扫描、在线探测、能力聚合
- catalog.py      MCP 工具目录（纯数据）
- tools.py        工具调用实现与分发表
- views.py        /view 的跳转与数据渲染
- dashboard.py    控制台页面（读取 dashboard.html）
- api.py          路由与处理函数

依赖方向：catalog <- registry <- tools / api，api 汇总全部。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 直接 import hubsrv（不经过 service.py）时也能找到 shared/infra。
_SHARED = Path(__file__).resolve().parents[3] / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from .api import (
    handle_capabilities,
    handle_health,
    handle_mcp,
    handle_tools,
    routes,
)
from .catalog import TOOLS
from .config import PORT
from .dashboard import DASHBOARD_HTML, handle_ui
from .tools import HANDLERS
from .views import handle_view

__all__ = [
    "PORT",
    "routes",
    "handle_ui",
    "handle_view",
    "handle_capabilities",
    "handle_tools",
    "handle_mcp",
    "handle_health",
    "TOOLS",
    "HANDLERS",
    "DASHBOARD_HTML",
]
