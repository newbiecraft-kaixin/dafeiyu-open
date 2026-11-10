"""model 的 HTTP 路由表。"""
from __future__ import annotations

from .complete import handle_complete
from .info import handle_providers, handle_status
from .route import handle_route

routes = {
    "POST /complete": handle_complete,
    "POST /route": handle_route,
    "GET /providers": handle_providers,
    "GET /status": handle_status,
}
