"""POST /complete 处理。"""
from __future__ import annotations

import time
from typing import Any

from infra import fail, ok
from router import ModelError, complete

from .common import _payload
from .state import LOG, _recent_errors, counts, router


def handle_complete(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    try:
        result = complete(body, router=router)
    except ModelError as exc:
        counts.inc("complete_failed")
        msg = str(exc)[:300]
        _recent_errors.append({"ts": time.time(), "error": msg})
        LOG.warning("complete failed: %s", msg)
        return fail(msg, 502)
    counts.inc("complete")
    counts.inc(f"provider {result['provider']}")
    return ok(result)
