"""路由装配、Handler、Server 与启动函数。"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from ..config import HOST
from .pages import data_page
from .responses import fail
from .types import Blob, Html, Request, Route, Stats


def _normalize_routes(routes: Mapping[str, Route]) -> Dict[str, Route]:
    out: Dict[str, Route] = {}
    for key, fn in routes.items():
        parts = key.strip().split(None, 1)
        if len(parts) == 1:
            method, path = "GET", parts[0]
        else:
            method, path = parts
        if not path.startswith("/"):
            path = "/" + path
        out[f"{method.upper()} {path}"] = fn
    return out


def _match_wildcard(table: Dict[str, Route], method: str, path: str) -> Optional[Route]:
    """支持 "GET /images/*" 这类前缀路由（用于按文件名取图）。"""
    for key, fn in table.items():
        meth, _, pattern = key.partition(" ")
        if meth != method or not pattern.endswith("/*"):
            continue
        if path.startswith(pattern[:-1]):
            return fn
    return None


def make_handler(
    service: str,
    routes: Mapping[str, Route],
    *,
    stats: Optional[Stats] = None,
    version: str = "1.0",
    cors: str = "*",
) -> type:
    """生成一个绑定好路由的 handler 类。"""
    table = _normalize_routes(routes)
    counts = stats or Stats()
    log = logging.getLogger(f"io_kit.{service}")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"{service}/{version}"

        # ---- 基础 ----
        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s %s", self.client_address[0], fmt % args)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._respond(204, None)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def _wants_html(self) -> bool:
            """浏览器地址栏打开（Accept 含 text/html）才转成页面，脚本调用不受影响。"""
            if self.command != "GET":
                return False
            return "text/html" in (self.headers.get("Accept") or "").lower()

        # ---- 内部 ----
        def _dispatch(self, method: str) -> None:
            try:
                req = self._build_request(method)
            except ValueError as exc:
                self._respond(*fail(f"请求体解析失败: {exc}", 400))
                return

            fn = table.get(f"{method} {req.path}")
            if fn is None:
                fn = _match_wildcard(table, method, req.path)
            if fn is None:
                # /health 与 /stats 走平铺 JSON，方便监控页直接读字段
                if method == "GET" and req.path == "/health":
                    fn = lambda r: (200, self._health())  # noqa: E731
                elif method == "GET" and req.path == "/stats":
                    fn = lambda r: (200, counts.snapshot())  # noqa: E731
                else:
                    counts.inc("http_404")
                    counts.inc(f"404 {method} {req.path}")
                    self._respond(*fail(f"未知接口 {method} {req.path}", 404))
                    return

            started = time.perf_counter()
            try:
                status, data = fn(req)
            except Exception as exc:  # noqa: BLE001 - 兜底任何业务异常
                counts.inc("http_500")
                log.error("接口异常 %s %s: %s\n%s", method, req.path, exc, traceback.format_exc())
                self._respond(*fail(f"内部错误: {exc}", 500))
                return
            finally:
                counts.inc("http_requests")
                counts.inc(f"route {method} {req.path}")

            ms = int((time.perf_counter() - started) * 1000)
            counts.inc("http_ms_total", ms)
            self._respond(status, data)

        def _read_body(self) -> bytes:
            """读请求体：兼容 Content-Length 与 Transfer-Encoding: chunked。

            NapCat 的事件上报走 chunked，没有 Content-Length；旧实现只认长度，
            导致 200+ 条群消息在 handler 之前就被当成「空事件」丢掉。
            """
            encoding = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in encoding:
                chunks: list[bytes] = []
                while True:
                    line = self.rfile.readline(65536)
                    if not line:
                        break
                    size_text = line.split(b";", 1)[0].strip()
                    try:
                        size = int(size_text, 16)
                    except ValueError as exc:
                        raise ValueError(f"chunk 长度非法: {size_text!r}") from exc
                    if size == 0:
                        while True:
                            trailer = self.rfile.readline(65536)
                            if trailer in (b"\r\n", b"\n", b""):
                                break
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline(65536)
                return b"".join(chunks)

            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _build_request(self, method: str) -> Request:
            parsed = urlparse(self.path)
            path = parsed.path or "/"
            if len(path) > 1 and path.endswith("/"):
                path = path.rstrip("/")
            query = {
                k: v[-1]
                for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
            }

            payload: Any = None
            raw = self._read_body()
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8-sig"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ValueError(str(exc)) from exc

            return Request(
                method=method,
                path=path,
                query=query,
                payload=payload,
                headers={k: v for k, v in self.headers.items()},
                client=self.client_address[0] if self.client_address else "",
            )

        def _respond(self, status: int, data: Any) -> None:
            if data is None:
                body = b""
            elif isinstance(data, Html):
                body = str(data).encode("utf-8")
                content_type = "text/html; charset=utf-8"
            elif isinstance(data, Blob):
                body = bytes(data)
                content_type = data.content_type
            elif isinstance(data, (dict, list, int, float, bool)):
                # 浏览器直接打开 /status、/events 这类接口时给一页可读的表格；
                # 程序调用（Accept 不含 text/html）还是原样的 JSON。
                if self._wants_html():
                    body = str(data_page(service, self.path, status, data)).encode("utf-8")
                    content_type = "text/html; charset=utf-8"
                else:
                    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
            elif isinstance(data, bytes):
                body = data
                content_type = "application/octet-stream"
            else:
                body = str(data).encode("utf-8")
                content_type = "text/plain; charset=utf-8"

            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type if body else "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # 中枢这类状态页/接口不允许缓存，否则浏览器会一直显示旧的空列表
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", cors)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
                self.send_header("Access-Control-Max-Age", "600")
                self.end_headers()
                if body:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _health(self) -> Dict[str, Any]:
            snap = counts.snapshot()
            return {
                "ok": True,
                "service": service,
                "version": version,
                "uptime_s": snap.pop("uptime_s"),
                "stats": snap,
            }

    return Handler


class _QuietHTTPServer(ThreadingHTTPServer):
    """长轮询客户端断开是常态（10053/10054），不要打整段 traceback。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def serve(
    port: int,
    routes: Mapping[str, Route],
    *,
    service: str = "io-svc",
    version: str = "1.0",
    host: str = HOST,
    stats: Optional[Stats] = None,
    cors: str = "*",
) -> ThreadingHTTPServer:
    """创建并返回（未启动的）服务器。端口传 0 由系统分配。"""
    handler = make_handler(service, routes, stats=stats, version=version, cors=cors)
    server = _QuietHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


