"""角色与身份解析相关路由。"""
from __future__ import annotations

from typing import Any

import roles as roles_mod
from infra import fail, ok

from .common import _resolve_route
from .state import config, counts


def handle_roles(req) -> Any:  # noqa: ANN001
    return ok(config.summary())


def handle_route(req) -> Any:  # noqa: ANN001
    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        route, parts = _resolve_route(payload)
    except (ValueError, roles_mod.RolesError) as exc:
        return fail(str(exc), 400)
    counts.inc("route")
    data = route.as_public(config.project_root)
    data.update({"platform": parts["platform"], "chat_id": parts["chat_id"]})
    return ok(data)
