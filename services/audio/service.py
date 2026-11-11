"""audio：音频能力微服务（训练 + 歌曲 + 语音识别/画面描述，同一进程）。

端口：23366（可用 AUDIO_PORT 覆盖）
     23405 由同一进程的副监听线程提供，兼容旧调用方（原 subtitle 服务）

页面
  GET /            导航页
  GET /ui/train    声线管理页（参考音频 + 提示文本 → 直接写入声线档案）
  GET /ui/songs    歌曲管理页（可配置酷狗/咪咕等音源，玩家有音频即可入库）
  GET /ui/covers   翻唱管理页（扫描机器人唱过的 cover_/sing_ 成品，试听/删除）
  GET /records     语音识别 / 画面描述最近记录（原 subtitle 控制台）

子模块（各自独立文件，互不修改）
  train_api.py     声线档案：POST/GET /train/voices、GET /train/refs
  songs_api.py     歌曲库：GET/POST /songs、/songs/sources、/songs/search
  covers_api.py    翻唱成品：GET /covers、/covers/audio、DELETE /covers
  ../subtitle/subsvc  语音识别与画面描述：/asr、/vision、/records

同类归并说明
  字幕服务与音频服务共用 AstrBot 的 venv 与 infra，直接并入本进程，减少一个常驻进程；
  CosyVoice 语音合成依赖独立 venv 与 GPU，无法并入，仍以 8714 独立进程运行。

本文件只做装配：合并子模块的 routes，不承载业务逻辑。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import Html, ok, port_of, run  # noqa: E402

import covers_api  # noqa: E402
import songs_api  # noqa: E402
import train_api  # noqa: E402

PORT = port_of("audio", 23366)
SUBTITLE_PORT = 23405
UI_DIR = _HERE.parent / "ui"

# 语音识别/画面描述并入本进程；依赖缺失时只降级这部分，不影响音频主功能
SUBTITLE_ROUTES = {}
SUBTITLE_ERROR = ""
try:
    sys.path.insert(0, str(_HERE.parents[1] / "subtitle"))
    from subsvc import ROUTES as _SUB_ROUTES  # noqa: E402

    SUBTITLE_ROUTES = dict(_SUB_ROUTES)
except Exception as _exc:  # noqa: BLE001
    SUBTITLE_ERROR = f"{type(_exc).__name__}: {_exc}"

VOICE_PORT = port_of("voice", 8714)
VOICE_HEALTH = f"http://127.0.0.1:{VOICE_PORT}/health"

_voice_cache = {"at": 0.0, "online": False}


def _voice_online() -> bool:
    """声线试听/发声都走 8714 语音合成，探测一下好把依赖关系在页面上说清楚。"""
    import time
    import urllib.request

    now = time.monotonic()
    if now - _voice_cache["at"] < 5:
        return _voice_cache["online"]
    online = False
    try:
        with urllib.request.urlopen(VOICE_HEALTH, timeout=0.8) as resp:
            online = 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 - 语音合成没启动是常态
        online = False
    _voice_cache.update(at=now, online=online)
    return online


NAV_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>大肥鱼 · 音频能力</title>
<style>
body{margin:0;min-height:100vh;background:#070a14;color:#e9edff;
font:14px/1.7 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;display:grid;place-items:center}
.wrap{max-width:860px;padding:40px 32px}
h1{margin:0 0 6px;font-size:24px;letter-spacing:.5px}
p.sub{margin:0 0 24px;color:#8b97bd}
h2.sec{margin:26px 0 12px;font-size:14px;font-weight:600;color:#bfd0ff;letter-spacing:.6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px}
a.card{display:block;padding:22px 24px;border-radius:16px;text-decoration:none;color:#e9edff;
border:1px solid rgba(102,126,182,.35);background:#101731;transition:.18s}
a.card:hover{border-color:rgba(107,143,255,.6);background:#131c3c;transform:translateY(-2px)}
a.card b{display:block;font-size:17px;margin-bottom:8px}
a.card span{color:#8b97bd;font-size:13px;line-height:1.6}
.tag{display:inline-block;margin-top:12px;padding:3px 10px;border-radius:999px;font-size:12px;
background:rgba(107,143,255,.16);color:#bfd0ff;border:1px solid rgba(107,143,255,.3)}
.tag.ok{background:rgba(70,211,154,.12);color:#46d39a;border-color:rgba(70,211,154,.32)}
.tag.warn{background:rgba(245,181,68,.14);color:#f5b544;border-color:rgba(245,181,68,.36)}
.notice{margin:0 0 8px;padding:10px 14px;border-radius:10px;font-size:13px;
border:1px solid rgba(245,181,68,.42);background:rgba(245,181,68,.07);color:#f5b544}
.notice.ok{border-color:rgba(70,211,154,.3);background:rgba(70,211,154,.06);color:#46d39a}
</style></head><body><div class="wrap">
<h1>大肥鱼 · 音频能力</h1>
<p class="sub">语音合成与声线是一块：声线档案在这里管理，试听和发声由语音合成服务完成</p>
<!--VOICE_BANNER-->
<h2 class="sec">语音合成 · 声线</h2>
<div class="cards">
<a class="card" href="<!--VOICE_LINK-->"><b>语音合成（CosyVoice）</b>
<span>三套角色音色的 TTS。依赖独立 venv 与 GPU 显存，作为 8714 独立进程运行。</span>
<span class="tag">8714 · 独立进程 · 需 GPU</span></a>
<a class="card" href="/ui/train"><b>声线管理</b>
<span>填参考音频路径和提示文本，直接写入声线档案；提交后立刻可用于说话。</span>
<span class="tag">ref.wav · prompt.txt</span><!--VOICE_TAG--></a>
</div>
<h2 class="sec">素材与识别</h2>
<div class="cards">
<a class="card" href="/ui/songs"><b>歌曲管理</b>
<span>配置可用音源（酷狗、咪咕等），搜索试听并入库；也支持直接登记本地音频。</span>
<span class="tag">音源适配器 · 歌曲库</span></a>
<a class="card" href="/ui/covers"><b>翻唱管理</b>
<span>机器人唱过的翻唱与唱歌成品都在这里：本机存储、在线试听、单条清理。</span>
<span class="tag">cover_ · sing_ · 本地存储</span></a>
<a class="card" href="/records"><b>语音识别 · 画面描述</b>
<span>本地 faster-whisper 转写音频/视频，Ollama 视觉描述画面；查看最近调用记录。</span>
<span class="tag">ASR · 视觉 · 原字幕服务</span></a>
</div></div></body></html>"""


