"""服务间 HTTP 调用：复用 infra.keepalive 长连接，能解析 JSON 就返回对象。"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from infra import keepalive

from .config import TIMEOUT


def http_call(method: str, url: str, payload: Any = None, timeout: float = TIMEOUT,
              headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
    try:
        # 服务间复用长连接：探测与调用都不再每次重新建 TCP
        status, raw = keepalive.request(method, url, payload, headers=headers, timeout=timeout)
    except keepalive.KeepAliveError as error:
        status, raw = error.status, error.body
    except (OSError, TimeoutError, ValueError) as error:
        return 0, {"error": str(error)}
    if raw[:1] in (b"{", b"["):
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return status, raw
