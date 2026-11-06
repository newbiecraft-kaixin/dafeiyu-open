"""HTTP 基座的数据类型：响应标记、请求对象与线程安全计数器。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Tuple

Route = Callable[["Request"], Tuple[int, Any]]


class Html(str):
    """标记一段字符串为 HTML 文档，响应时使用 text/html。"""

    __slots__ = ()


class Blob(bytes):
    """标记一段字节为带 Content-Type 的二进制响应（图片、文件等）。

    bytes 子类不能带非空 __slots__，这里用普通属性。
    """

    def __new__(cls, data: bytes, content_type: str = "application/octet-stream") -> "Blob":
        obj = super().__new__(cls, data)
        obj.content_type = content_type
        return obj


class Stats:
    """轻量计数器，线程安全。"""

    def __init__(self) -> None:
        self._counters: Dict[str, int] = {}
        self._lock = threading.Lock()
        self.started_at = time.time()

    def inc(self, key: str, n: int = 1) -> int:
        with self._lock:
            value = self._counters.get(key, 0) + n
            self._counters[key] = value
            return value

    def get(self, key: str) -> int:
        with self._lock:
            return self._counters.get(key, 0)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            data = dict(self._counters)
        data["uptime_s"] = round(time.time() - self.started_at, 1)
        return data


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: Dict[str, str]
    payload: Any
    headers: Dict[str, str] = field(default_factory=dict)
    client: str = ""

    def q(self, name: str, default: Any = None) -> Any:
        return self.query.get(name, default)