def _page(name: str):
    path = UI_DIR / name
    if not path.exists():
        return 404, Html(f'<h1 style="font:16px system-ui;color:#ffc2cd">页面缺失：{name}</h1>')
    return 200, Html(path.read_text(encoding="utf-8"))


def _nav_page():
    if _voice_online():
        banner = '<div class="notice ok">语音合成（8714）在线，声线可以直接试听和使用。</div>'
        tag = '<span class="tag ok">依赖语音合成 · 在线</span>'
    else:
        banner = ('<div class="notice warn">语音合成（8714）未启动：声线可以保存和管理，'
                  '但试听和让肥鱼说话都用不了。请先启动「语音合成」再回来。</div>')
        tag = '<span class="tag warn">依赖语音合成 · 未启动</span>'
    return (NAV_HTML
            .replace("<!--VOICE_BANNER-->", banner)
            .replace("<!--VOICE_TAG-->", tag)
            .replace("<!--VOICE_LINK-->", VOICE_HEALTH))


def handle_nav(_req):
    return 200, Html(_nav_page())


def handle_voice_status(_req):
    return ok({"online": _voice_online(), "port": VOICE_PORT, "health": VOICE_HEALTH})


def handle_train_ui(_req):
    return _page("train.html")


def handle_songs_ui(_req):
    return _page("songs.html")


def handle_covers_ui(_req):
    return _page("covers.html")


def handle_health(_req):
    return ok({
        "service": "audio",
        "port": PORT,
        "train": train_api.status(),
        "songs": songs_api.status(),
        "covers": covers_api.status(),
        "subtitle": {
            "merged": bool(SUBTITLE_ROUTES),
            "port": SUBTITLE_PORT,
            "error": SUBTITLE_ERROR,
        },
    })


routes = {
    "GET /": handle_nav,

    "GET /voice-status": handle_voice_status,
    "GET /ui/train": handle_train_ui,
    "GET /ui/songs": handle_songs_ui,
    "GET /ui/covers": handle_covers_ui,
    "GET /health": handle_health,
}
routes.update(train_api.routes)
routes.update(songs_api.routes)
routes.update(covers_api.routes)
# 字幕路由并入：/records、/asr、/vision（与音频路由不同名，不覆盖已有项）
for _key, _handler in SUBTITLE_ROUTES.items():
    routes.setdefault(_key, _handler)


def main() -> None:
    # 旧端口 23405 由副线程继续提供，原调用方无需改动
    if SUBTITLE_ROUTES:
        threading.Thread(
            target=run,
            args=(SUBTITLE_PORT, SUBTITLE_ROUTES),
            kwargs={"service": "subtitle", "version": "1.0"},
            daemon=True,
            name="subtitle-port",
        ).start()
    run(PORT, routes, service="audio", version="0.1.0")
    print(f"[audio] listening on http://127.0.0.1:{PORT}", flush=True)


if __name__ == "__main__":
    main()
