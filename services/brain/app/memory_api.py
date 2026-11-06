"""统一记忆接口：长期事实与最近消息。"""
from __future__ import annotations

from typing import Any

from infra import fail, ok

from .state import counts, store


def handle_memory_get(req) -> Any:  # noqa: ANN001
    scope = str(req.q("scope") or "").strip()
    if not scope:
        return fail("缺少 scope", 400)
    limit = int(req.q("limit") or 12)
    counts.inc("memory_read")
    return ok(
        {
            "scope": scope,
            "facts": store.recall(scope, str(req.q("q") or ""), 8),
            "messages": store.recent_messages(scope, limit),
        }
    )


def handle_memory_post(req) -> Any:  # noqa: ANN001
    payload = req.payload if isinstance(req.payload, dict) else {}
    scope = str(payload.get("scope") or "").strip()
    key = str(payload.get("key") or "").strip()
    value = str(payload.get("value") or "").strip()
    if not scope or not key or not value:
        return fail("scope、key、value 不能为空", 400)
    try:
        row = store.remember(scope, key, value, float(payload.get("importance") or 1.0))
    except ValueError as exc:
        return fail(str(exc), 400)
    counts.inc("memory_write")
    return ok(row)


def handle_memory_delete(req) -> Any:  # noqa: ANN001
    scope = str(req.q("scope") or "").strip()
    key = str(req.q("key") or "").strip()
    if not scope or not key:
        return fail("缺少 scope 或 key", 400)
    return ok({"deleted": store.forget(scope, key)})