class ServiceHandle:
    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread, service: str) -> None:
        self.server = server
        self.thread = thread
        self.service = service

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.server.server_address[0]}:{self.port}"

    def stop(self, timeout: float = 3.0) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout)


def serve_in_thread(
    port: int,
    routes: Mapping[str, Route],
    *,
    service: str = "io-svc",
    version: str = "1.0",
    host: str = HOST,
    stats: Optional[Stats] = None,
    cors: str = "*",
) -> ServiceHandle:
    server = serve(port, routes, service=service, version=version, host=host, stats=stats, cors=cors)
    thread = threading.Thread(target=server.serve_forever, name=f"{service}-http", daemon=True)
    thread.start()
    logging.getLogger(f"io_kit.{service}").info("监听 %s:%d", host, server.server_address[1])
    return ServiceHandle(server, thread, service)


def run(
    port: int,
    routes: Mapping[str, Route],
    *,
    service: str = "io-svc",
    version: str = "1.0",
    host: str = HOST,
    stats: Optional[Stats] = None,
    cors: str = "*",
) -> None:
    """阻塞运行，Ctrl+C 退出。服务主入口用这个。"""
    server = serve(port, routes, service=service, version=version, host=host, stats=stats, cors=cors)
    logging.getLogger(f"io_kit.{service}").info("监听 %s:%d", host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
