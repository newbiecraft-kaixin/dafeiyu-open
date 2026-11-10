"""POST /route 处理。"""
from __future__ import annotations

from typing import Any

from infra import fail, ok
from router import ModelError

from .common import _payload
from .state import counts, router


def handle_route(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    try:
        route = router.resolve(
            str(body.get("role") or ""),
            provider=str(body.get("provider") or ""),
            model=str(body.get("model") or ""),
        )
    except ModelError as exc:
        return fail(str(exc), 400)
    counts.inc("route")
    return ok(route.as_public())
