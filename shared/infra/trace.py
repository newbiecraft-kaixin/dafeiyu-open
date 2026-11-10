"""链路追踪：请求进来时确定 trace_id，出站请求自动带上同一个 id。"""
from __future__ import annotations

import threading
from typing import Dict, Mapping, Optional

from .protocol import make_trace_id

_local = threading.local()


def current() -> str:
    """当前线程正在处理的 trace_id；没有正在处理的请求时返回空串。"""
    return getattr(_local, "trace_id", "")


def inbound(headers: Mapping[str, str], platform: str = "svc") -> str:
    """从请求头取 trace_id，没有就新生成，并设为当前线程的链路 id。"""
    trace_id = ""
    for name, value in headers.items():
        if name.lower() == "x-trace-id":
            trace_id = str(value).strip()
            break
    if not trace_id:
        trace_id = make_trace_id(platform)
    _local.trace_id = trace_id
    return trace_id


def headers(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """出站请求头：调用方没显式指定时补上当前 trace_id。"""
    merged = dict(base or {})
    trace_id = current()
    if trace_id and not any(name.lower() == "x-trace-id" for name in merged):
        merged["X-Trace-Id"] = trace_id
    return merged
