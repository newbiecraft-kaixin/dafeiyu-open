"""source_login：音乐音源扫码登录（酷狗 / QQ音乐 / 网易云，咪咕不需要）。

除咪咕外，远程音源都要先扫码登录；登录后 Cookie 存 data/source_cookies.json，
搜索与取直链时带上，才能拿到 VIP 曲的可播放地址。

扫码窗口是「有头」的（持久化在 data/browser/<source>）：
  1. 能直接截到二维码就回传给前端，页面里展示，手机扫完自动收 Cookie；
  2. 截不到（站点改版/跨域图片）也不影响，用户在那个浏览器窗口里扫一样能登。

接口
  POST /songs/login/start    {source}      → {sid}
  GET  /songs/login/qr       ?sid=         → image/png
  GET  /songs/login/status   ?sid=         → {status, message, logged_in}
  POST /songs/login/logout   {source}      → {source, logged_in:false}
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import Blob, fail, ok  # noqa: E402

DATA_DIR = _HERE.parent / "data"
COOKIE_FILE = DATA_DIR / "source_cookies.json"
PROFILE_DIR = DATA_DIR / "browser"

EXPIRES = 300  # 二维码有效期（秒）

# 每个音源的登录页、登录按钮、Cookie 判据
SOURCES: Dict[str, Dict[str, Any]] = {
    "kugou": {
        "name": "酷狗音乐",
        "url": "https://www.kugou.com/",
        "login_text": "登录",
        "frame_hint": "login",
        "domains": ("kugou.com",),
        "cookies": ("token", "userid", "kg_music_token", "KugooID"),
    },
    "qqmusic": {
        "name": "QQ音乐",
        "url": "https://y.qq.com/",
        "login_text": "登录",
        "frame_hint": "ptlogin",
        "domains": ("qq.com",),
        "cookies": ("qm_keyst", "qqmusic_key", "wxskey", "p_skey"),
    },
    "netease": {
        "name": "网易云音乐",
        "url": "https://music.163.com/",
        "login_text": "登录",
        "frame_hint": "music.163.com",
        "domains": ("music.163.com", "163.com"),
        "cookies": ("MUSIC_U",),
    },
}
LOGIN_REQUIRED = tuple(SOURCES)  # 这三家都要求扫码；咪咕、本地不需要

_LOCK = threading.RLock()
_SESSIONS: Dict[str, Dict[str, Any]] = {}


# ---------------- Cookie 存取 ----------------

def _read_cookies() -> Dict[str, str]:
    try:
        if COOKIE_FILE.exists():
            data = json.loads(COOKIE_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v}
    except (OSError, ValueError):
        pass
    return {}


def _write_cookies(data: Dict[str, str]) -> None:
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = COOKIE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, COOKIE_FILE)


def cookie_of(source: str) -> str:
    with _LOCK:
        return _read_cookies().get(str(source or "").strip().lower(), "")


def has_login(source: str) -> bool:
    return bool(cookie_of(source))


def _save_cookie(source: str, header: str) -> None:
    if not header:
        return
    with _LOCK:
        rows = _read_cookies()
        rows[source] = header
        _write_cookies(rows)


def logout(source: str) -> None:
    with _LOCK:
        rows = _read_cookies()
        rows.pop(source, None)
        _write_cookies(rows)


def _cookie_header(cookies: List[Dict[str, Any]], domains: tuple) -> str:
    picked: Dict[str, tuple] = {}
    for item in cookies:
        domain = str(item.get("domain") or "").lower()
        if not any(needle in domain for needle in domains):
            continue
        name = str(item.get("name") or "")
        value = item.get("value")
        if not name or value is None:
            continue
        old = picked.get(name)
        # 同名 Cookie 取域名更宽（点更少）的那个
        if old is None or domain.count(".") <= old[0].count("."):
            picked[name] = (domain, str(value))
    return "; ".join(f"{name}={value}" for name, (_d, value) in picked.items())


def _logged_in(cookies: List[Dict[str, Any]], cfg: Dict[str, Any]) -> bool:
    names = {str(item.get("name") or "") for item in cookies}
    return any(name in names for name in cfg["cookies"])


def _uin_of(header: str) -> str:
    match = re.search(r"(?:^|;\s*)uin=o?0*(\d+)", header or "")
    return match.group(1) if match else "0"


# ---------------- 浏览器：抓码 / 提示 ----------------

def _grab_qr(page: Any, hint: str) -> bytes:
    """截二维码元素本身：优先登录相关 frame，退化到整页，避免抓到头像。"""
    frames = list(page.frames)
    ordered = [fr for fr in frames if hint and hint in str(fr.url or "").lower()]
    ordered += [fr for fr in frames if fr not in ordered]
    for frame in ordered:
        try:
            locator = frame.locator("img, canvas")
            total = min(locator.count(), 40)
        except Exception:  # noqa: BLE001
            continue
        for index in range(total):
            element = locator.nth(index)
            try:
                box = element.bounding_box()
            except Exception:  # noqa: BLE001
                continue
            if not box:
                continue
            width, height = float(box.get("width") or 0), float(box.get("height") or 0)
            if width < 110 or width > 360 or abs(width - height) > 24:
                continue
            try:
                png = element.screenshot(timeout=3000)
            except Exception:  # noqa: BLE001
                continue
            if png and len(png) > 800:
                return png
    return b""


def _page_hint(page: Any) -> str:
    """扫码后站点常要求手机确认，抓页面文案让前端别一直显示「用手机扫码」。"""
    hints = (
        ("扫描成功", "已扫码，请在手机上确认"),
        ("请在手机", "已扫码，请在手机上确认"),
        ("确认登录", "已扫码，请在手机上确认"),
        ("安全验证", "站点要求安全验证，请在弹出的浏览器窗口里完成"),
        ("身份验证", "站点要求身份验证，请在弹出的浏览器窗口里完成"),
    )
    for needle, message in hints:
        try:
            if page.locator(f"text={needle}").count():
                return message
        except Exception:  # noqa: BLE001
            continue
    return ""


def _open_login_page(page: Any, cfg: Dict[str, Any]) -> None:
    """点开登录入口；点不到就算了，二维码可能本来就在页面上。"""
    for selector in (f"text={cfg['login_text']}", "[class*=login]", "a[href*=login]"):
        try:
            node = page.locator(selector).first
            if node.count():
                node.click(timeout=4000)
                page.wait_for_timeout(1200)
                return
        except Exception:  # noqa: BLE001
            continue


# ---------------- 后台线程 ----------------

def _worker(session: Dict[str, Any]) -> None:
    source = str(session["source"])
    cfg = SOURCES[source]
    playwright = context = None
    try:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        profile = PROFILE_DIR / source
        profile.mkdir(parents=True, exist_ok=True)
        errors: List[str] = []
        for kwargs in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(profile),
                    headless=False,
                    viewport={"width": 1280, "height": 900},
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
                    **kwargs,
                )
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{kwargs.get('channel') or 'chromium'}:{type(exc).__name__}")
        if context is None:
            raise RuntimeError("没有可用浏览器（" + "；".join(errors) + "）")

        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(cfg["url"], wait_until="domcontentloaded", timeout=45000)

        # 配置目录里已经登录过：直接算成功，不打扰用户扫码
        cookies = _cookies_of(context)
        if _logged_in(cookies, cfg):
            header = _cookie_header(cookies, cfg["domains"])
            if header:
                _save_cookie(source, header)
                session.update(status="ok", message="已登录（沿用了上次的登录状态）")
                print(f"[login] {source} 沿用已保存的登录态", flush=True)
                return

        if session.get("cancel"):
            session.update(status="canceled", message="已取消")
            return
        _open_login_page(page, cfg)

        ready_at = time.time() + 45
        qr = b""
        while time.time() < ready_at and not session.get("cancel"):
            if not qr:
                qr = _grab_qr(page, str(cfg["frame_hint"]))
                if qr:
                    session.update(qr=qr, status="waiting", message="用手机 App 扫码")
                    print(f"[login] {source} 二维码就绪，等待扫码", flush=True)
            cookies = _cookies_of(context)
            if _logged_in(cookies, cfg):
                break
            page.wait_for_timeout(700)
        if not qr:
            session.update(
                status="waiting",
                message=f"{cfg['name']}的二维码没抓到，请在弹出的浏览器窗口里扫码",
            )
            print(f"[login] {source} 未截到二维码，请在浏览器窗口里扫码", flush=True)

        deadline = time.time() + EXPIRES
        ticks = 0
        while time.time() < deadline:
            if session.get("cancel"):
                session.update(status="canceled", message="已取消")
                return
            cookies = _cookies_of(context)
            if _logged_in(cookies, cfg):
                time.sleep(1.2)  # 登录跳转过程中 Cookie 会再补几个
                cookies = _cookies_of(context)
                header = _cookie_header(cookies, cfg["domains"])
                if header:
                    _save_cookie(source, header)
                    session.update(qr=b"", status="ok", message="登录成功，Cookie 已保存")
                    print(f"[login] {source} 登录成功，已保存 cookie", flush=True)
                    return
            hint = _page_hint(page)
            if hint and session.get("status") != "scanned":
                session.update(status="scanned", message=hint)
                print(f"[login] {source} {hint}", flush=True)
            ticks += 1
            if ticks % 10 == 0:
                fresh = _grab_qr(page, str(cfg["frame_hint"]))
                if fresh:
                    session["qr"] = fresh
                print(f"[login] {source} 等待中 cookies={len(cookies)} url={str(page.url)[:80]}",
                      flush=True)
            time.sleep(1.2)
        session.update(status="expired", message="二维码过期了，重新点一次扫码登录")
        print(f"[login] {source} 二维码过期", flush=True)
    except Exception as exc:  # noqa: BLE001
        session.update(status="error", message=f"打开登录页失败：{type(exc).__name__}: {str(exc)[:100]}")
    finally:
        for closer in (lambda: context and context.close(),
                       lambda: playwright and playwright.stop()):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        session["done"] = True


def _cookies_of(context: Any) -> List[Dict[str, Any]]:
    try:
        return list(context.cookies() or [])
    except Exception:  # noqa: BLE001
        return []


# ---------------- 会话管理 ----------------

def _gc() -> None:
    now = time.time()
    with _LOCK:
        for sid in [
            key for key, item in _SESSIONS.items()
            if now - float(item.get("created") or 0) > EXPIRES + 180
        ]:
            _SESSIONS.pop(sid, None)


def _cancel_active(source: str) -> None:
    with _LOCK:
        olds = [
            item for item in _SESSIONS.values()
            if item.get("source") == source and not item.get("done")
        ]
    for item in olds:
        item["cancel"] = True


def _session_of(req: Any) -> Optional[Dict[str, Any]]:  # noqa: ANN001
    sid = str(req.q("sid") or "").strip()
    if not sid:
        return None
    return _SESSIONS.get(sid)


def _payload(req: Any) -> Dict[str, Any]:  # noqa: ANN001
    return req.payload if isinstance(req.payload, dict) else {}


# ---------------- handlers ----------------

def handle_start(req: Any) -> Any:  # noqa: ANN001
    source = str(_payload(req).get("source") or "").strip().lower()
    if source not in SOURCES:
        return fail("source 只能是 kugou / qqmusic / netease", 400)
    _gc()
    _cancel_active(source)
    sid = uuid.uuid4().hex[:16]
    session: Dict[str, Any] = {
        "sid": sid,
        "source": source,
        "status": "starting",
        "message": "正在打开登录页…",
        "qr": b"",
        "created": time.time(),
        "done": False,
    }
    with _LOCK:
        _SESSIONS[sid] = session
    threading.Thread(target=_worker, args=(session,), daemon=True,
                     name=f"login-{source}").start()
    return ok({"sid": sid, "source": source, "name": SOURCES[source]["name"],
               "expires_in": EXPIRES})


def handle_qr(req: Any) -> Any:  # noqa: ANN001
    session = _session_of(req)
    if session is None:
        return fail("扫码会话不存在或已过期", 404)
    deadline = time.time() + 8
    while not session.get("qr") and time.time() < deadline:
        if session.get("status") in ("error", "expired", "canceled", "ok"):
            break
        time.sleep(0.2)
    data = session.get("qr") or b""
    if not data:
        if session.get("status") in ("error", "expired", "canceled"):
            return fail(str(session.get("message") or "二维码不可用"), 410)
        return fail("二维码还没准备好", 425)
    return 200, Blob(data, "image/png")


def handle_status(req: Any) -> Any:  # noqa: ANN001
    session = _session_of(req)
    if session is None:
        return fail("扫码会话不存在或已过期", 404)
    source = str(session.get("source") or "")
    return ok({
        "sid": session.get("sid"),
        "source": source,
        "status": session.get("status"),
        "message": session.get("message") or "",
        "logged_in": has_login(source),
    })


def handle_logout(req: Any) -> Any:  # noqa: ANN001
    source = str(_payload(req).get("source") or "").strip().lower()
    if source not in SOURCES:
        return fail("source 只能是 kugou / qqmusic / netease", 400)
    _cancel_active(source)
    logout(source)
    return ok({"source": source, "logged_in": False})


routes: Dict[str, Any] = {
    "POST /songs/login/start": handle_start,
    "GET /songs/login/qr": handle_qr,
    "GET /songs/login/status": handle_status,
    "POST /songs/login/logout": handle_logout,
}
