"""界面多语言：把前端运行时 + 中文→英文字典拼成一段可直接返回的 JS。

各服务只需要暴露一个路由：

    from infra import i18n_route
    routes["GET /i18n.js"] = i18n_route

页面里加 <script src="/i18n.js" defer></script> 即可。字典文件缺失时
返回空字典，页面维持中文，不会报错。
"""
from __future__ import annotations

import json
from pathlib import Path

_ASSETS = Path(__file__).resolve().parents[1] / "i18n"
_RUNTIME = _ASSETS / "ui_i18n.js"
_DICT = _ASSETS / "zh-en.json"

_cache: dict[str, tuple[float, str]] = {}


def _read(path: Path) -> str:
    """按 mtime 缓存，改字典不用重启服务。"""
    if not path.is_file():
        return ""
    mtime = path.stat().st_mtime
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    text = path.read_text(encoding="utf-8")
    _cache[str(path)] = (mtime, text)
    return text


def i18n_js() -> str:
    runtime = _read(_RUNTIME)
    if not runtime:
        return ""
    raw = _read(_DICT) or "{}"
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError:
        mapping = {}
    if not isinstance(mapping, dict):
        mapping = {}
    payload = json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
    return "window.__DAFEIYU_I18N__=" + payload + ";\n" + runtime


def i18n_route(_req):
    from .web import Blob

    body = i18n_js()
    return 200, Blob(body.encode("utf-8"), "application/javascript; charset=utf-8")
