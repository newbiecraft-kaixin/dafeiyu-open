# -*- coding: utf-8 -*-
"""充值页：点开链接 → 现场调 ds_qr.py 抓一张微信支付码 → 浏览器里显示。

路由（SLUG 默认 fy8k2m，走樱花 25565 隧道）：
    /                       档位列表
    /{SLUG}/pay?amount=10   选档位后出码的页面（服务端现抓）
    /{SLUG}/qr?amount=10    直接返回二维码 PNG（60 秒内复用同一张，避免重复下单）
启动：python serve.py
环境变量：RECHARGE_PORT / RECHARGE_SLUG / RECHARGE_TLS_CERT / RECHARGE_TLS_KEY / RECHARGE_PY
"""
import base64
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import time
import urllib.request
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env_file() -> None:
    """读安装根的 .env（不入 git），只补缺不覆盖已有环境变量。"""
    path = os.path.join(os.path.dirname(HERE), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


_load_env_file()

PORT = int(os.environ.get("RECHARGE_PORT", "25565"))
SLUG = os.environ.get("RECHARGE_SLUG", "fy8k2m")
# 证书文件名跟隧道域名走：域名属于个人基础设施，只从环境变量读，不写进仓库
_SITE_DOMAIN = os.environ.get("RECHARGE_SITE_DOMAIN", "localhost")
TLS_CERT = os.environ.get(
    "RECHARGE_TLS_CERT",
    rf"C:\ProgramData\SakuraFrpService\FrpcWorkingDirectory\{_SITE_DOMAIN}.crt",
)
TLS_KEY = os.environ.get(
    "RECHARGE_TLS_KEY",
    rf"C:\ProgramData\SakuraFrpService\FrpcWorkingDirectory\{_SITE_DOMAIN}.key",
)
TIERS = (10, 20, 50, 100)
QR_TTL = 60          # 同一档位 60 秒内复用，防止连点/刷新重复下单
GEN_TIMEOUT = 75
PY = os.environ.get("RECHARGE_PY", sys.executable)
SCRIPT = os.path.join(HERE, "ds_qr.py")

_lock = threading.Lock()
_cache: dict[int, tuple[str, float]] = {}   # amount -> (path, ts)

# ---- /bili 刷视频常驻页：web_lookup 插件写好后由这里对外 ----
FEED_FILE = os.environ.get(
    "BILI_FEED_FILE",
    os.path.join(HERE, "..", "astrbot-win", "AstrBot-4.29.3",
                 "data", "stickers", "_share", "bili_feed.html"))

AVATAR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dafeiyu.png")

FEED_EMPTY = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>刷到的视频</title>
<style>%s</style></head><body><div class="card">
<h1>还没有刷到内容</h1>
<p>回群里发一句 /看视频 或让它自己刷，页面会自动更新</p>
</div></body></html>"""

# ---- 云端训练试听：AI Studio 每 500 步 POST 一个 wav，这里转 mp3 后发 QQ ----
# 主人 QQ 从中枢管理员设置动态注入（与插件白名单同源），不落任何静态配置
DEMO_TOKEN = os.environ.get("XIAOYI_DEMO_TOKEN", "")
DEMO_DIR = os.environ.get("XIAOYI_DEMO_DIR", "")
HUB_ADMINS_URL = os.environ.get("HUB_URL", "http://127.0.0.1:23408") + "/bot/admins"
_admin_cache: dict = {"qq": 0, "t": 0.0}


def _owner_qq() -> int:
    """拉中枢管理员列表，取第一位作为主人 QQ；60 秒缓存，失败沿用旧值。"""
    now = time.time()
    if now - _admin_cache["t"] > 60 or not _admin_cache["qq"]:
        try:
            with urllib.request.urlopen(HUB_ADMINS_URL, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            rows = (data.get("data") or {}).get("admins") or []
            if rows:
                _admin_cache["qq"] = int(rows[0])
                _admin_cache["t"] = now
        except Exception:
            pass
    return _admin_cache["qq"]

NAPCAT = os.environ.get("XIAOYI_NAPCAT_URL", "http://127.0.0.1:3000")
NAPCAT_TOKEN = os.environ.get("XIAOYI_NAPCAT_TOKEN", "dafeiyu-api-token")

# ---- 云端训练取包：AI Studio 用 wget 从这里下载 cosy 代码包与数据包 ----
DL_DIR = os.environ.get("XIAOYI_DL_DIR", "")
DL_TOKEN = os.environ.get("XIAOYI_DL_TOKEN", "")
DL_FILES = {"cosy_code.zip", "cosy_data.zip", "cosy.sh", "aistudio_cosy.ipynb"}

# ---- /vote 语音投票 ----
VOTE_DIR = os.path.join(HERE, "vote")
VOTE_FILE = os.path.join(VOTE_DIR, "votes.json")
VOTE_ADMIN = "xiaoyi-vote-7c41"      # 清空全部投票的口令，页面用 /vote?admin=xxx 打开
_vlock = threading.Lock()
_votes: dict | None = None
_vids: set | None = None


def _load_votes() -> dict:
    global _votes
    if _votes is None:
        try:
            with open(VOTE_FILE, encoding="utf-8") as f:
                _votes = json.load(f)
        except Exception:  # noqa: BLE001
            _votes = {}
    return _votes


def _save_votes() -> None:
    tmp = VOTE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_votes, f, ensure_ascii=False)
    os.replace(tmp, VOTE_FILE)


def _vote_ids() -> set:
    global _vids
    if _vids is None:
        try:
            with open(os.path.join(VOTE_DIR, "data.json"), encoding="utf-8") as f:
                _vids = {x["id"] for x in json.load(f)}
        except Exception:  # noqa: BLE001
            _vids = set()
    return _vids


def _counts() -> dict:
    c: dict[str, int] = {}
    for items in _load_votes().values():
        for k in items:
            c[k] = c.get(k, 0) + 1
    return c

STYLE = """*{box-sizing:border-box}
body{margin:0;min-height:100vh;padding:24px 16px;display:flex;align-items:center;justify-content:center;
color:#e8edfb;font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
background:
radial-gradient(1100px 640px at 12% -8%, rgba(86,120,255,.22), transparent 60%),
radial-gradient(900px 560px at 92% 6%, rgba(255,184,107,.14), transparent 55%),
radial-gradient(760px 520px at 50% 112%, rgba(58,210,255,.14), transparent 65%),
linear-gradient(118deg, transparent 34%, rgba(140,170,255,.07) 46%, transparent 58%),
url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='1200' height='260' viewBox='0 0 1200 260'%3E%3Cpath d='M0 130 Q150 60 300 130 T600 130 T900 130 T1200 130 V260 H0 Z' fill='%230d1a3a' fill-opacity='.65'/%3E%3Cpath d='M0 175 Q150 105 300 175 T600 175 T900 175 T1200 175 V260 H0 Z' fill='%23060c1c' fill-opacity='.85'/%3E%3C/svg%3E") no-repeat bottom center / 100% auto,
linear-gradient(180deg,#080e20 0%,#05080f 100%);
background-attachment:fixed}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
background:
radial-gradient(150px 150px at 16% 72%, rgba(58,210,255,.12), transparent 70%),
radial-gradient(200px 200px at 84% 26%, rgba(255,184,107,.12), transparent 70%),
radial-gradient(120px 120px at 68% 88%, rgba(86,120,255,.16), transparent 70%),
radial-gradient(90px 90px at 30% 24%, rgba(255,255,255,.05), transparent 70%);
animation:breathe 11s ease-in-out infinite alternate}
@keyframes breathe{from{opacity:.55;transform:translateY(0)}to{opacity:1;transform:translateY(-14px)}}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
background-image:linear-gradient(rgba(140,170,255,.05) 1px,transparent 1px),
linear-gradient(90deg,rgba(140,170,255,.05) 1px,transparent 1px),
radial-gradient(circle at 30% 25%, rgba(255,255,255,.30) 0 1px, transparent 1.8px),
radial-gradient(circle at 70% 65%, rgba(255,209,102,.30) 0 1.4px, transparent 2.2px);
background-size:34px 34px,34px 34px,150px 150px,230px 230px;
-webkit-mask-image:radial-gradient(circle at 50% 30%,#000 0%,transparent 78%);
mask-image:radial-gradient(circle at 50% 30%,#000 0%,transparent 78%)}
main{position:relative;z-index:1;width:100%;display:flex;justify-content:center}
.card{position:relative;overflow:hidden;width:min(380px,100%);padding:30px 24px 26px;
text-align:center;border-radius:24px;border:1px solid rgba(140,170,255,.20);
background:linear-gradient(180deg,rgba(28,38,66,.90),rgba(14,20,38,.86));
box-shadow:0 20px 60px rgba(0,0,0,.45);animation:rise .5s ease both}
@keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.card::before{content:"";position:absolute;left:18%;right:18%;top:0;height:1px;
background:linear-gradient(90deg,transparent,rgba(255,209,102,.7),transparent)}
.glow{position:absolute;left:50%;top:-130px;width:240px;height:240px;transform:translateX(-50%);
background:radial-gradient(circle,rgba(86,120,255,.42),transparent 70%);filter:blur(8px);
pointer-events:none}
.badge{width:66px;height:66px;margin:0 auto 12px;border-radius:50%;overflow:hidden;
border:1px solid rgba(255,209,102,.45);background:rgba(86,120,255,.18);
box-shadow:0 0 0 3px rgba(255,209,102,.10),0 0 26px rgba(86,120,255,.35)}
.badge img{display:block;width:100%;height:100%;object-fit:cover}
.eyebrow{font-size:11px;letter-spacing:.28em;color:#8f9bbd;text-transform:uppercase;margin-bottom:10px}
h1{font-size:26px;font-weight:600;letter-spacing:.04em;margin:0 0 8px}
h1 .cur{font-size:16px;color:#ffd166;margin-right:3px;vertical-align:7px}
.sub{color:#8f9bbd;font-size:13px;line-height:1.7;margin:0 0 18px}
p{color:#8f9bbd;font-size:13px;line-height:1.7;margin:0 0 10px}
.tiers{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:6px 0 18px}
.tier{position:relative;display:flex;flex-direction:column;gap:3px;align-items:center;padding:16px 10px;
border-radius:14px;border:1px solid rgba(140,170,255,.22);background:rgba(255,255,255,.03);
text-decoration:none;transition:transform .18s,border-color .18s,background .18s,box-shadow .18s}
.tier .amt{font-size:20px;font-weight:600;color:#e8edfb}
.tier .tier-sub{font-size:11.5px;color:#8f9bbd}
.tier:hover{transform:translateY(-2px);border-color:rgba(255,209,102,.50);
background:rgba(255,209,102,.08);box-shadow:0 8px 22px rgba(0,0,0,.28)}
.tier:hover .amt,.tier:hover .tier-sub{color:#ffd166}
.tier.hot{border-color:rgba(255,209,102,.42);background:rgba(255,209,102,.06)}
.tier.hot::after{content:"最多人点";position:absolute;top:-9px;left:50%;transform:translateX(-50%);
font-size:10px;line-height:1;padding:3px 8px;border-radius:999px;white-space:nowrap;
background:linear-gradient(90deg,#ffd166,#ffb86b);color:#1a1206}
.facts{display:flex;flex-wrap:wrap;justify-content:center;gap:6px 16px;margin:0 0 14px;
font-size:11.5px;color:#8f9bbd}
.facts span{display:flex;align-items:center;gap:5px}
.ic{width:5px;height:5px;border-radius:50%;background:#ffd166;
box-shadow:0 0 6px rgba(255,209,102,.75)}
.qr{position:relative;display:inline-block;padding:10px;border-radius:18px;background:#fff;
box-shadow:0 12px 30px rgba(0,0,0,.35);margin:2px 0 14px}
.qr::before{content:"";position:absolute;inset:5px;border-radius:13px;
border:1px solid rgba(255,209,102,.28);pointer-events:none}
.qr img{display:block;width:230px;height:230px;object-fit:contain}
.pill{display:flex;align-items:center;gap:6px;width:fit-content;margin:0 auto 14px;padding:5px 12px;
border-radius:999px;font-size:11.5px;color:#c7d2f0;
border:1px solid rgba(140,170,255,.22);background:rgba(255,255,255,.03)}
.back{display:inline-block;padding:9px 20px;border-radius:999px;font-size:13px;
color:#c7d2f0;text-decoration:none;border:1px solid rgba(140,170,255,.25)}
.back:hover{border-color:rgba(255,209,102,.45);color:#ffd166}
.foot{display:flex;align-items:center;justify-content:center;gap:6px;
margin-top:16px;font-size:11.5px;color:#6f7b9a}
.dot{width:6px;height:6px;border-radius:50%;background:#ffd166;
box-shadow:0 0 8px rgba(255,209,102,.8)}
.err{color:#f66;font-size:13px;margin:14px 0}
@media (max-width:400px){.card{padding:24px 18px 22px}h1{font-size:23px}.qr img{width:210px;height:210px}}"""

TIER_LABELS = {10: "一杯奶茶", 20: "一顿外卖", 50: "一晚电费", 100: "两天口粮"}

HOME = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>给我续点口粮</title>
<style>%s</style></head><body><main><section class="card">
<div class="glow"></div>
<div class="badge"><img src="/avatar" alt="大肥鱼"></div>
<div class="eyebrow">支持一下 · 续口粮</div>
<h1>给我续点口粮</h1>
<p class="sub">选个档位，点开就是微信收款码</p>
<div class="tiers">%s</div>
<div class="facts">
<span><i class="ic"></i>微信扫码</span>
<span><i class="ic"></i>15 分钟有效</span>
<span><i class="ic"></i>群里说一声</span>
</div>
<div class="foot"><span class="dot"></span>感谢投喂 · 大肥鱼</div>
</section></main></body></html>"""

PAY = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>%s 元</title>
<style>%s</style></head><body><main><section class="card">
<div class="glow"></div>
<div class="badge"><img src="/avatar" alt="大肥鱼"></div>
<div class="eyebrow">微信扫码 · 支付</div>
<h1><span class="cur">¥</span>%s</h1>
<p class="sub">付完在群里说一声就行</p>
<div class="qr">%s</div>
<div class="pill"><span class="dot"></span>等待支付 · 15 分钟内有效</div>
<a class="back" href="/%s">换个档位</a>
</section></main></body></html>"""


def _gen(amount: int) -> str:
    """调 ds_qr.py 现抓一张码，返回图片路径；失败返回空串。"""
    with _lock:
        hit = _cache.get(amount)
        if hit and time.time() - hit[1] < QR_TTL and os.path.exists(hit[0]):
            return hit[0]
        try:
            out = subprocess.run(
                [PY, SCRIPT, "--amount", str(amount), "--json"],
                capture_output=True, timeout=GEN_TIMEOUT, cwd=HERE,
            )
            data = json.loads((out.stdout or b"").decode("utf-8", "ignore").strip()
                              .splitlines()[-1] or "{}")
        except Exception:  # noqa: BLE001
            return ""
        path = str(data.get("file") or "")
        if data.get("ok") and path and os.path.exists(path):
            _cache[amount] = (path, time.time())
            return path
        return ""


def _qq_send(message: list) -> dict:
    """直接调本机 NapCat 给主人发私聊。"""
    req = urllib.request.Request(
        NAPCAT + "/send_private_msg",
        data=json.dumps({"user_id": _owner_qq(), "message": message}).encode(),
        headers={"Authorization": "Bearer " + NAPCAT_TOKEN,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, ctype: str, body: bytes, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _uid(self, create: bool = True) -> tuple[str, dict | None]:
        """取投票人标识；没有就发一个匿名 cookie，不要求登录。"""
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "vp_uid" and re.fullmatch(r"[0-9a-f]{32}", v or ""):
                return v, None
        if not create:
            return "", None
        u = secrets.token_hex(16)
        return u, {"Set-Cookie": f"vp_uid={u}; Path=/; Max-Age=31536000; SameSite=Lax"}

    def _vote_page(self) -> None:
        with open(os.path.join(VOTE_DIR, "index.html"), "rb") as f:
            body = f.read()
        _, ck = self._uid()
        self._send(200, "text/html; charset=utf-8", body, ck)

    def _vote_static(self, path: str) -> None:
        if path == "/vote/data.json":
            with open(os.path.join(VOTE_DIR, "data.json"), "rb") as f:
                self._send(200, "application/json; charset=utf-8", f.read())
            return
        name = path.rsplit("/", 1)[-1]
        if re.fullmatch(r"[a-z]{2,3}_\d{3}\.mp3", name) and os.path.exists(os.path.join(VOTE_DIR, "audio", name)):
            with open(os.path.join(VOTE_DIR, "audio", name), "rb") as f:
                self._send(200, "audio/mpeg", f.read())
            return
        self._send(404, "text/plain; charset=utf-8", b"not found")

    def _vote_results(self) -> None:
        with _vlock:
            payload = {"ok": True, "counts": _counts(), "voters": len(_load_votes())}
        self._send(200, "application/json; charset=utf-8",
                   json.dumps(payload, ensure_ascii=False).encode())

    def _vote_cast(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 4096:
            self._send(400, "application/json; charset=utf-8", b'{"ok":false}')
            return
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._send(400, "application/json; charset=utf-8", b'{"ok":false}')
            return
        iid = str(data.get("id") or "")
        on = bool(data.get("on"))
        if iid not in _vote_ids():
            self._send(400, "application/json; charset=utf-8", b'{"ok":false}')
            return
        uid, ck = self._uid()
        with _vlock:
            votes = _load_votes()
            items = votes.setdefault(uid, {})
            if on:
                items[iid] = 1
            else:
                items.pop(iid, None)
            if not items:
                votes.pop(uid, None)
            _save_votes()
            payload = {"ok": True, "counts": _counts()}
        self._send(200, "application/json; charset=utf-8",
                   json.dumps(payload, ensure_ascii=False).encode(), ck)

    def _vote_clear(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 4096:
            self._send(400, "application/json; charset=utf-8", b'{"ok":false}')
            return
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._send(400, "application/json; charset=utf-8", b'{"ok":false}')
            return
        if str(data.get("scope") or "mine") == "all":
            if str(data.get("admin") or "") != VOTE_ADMIN:
                self._send(403, "application/json; charset=utf-8", b'{"ok":false}')
                return
            with _vlock:
                _load_votes().clear()
                _save_votes()
                payload = {"ok": True, "counts": {}}
        else:
            uid, _ = self._uid(create=False)
            with _vlock:
                votes = _load_votes()
                votes.pop(uid, None)
                _save_votes()
                payload = {"ok": True, "counts": _counts()}
        self._send(200, "application/json; charset=utf-8",
                   json.dumps(payload, ensure_ascii=False).encode())

    def _amount(self) -> int:
        try:
            q = self.path.split("?", 1)[1]
        except IndexError:
            return 0
        for part in q.split("&"):
            if part.startswith("amount="):
                try:
                    v = int(float(part.split("=", 1)[1]))
                except ValueError:
                    return 0
                return v if v in TIERS else 0
        return 0

    def _demo_post(self) -> None:
        """云端训练回调：收一个 wav/mp3，转码后发到 QQ；?dry=1 只落盘不发送。"""
        if self.headers.get("X-Token") != DEMO_TOKEN:
            self._send(403, "application/json; charset=utf-8",
                       b'{"ok":false,"err":"token"}')
            return
        q = self.path.partition("?")[2]
        params = dict(p.partition("=")[::2] for p in q.split("&") if "=" in p)
        try:
            step = int(params.get("step", "0"))
        except ValueError:
            step = 0
        fmt = params.get("fmt", "wav")
        dry = params.get("dry") == "1"
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 30_000_000:
            self._send(400, "application/json; charset=utf-8",
                       b'{"ok":false,"err":"size"}')
            return
        body = self.rfile.read(n)
        os.makedirs(DEMO_DIR, exist_ok=True)
        raw = os.path.join(DEMO_DIR, f"step_{step:05d}.{fmt}")
        with open(raw, "wb") as f:
            f.write(body)
        mp3 = raw
        if fmt != "mp3":
            mp3 = os.path.join(DEMO_DIR, f"step_{step:05d}.mp3")
            subprocess.run(["ffmpeg", "-y", "-i", raw, "-codec:a", "libmp3lame",
                            "-b:a", "96k", mp3], capture_output=True, timeout=60)
            if not os.path.exists(mp3):
                mp3 = raw
        if dry:
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"ok": True, "file": mp3, "dry": True}).encode())
            return
        try:
            with open(mp3, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            _qq_send([{"type": "text",
                       "data": {"text": f"云端训练第 {step} 步试听："}}])
            resp = _qq_send([{"type": "record",
                              "data": {"file": "base64://" + b64}}])
        except Exception as e:  # noqa: BLE001
            self._send(502, "application/json; charset=utf-8",
                       json.dumps({"ok": False,
                                   "err": type(e).__name__}).encode())
            return
        mid = (resp.get("data") or {}).get("message_id")
        self._send(200, "application/json; charset=utf-8",
                   json.dumps({"ok": True, "step": step, "file": mp3,
                               "message_id": mid}).encode())

    def _dl_file(self) -> None:
        """云端训练取包：白名单文件流式下发，?k=<token> 校验。"""
        q = self.path.partition("?")[2]
        params = dict(p.partition("=")[::2] for p in q.split("&") if "=" in p)
        if params.get("k") != DL_TOKEN:
            self._send(403, "application/json; charset=utf-8",
                       b'{"ok":false,"err":"token"}')
            return
        name = self.path.split("?")[0].rsplit("/", 1)[-1]
        if name not in DL_FILES:
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        fp = os.path.join(DL_DIR, name)
        if not os.path.exists(fp):
            self._send(404, "text/plain; charset=utf-8", b"missing")
            return
        size = os.path.getsize(fp)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(name))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(fp, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/xiaoyi/demo":
            self._demo_post()
            return
        if path == "/vote/api/vote":
            self._vote_cast()
            return
        if path == "/vote/api/clear":
            self._vote_clear()
            return
        self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/vote", "/vote/"):
            self._vote_page()
            return
        if path == "/vote/api/results":
            self._vote_results()
            return
        if path.startswith("/vote/"):
            self._vote_static(path)
            return
        if path.startswith("/xiaoyi/dl/"):
            self._dl_file()
            return
        if path in ("/avatar", "/avatar.png"):
            try:
                with open(AVATAR_FILE, "rb") as f:
                    body = f.read()
            except OSError:
                self._send(404, "text/plain; charset=utf-8", b"no avatar")
                return
            self._send(200, "image/png", body,
                       {"Cache-Control": "public, max-age=86400"})
            return
        if path in ("/bili", "/bili/", "/bili/index.html"):
            try:
                with open(FEED_FILE, "rb") as f:
                    body = f.read()
            except OSError:
                body = (FEED_EMPTY % STYLE).encode()
            self._send(200, "text/html; charset=utf-8", body)
            return
        if path in ("/", f"/{SLUG}", f"/{SLUG}/"):
            links = "".join(
                f'<a class="{"tier hot" if t == 50 else "tier"}" href="/{SLUG}/pay?amount={t}">'
                f'<span class="amt">¥{t}</span>'
                f'<span class="tier-sub">{TIER_LABELS.get(t, "")}</span></a>'
                for t in TIERS
            )
            self._send(200, "text/html; charset=utf-8",
                       (HOME % (STYLE, links)).encode())
            return

        if path == f"/{SLUG}/qr":
            amount = self._amount()
            if not amount:
                self._send(404, "text/plain; charset=utf-8", b"bad amount")
                return
            p = _gen(amount)
            if not p:
                self._send(503, "text/plain; charset=utf-8", b"gen failed")
                return
            with open(p, "rb") as f:
                self._send(200, "image/png", f.read())
            return

        if path == f"/{SLUG}/pay":
            amount = self._amount()
            if not amount:
                self._send(200, "text/html; charset=utf-8",
                           (PAY % ("?", STYLE, "?", '<p class="err">档位不对</p>',
                                   SLUG)).encode())
                return
            p = _gen(amount)
            if p:
                img = f'<img src="/{SLUG}/qr?amount={amount}&t={int(time.time())}" alt="微信收款码">'
                body = PAY % (amount, STYLE, amount, img, SLUG)
            else:
                body = PAY % (amount, STYLE, amount,
                              '<p class="err">码没生成出来，回群里再发一次 /充值 试试</p>',
                              SLUG)
            self._send(200, "text/html; charset=utf-8", body.encode())
            return

        self._send(404, "text/plain; charset=utf-8", b"not found")


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    tls = os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY)
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print(f"本地页面: {'https' if tls else 'http'}://127.0.0.1:{PORT}/{SLUG}")
    srv.serve_forever()
