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
  :root { --bg:#05070f; --panel:#0a1020; --line:rgba(103,232,249,.16); --text:#e6edf3; --sub:#8b949e; --acc:#67e8f9; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; background:linear-gradient(180deg,#05070f,#0a1020); color:var(--text); font:14px/1.6 "Segoe UI",system-ui,sans-serif; animation:pagein .4s ease both; }
  .aurora { position:fixed; inset:0; z-index:0; pointer-events:none; overflow:hidden; }
  .aurora i { position:absolute; display:block; border-radius:50%; filter:blur(70px); }
  .aurora .a1 { width:52vw; height:52vw; left:-12vw; top:-18vw; background:radial-gradient(circle, rgba(34,211,238,.15), transparent 65%); animation:drift1 26s ease-in-out infinite alternate; }
  .aurora .a2 { width:46vw; height:46vw; right:-14vw; top:4vw; background:radial-gradient(circle, rgba(99,102,241,.16), transparent 65%); animation:drift2 22s ease-in-out infinite alternate; }
  .aurora .a3 { width:42vw; height:42vw; left:20vw; bottom:-18vw; background:radial-gradient(circle, rgba(45,212,191,.11), transparent 65%); animation:drift3 30s ease-in-out infinite alternate; }
  @keyframes drift1 { to { transform:translate(9vw,7vh) scale(1.12); } }
  @keyframes drift2 { to { transform:translate(-8vw,9vh) scale(.92); } }
  @keyframes drift3 { to { transform:translate(7vw,-8vh) scale(1.1); } }
  @keyframes pagein { from { opacity:0; } to { opacity:1; } }
  header, main { position:relative; z-index:1; }
  header { padding:22px 28px; border-bottom:1px solid transparent; border-image:linear-gradient(90deg, transparent, rgba(103,232,249,.45), rgba(165,180,252,.32), transparent) 1; }
  h1 { margin:0 0 6px; font-size:20px; background:linear-gradient(120deg,#7dd3fc,#a5b4fc); -webkit-background-clip:text; background-clip:text; color:transparent; }
  .sub { color:var(--sub); font-size:12px; }
  main { padding:20px 28px 60px; }
  code { background:rgba(34,211,238,.08); border:1px solid rgba(103,232,249,.22); border-radius:6px; padding:2px 6px; color:#a5f3fc; }
  table { width:100%; border-collapse:collapse; margin-top:14px; }
  th,td { text-align:left; padding:8px 10px; font-size:13px; vertical-align:top; background-image:linear-gradient(90deg, transparent, rgba(103,232,249,.22), rgba(165,180,252,.12), transparent); background-repeat:no-repeat; background-size:100% 1px; background-position:0 100%; }
  th { color:var(--sub); font-weight:600; background-color:rgba(103,232,249,.06); }
  tbody tr { transition:background-color .18s ease; }
  tbody tr:hover td { background-color:rgba(34,211,238,.07); }
  td[data-k]::before { content:""; display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:7px; vertical-align:1px; }
  td[data-k="asr"]::before { background:#67e8f9; box-shadow:0 0 6px rgba(103,232,249,.8); }
  td[data-k="vision"]::before { background:#818cf8; box-shadow:0 0 6px rgba(129,140,248,.8); }
  .empty { color:var(--sub); padding:28px 0; }
</style>
</head>
<body>
<div class="aurora" aria-hidden="true"><i class="a1"></i><i class="a2"></i><i class="a3"></i></div>
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
<script>
document.querySelectorAll('tbody tr').forEach(function (tr) {
  var td = tr.querySelector('td:nth-child(2)');
  if (!td) return;
  var k = td.textContent.trim();
  if (k === 'asr' || k === 'vision') td.setAttribute('data-k', k);
});
</script>
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
