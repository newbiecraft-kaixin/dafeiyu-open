"""只读信息接口：provider 与状态。"""
from __future__ import annotations

from typing import Any

from infra import ok

from .state import _recent_errors, router


def handle_providers(req) -> Any:  # noqa: ANN001
    return ok(router.public_providers())


def handle_status(req) -> Any:  # noqa: ANN001
    return ok(
        {
            "providers": list(router.providers),
            "routes": sorted(router.routes),
            "fallbacks": list(router.fallbacks),
            "recent_errors": list(_recent_errors),
        }
    )
