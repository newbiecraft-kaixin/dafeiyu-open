"""极简 ASGI 鉴权。"""


class _BearerAuth:
    """极简 ASGI 鉴权：非 HTTP 请求（lifespan）直接放行，/health 免鉴权。"""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == "/health":
            body = b'{"ok":true}'
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        headers = {}
        for key, value in scope.get("headers") or []:
            try:
                headers[key.decode("latin-1").lower()] = value.decode("latin-1")
            except Exception:  # noqa: BLE001
                continue
        if headers.get("authorization", "") != f"Bearer {self.token}":
            body = b'{"error":"unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
