"""控制台的 /view：本机网页原样跳转，JSON 渲染成表格，打不开给说明页。"""
from __future__ import annotations

import html as _html
import json
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import urlparse

from infra import Html, data_page, fail, port_of

VIEW_TIMEOUT = 6.0
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _view_url(raw: str) -> Optional[str]:
    """只允许转到本机，避免 /view 被当成任意 URL 代理。"""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in _LOOPBACK:
        return None
    return raw


def _open_page(target: str) -> Html:
    """目标本身是网页：用 meta 跳过去，原页面保持完整样式。"""
    safe = _html.escape(target, quote=True)
    return Html(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0;url={safe}">'
        f"<title>正在打开 {safe}</title></head>"
        '<body style="margin:0;background:#070a14;color:#e9edff;font:14px system-ui;padding:40px">'
        f'<p>正在打开 <a style="color:#9fbcff" href="{safe}">{safe}</a> …</p></body></html>'
    )


def _view_error(target: str, headline: str, detail: str) -> Html:
    safe_target = _html.escape(target, quote=True)
    hub = f"http://127.0.0.1:{port_of('hub', 23408)}/ui"
    return Html(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_html.escape(headline)}</title>"
        '<style>body{margin:0;min-height:100vh;background:#070a14;color:#e9edff;'
        'font:14px/1.7 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;display:grid;place-items:center}'
        ".box{max-width:580px;padding:28px 30px;border:1px solid rgba(102,126,182,.35);"
        "border-radius:16px;background:#101731}"
        "h1{margin:0 0 10px;font-size:19px;color:#ffc2cd}p{margin:0;color:#8b97bd;line-height:1.7}"
        ".url{margin:14px 0;font-family:Consolas,monospace;font-size:12.5px;color:#bfd0ff;"
        "word-break:break-all}"
        "a{display:inline-block;margin-top:18px;padding:9px 16px;border-radius:10px;"
        "border:1px solid rgba(107,143,255,.45);background:rgba(107,143,255,.12);"
        "color:#d7e2ff;text-decoration:none;margin-right:8px}"
        "a:hover{background:rgba(107,143,255,.24)}</style></head>"
        f'<body><div class="box"><h1>{_html.escape(headline)}</h1>'
        f'<div class="url">{safe_target}</div><p>{_html.escape(detail)}</p>'
        f'<a href="{hub}">← 返回中枢</a><a href="{safe_target}" target="_blank">直接打开</a>'
        "</div></body></html>"
    )


def handle_view(req: Any) -> Any:
    """控制台里的每个链接都先到这里：网页原样跳转，JSON 变成表格，打不开给说明页。"""
    target = _view_url(str(req.q("target") or "").strip())
    if target is None:
        return fail("参数 target 必须是本机 http://127.0.0.1 地址", 400)
    try:
        request = urllib.request.Request(
            # 明确要 JSON：若带上 text/html，Python 服务会直接把 JSON 渲染成网页，
            # /view 就只能原样跳转，反而看不出这是个数据接口。
            target, headers={"Accept": "application/json, */*;q=0.5"}
        )
        with urllib.request.urlopen(request, timeout=VIEW_TIMEOUT) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            body = response.read(800_000)
    except urllib.error.HTTPError as error:
        return 200, _view_error(
            target, f"打不开（HTTP {error.code}）",
            f"{error.reason}；这个地址可能需要登录，或者路径本身不对。",
        )
    except Exception as error:  # noqa: BLE001
        return 200, _view_error(
            target, "这个页面现在打不开",
            f"端口没有响应（{type(error).__name__}），对应的服务可能没启动。",
        )
    if "text/html" in content_type:
        return 200, _open_page(target)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 200, _open_page(target)
    parsed = urlparse(target)
    title = f"{parsed.hostname}:{parsed.port}" if parsed.port else str(parsed.hostname)
    return 200, data_page(title, parsed.path or "/", 200, payload)
