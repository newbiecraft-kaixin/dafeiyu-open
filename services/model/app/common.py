"""通用日志与入参提取。"""
from __future__ import annotations

import time
from typing import Any, Dict


def _log(*args: Any) -> None:
    print(f"[model {time.strftime('%H:%M:%S')}]", *args, flush=True)


def _payload(req) -> Dict[str, Any]:  # noqa: ANN001
    return req.payload if isinstance(req.payload, dict) else {}
