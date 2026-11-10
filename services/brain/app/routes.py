"""brain 的 HTTP 路由表。"""
from __future__ import annotations

from .chat import handle_chat, handle_event
from .identity import handle_route, handle_roles
from .memory_api import handle_memory_delete, handle_memory_get, handle_memory_post
from .status import handle_capabilities, handle_status

routes = {
    "GET /roles": handle_roles,
    "POST /route": handle_route,
    "POST /chat": handle_chat,
    "POST /event": handle_event,
    "GET /memory": handle_memory_get,
    "POST /memory": handle_memory_post,
    "DELETE /memory": handle_memory_delete,
    "GET /status": handle_status,
    "GET /capabilities": handle_capabilities,
}
