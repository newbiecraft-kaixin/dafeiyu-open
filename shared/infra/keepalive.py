"""HTTP/1.1 长连接复用客户端（纯标准库）。

服务之间以前每次调用都用 urllib.urlopen，等于每条消息都重新建 TCP/TLS 连接。
这里按 (scheme, host, port) 复用一个 HTTPConnection，请求完不关连接，
连接失效时自动重建并重试一次；同一主机的并发请求用独立锁串行，互不影响。
"""
from __future__ import annotations

import http.client
import json
import threading
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit


class KeepAliveError(RuntimeError):
    """HTTP 状态码 >= 400 时抛出，保留状态码和响应体。"""

    def __init__(self, status: int, body: bytes) -> None:
        preview = body[:200].decode("utf-8", "replace")
        super().__init__(f"HTTP {status}: {preview}")
        self.status = status
        self.body = body


_POOL: Dict[Tuple[str, str, int], Tuple[http.client.HTTPConnection, threading.Lock]] = {}
_GUARD = threading.Lock()


def _split(url: str) -> Tuple[Tuple[str, str, int], str]:
    parts = urlsplit(url)
    scheme = parts.scheme or "http"
    if scheme not in ("http", "https"):
        raise ValueError(f"不支持的协议: {scheme}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return (scheme, host, port), path


def _connect(key: Tuple[str, str, int], timeout: float) -> http.client.HTTPConnection:
    scheme, host, port = key
    factory = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return factory(host, port, timeout=timeout)


def _slot(key: Tuple[str, str, int], timeout: float) -> Tuple[http.client.HTTPConnection, threading.Lock]:
    with _GUARD:
        slot = _POOL.get(key)
        if slot is None:
            slot = (_connect(key, timeout), threading.Lock())
            _POOL[key] = slot
        return slot


def _drop(key: Tuple[str, str, int]) -> None:
    with _GUARD:
        slot = _POOL.pop(key, None)
    if slot is not None:
        try:
            slot[0].close()
        except Exception:  # noqa: BLE001
            pass


def close_all() -> None:
    """进程退出前关闭所有复用的连接。"""
    with _GUARD:
        conns = list(_POOL.values())
        _POOL.clear()
    for conn, _ in conns:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def request(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> Tuple[int, bytes]:
    """发一个请求并复用连接；返回 (status, body)。"""
    key, path = _split(url)
    body = None
    send_headers = {"Accept": "application/json", "Connection": "keep-alive"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        send_headers["Content-Type"] = "application/json"
    for name, value in (headers or {}).items():
        if name.lower() == "content-type" and payload is not None:
            continue
        send_headers[name] = value
    for attempt in range(2):
        conn, lock = _slot(key, timeout)
        try:
            with lock:
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                conn.request(method, path, body=body, headers=send_headers)
                response = conn.getresponse()
                status = response.status
                raw = response.read()
                if response.will_close:
                    _drop(key)
            if status >= 400:
                raise KeepAliveError(status, raw)
            return status, raw
        except KeepAliveError:
            raise
        except (http.client.HTTPException, OSError) as error:
            _drop(key)
            if attempt == 1:
                raise
    raise AssertionError("unreachable")


def request_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> Any:
    """请求并解析 JSON；空响应返回 {}。"""
    _, raw = request(method, url, payload, headers=headers, timeout=timeout)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", "replace"))
