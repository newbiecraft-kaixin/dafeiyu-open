"""video：视频能力微服务（看视频 + 刷视频 同一端口）。

端口：23368（可用 VIDEO_PORT 覆盖）

页面
  GET /            导航页
  GET /ui/parse    看视频：贴链接 → 标题/作者/字幕/评论，可读内容整理
  GET /ui/feed     刷视频：B站 / 抖音 / 快手 按标签取片单、维护片单池、记录喜欢

子模块（各自独立文件，互不修改）
  parse_api.py     视频解析：POST /video/parse 等
  feed_api.py      刷视频：GET /video/feed、/video/pool、POST /video/like 等
  login_api.py     扫码登录：B站 / 抖音 / 快手，登录后自动保存 Cookie

本文件只做装配：合并子模块的 routes，不承载业务逻辑。
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import Html, ok, port_of, run  # noqa: E402

import feed_api  # noqa: E402
import login_api  # noqa: E402
import parse_api  # noqa: E402

PORT = port_of("video", 23368)
UI_DIR = _HERE.parent / "ui"

NAV_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>大肥鱼 · 视频能力</title>
<style>
body{margin:0;min-height:100vh;background:#070a14;color:#e9edff;
font:14px/1.7 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;display:grid;place-items:center}
.wrap{max-width:860px;padding:40px 32px}
h1{margin:0 0 6px;font-size:24px;letter-spacing:.5px}
p.sub{margin:0 0 28px;color:#8b97bd}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px}
a.card{display:block;padding:22px 24px;border-radius:16px;text-decoration:none;color:#e9edff;
border:1px solid rgba(102,126,182,.35);background:#101731;transition:.18s}
a.card:hover{border-color:rgba(107,143,255,.6);background:#131c3c;transform:translateY(-2px)}
a.card b{display:block;font-size:17px;margin-bottom:8px}
a.card span{color:#8b97bd;font-size:13px;line-height:1.6}
.tag{display:inline-block;margin-top:12px;padding:3px 10px;border-radius:999px;font-size:12px;
background:rgba(107,143,255,.16);color:#bfd0ff;border:1px solid rgba(107,143,255,.3)}
</style></head><body><div class="wrap">
<h1>大肥鱼 · 视频能力</h1>
<p class="sub">看视频和刷 B站，同一个微服务（23368）承载，属于可选能力</p>
<div class="cards">
<a class="card" href="/ui/parse"><b>看视频</b>
<span>贴 B站/抖音/快手/西瓜 链接，取标题、作者、字幕与热门评论，整理成能读的一段话。</span>
<span class="tag">字幕 · 评论 · 转写兜底</span></a>
<a class="card" href="/ui/feed"><b>刷视频</b>
<span>B站 / 抖音 / 快手，按标签取片单并维护片单池；喜欢的视频可以记录下来。</span>
<span class="tag">片单池 · 标签 · 点赞</span></a>
</div></div></body></html>"""


def _page(name: str):
    path = UI_DIR / name
    if not path.exists():
        return 404, Html(f'<h1 style="font:16px system-ui;color:#ffc2cd">页面缺失：{name}</h1>')
    return 200, Html(path.read_text(encoding="utf-8"))


def handle_nav(_req):
    return 200, Html(NAV_HTML)


def handle_parse_ui(_req):
    return _page("parse.html")


def handle_feed_ui(_req):
    return _page("feed.html")


def handle_health(_req):
    return ok({
        "service": "video",
        "port": PORT,
        "parse": parse_api.status(),
        "feed": feed_api.status(),
        "login": login_api.status(),
    })


routes = {
    "GET /": handle_nav,

    "GET /ui/parse": handle_parse_ui,
    "GET /ui/feed": handle_feed_ui,
    "GET /health": handle_health,
}
routes.update(parse_api.routes)
routes.update(feed_api.routes)
routes.update(login_api.routes)


def main() -> None:
    run(PORT, routes, service="video", version="0.1.0")
    print(f"[video] listening on http://127.0.0.1:{PORT}", flush=True)


if __name__ == "__main__":
    main()
