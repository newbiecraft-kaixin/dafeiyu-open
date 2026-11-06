"""路由处理与可读页面。"""
from __future__ import annotations

import html
import os
import time
from typing import Any

from infra import Html, fail, i18n_route, ok

from .config import ASR_MODEL, DEFAULT_LANG, VISION_MODEL
from .engines import asr_sync, fetch, vision_sync, whisper_loaded
from .records import count, remember, snapshot

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>音频字幕系统 · 大肥鱼</title>
<style>
  :root { --bg:#0d1117; --panel:#161b22; --line:#26303d; --text:#e6edf3; --sub:#8b949e; --acc:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:14px/1.6 "Segoe UI",system-ui,sans-serif; }
  header { padding:22px 28px; border-bottom:1px solid var(--line); }
  h1 { margin:0 0 6px; font-size:20px; }
  .sub { color:var(--sub); font-size:12px; }
  main { padding:20px 28px 60px; }
  code { background:#0a0e14; border:1px solid var(--line); border-radius:6px; padding:2px 6px; }
  table { width:100%; border-collapse:collapse; margin-top:14px; }
  th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); font-size:13px; vertical-align:top; }
  th { color:var(--sub); font-weight:600; }
  .empty { color:var(--sub); padding:28px 0; }
</style>
</head>
<body>
<header>
  <h1>音频字幕系统</h1>
  <span class="sub">__ENGINE__ · <span>视觉</span> __VISION__ · <span>最近</span> __COUNT__ <span>次</span></span>
</header>
<main>
  <p>接口：<code>POST /asr {"path"|"url"}</code> 语音/视频转写；<code>POST /vision {"path"|"url"}</code> 画面描述。</p>
  <table>
    <thead><tr><th>时间</th><th>类型</th><th>来源</th><th>结果</th><th>耗时</th></tr></thead>
    <tbody>__ROWS__</tbody>
  </table>
</main>
<script src="/i18n.js" defer></script>
</body>
</html>
"""


def _payload(req) -> dict[str, Any]:
    return req.payload if isinstance(req.payload, dict) else {}


def _source(req) -> str:
    data = _payload(req)
    return str(data.get("path") or data.get("url") or "").strip()


def _lang(req) -> str:
    """请求可用 lang=zh|en|auto 覆盖；缺省取 SUBTITLE_LANG（默认 zh）。"""
    data = _payload(req)
    value = data.get("lang") or data.get("language") or ""
    return str(value).strip() or DEFAULT_LANG


def _health(_req) -> tuple[int, dict]:
    return ok(
        {
            "engine": {"asr": f"faster-whisper/{ASR_MODEL} int8", "vision": VISION_MODEL},
            "device": "cpu",
            "asr_loaded": whisper_loaded(),
            "records": count(),
            "languages": ["zh", "en", "auto"],
            "default_lang": DEFAULT_LANG,
        }
    )


def _records(_req) -> tuple[int, dict]:
    return ok({"items": snapshot(), "count": count()})


def _asr_route(req) -> tuple[int, dict]:
    src = _source(req)
    if not src:
        return fail("path 或 url 必填")
    started = time.time()
    tmp = False
    lang = _lang(req)
    try:
        path, tmp = fetch(src)
        text = asr_sync(path, lang)
    except FileNotFoundError:
        return fail(f"文件不存在: {src}", 404)
    except Exception as error:  # noqa: BLE001
        return fail(f"转写失败: {type(error).__name__}: {str(error)[:120]}", 500)
    finally:
        if tmp:
            try:
                os.remove(path)
            except OSError:
                pass
    cost = time.time() - started
    remember("asr", src, text, cost)
    return ok({"text": text, "src": src, "seconds": round(cost, 2), "lang": lang})


def _vision_route(req) -> tuple[int, dict]:
    src = _source(req)
    if not src:
        return fail("path 或 url 必填")
    started = time.time()
    tmp = False
    lang = _lang(req)
    try:
        path, tmp = fetch(src)
        text = vision_sync(path, lang)
    except FileNotFoundError:
        return fail(f"文件不存在: {src}", 404)
    except Exception as error:  # noqa: BLE001
        return fail(f"画面描述失败: {type(error).__name__}: {str(error)[:120]}", 500)
    finally:
        if tmp:
            try:
                os.remove(path)
            except OSError:
                pass
    cost = time.time() - started
    remember("vision", src, text, cost)
    return ok({"text": text, "src": src, "seconds": round(cost, 2), "lang": lang})


def _page(_req) -> tuple[int, Html]:
    rows = []
    for item in snapshot():
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}s</td></tr>".format(
                time.strftime("%H:%M:%S", time.localtime(item["at"])),
                html.escape(item["kind"]),
                html.escape(item["src"]),
                html.escape(item["text"]),
                item["seconds"],
            )
        )
    body = "".join(rows) or '<tr><td class="empty" colspan="5">还没有记录</td></tr>'
    page = (
        PAGE.replace("__ROWS__", body)
        .replace("__ENGINE__", f"faster-whisper/{ASR_MODEL}")
        .replace("__VISION__", VISION_MODEL)
        .replace("__COUNT__", str(count()))
    )
    return 200, Html(page)


ROUTES = {
    "GET /": _page,
    "GET /i18n.js": i18n_route,
    "GET /health": _health,
    "GET /records": _records,
    "POST /asr": _asr_route,
    "POST /vision": _vision_route,
}
