"""web：HTTP 基座（原 httpserver.py 拆分）。

- types.py       响应标记 / 请求对象 / 计数器
- responses.py   ok / fail 统一返回体
- pages.py       把 JSON 渲染成浏览器可读页面
- server.py      路由装配、Handler、Server 与启动函数
"""
from __future__ import annotations

from .pages import data_page
from .responses import fail, ok
from .server import (
    ServiceHandle,
    make_handler,
    run,
    serve,
    serve_in_thread,
)
from .types import Blob, Html, Request, Route, Stats

__all__ = [
    "Route",
    "Html",
    "Blob",
    "Stats",
    "Request",
    "ok",
    "fail",
    "data_page",
    "make_handler",
    "serve",
    "serve_in_thread",
    "ServiceHandle",
    "run",
]
