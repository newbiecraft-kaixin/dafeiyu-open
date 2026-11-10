"""video.login_api：扫码登录（B站 / 抖音 / 快手）。

用户不该被逼着去浏览器里翻 Cookie，所以这里直接给二维码：

  B站    走官方 passport 扫码接口（generate + poll），不需要浏览器
  抖音   用 Playwright 打开 www.douyin.com 的登录弹窗，取二维码图片
  快手   用 Playwright 打开 passport.kuaishou.com，切到「扫码登录」再取二维码

浏览器二维码每秒刷新一次，页面只需要反复拉 /video/login/qr。
登录成功后 Cookie 直接落盘：B站写 feed.json 与 parse.json 的 cookie.bili，
抖音 / 快手写 parse.json 对应项。前端不会看到 Cookie 原值。

接口
  POST /video/login/start    {platform} → {sid, platform, name, expires_in}
  GET  /video/login/qr       ?sid=      → image/png
  GET  /video/login/status   ?sid=      → {status, message}
  GET  /video/login/state               → {cookies:{bilibili,douyin,kuaishou,ixigua}}
  POST /video/login/cancel   {sid}
  POST /video/login/logout   {platform}

status 取值：starting / waiting / scanned / ok / expired / canceled / error
"""
from __future__ import annotations

import base64
import io
import json
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import Blob, fail, ok  # noqa: E402

import feed_api  # noqa: E402
import parse_api  # noqa: E402

PLATFORMS = {"bilibili": "B站", "douyin": "抖音", "kuaishou": "快手"}
EXPIRES = 300

_BILI_QR_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_BILI_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"
_BILI_COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")

_BROWSER_URLS = {
    "douyin": "https://www.douyin.com/user/self?show_login=1",
    "kuaishou": "https://passport.kuaishou.com/pc/account/login/?sid=kuaishou.web.cp.api",
}
# 出现这些 Cookie 才判定登录成功（快手在登录前也会种 _did 等，不能算）
_LOGIN_COOKIES = {
    "douyin": ("sessionid", "sessionid_ss", "sid_tt"),
    "kuaishou": ("kuaishou.server.web_st", "kuaishou.server.web_ph", "userId"),
}
_Q = "() => {const out=[];for(const el of document.querySelectorAll('img')){const s=el.getAttribute('src')||'';" \
     "if(!s.startsWith('data:image/'))continue;const r=el.getBoundingClientRect();" \
     "if(r.width>=110&&r.height>=110)out.push(s);}return out.length?out[0]:'';}"

_SESSIONS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.RLock()

_UA = parse_api.UA


# ---------------- 小工具 ----------------

def _payload(req: Any) -> Dict[str, Any]:  # noqa: ANN001
    return req.payload if isinstance(req.payload, dict) else {}


def _get_json(url: str, headers: Optional[Dict[str, str]] = None,
              timeout: float = 12.0) -> Tuple[Optional[Dict[str, Any]], Any]:
    """直连（不吃系统代理），返回 (JSON, 响应对象)；失败 (None, None)。"""
    hdrs = headers or {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
    try:
        with parse_api._http(url, hdrs, timeout=timeout) as response:
            text = response.read().decode("utf-8", "ignore")
            try:
                body = json.loads(text)
            except ValueError:
                body = None
            return (body if isinstance(body, dict) else None), response
    except Exception:  # noqa: BLE001 - 网络问题交给状态提示
        return None, None


def _set_cookies(response: Any) -> List[str]:
    try:
        return list(response.headers.get_all("Set-Cookie") or [])
    except Exception:  # noqa: BLE001
        return []


def _cookie_from_url(url: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(url or "")).query)
    parts = []
    for key in _BILI_COOKIE_KEYS:
        values = query.get(key) or []
        if values and values[0]:
            parts.append(f"{key}={values[0]}")
    return "; ".join(parts)


def _cookie_from_set_cookie(items: List[str]) -> str:
    got: Dict[str, str] = {}
    for raw in items:
        head = str(raw or "").split(";", 1)[0].strip()
        if "=" not in head:
            continue
        key, value = head.split("=", 1)
        key, value = key.strip(), value.strip()
        if key in _BILI_COOKIE_KEYS and value:
            got[key] = value
    return "; ".join(f"{key}={got[key]}" for key in _BILI_COOKIE_KEYS if key in got)


def _qr_png(text: str) -> bytes:
    """把二维码内容画成 PNG；qrcode 没装就返回空（前端会提示失败）。"""
    try:
        import qrcode

        image = qrcode.make(text, box_size=8, border=2)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001
        return b""


def _cookie_flags() -> Dict[str, bool]:
    try:
        parse_cookie = parse_api._load_cfg()["cookie"]  # noqa: SLF001 - 同服务内部复用
        feed_cookie = feed_api._load_config().get("cookie")  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return {"bilibili": False, "douyin": False, "kuaishou": False, "ixigua": False}
    return {
        "bilibili": bool(parse_cookie.get("bili") or feed_cookie),
        "douyin": bool(parse_cookie.get("douyin")),
        "kuaishou": bool(parse_cookie.get("kuaishou")),
        "ixigua": bool(parse_cookie.get("ixigua")),
    }


def _save_cookie(platform: str, cookie: str) -> None:
    """B站要同时写给「刷视频」（点赞用 bili_jct）和「看视频」（AI 字幕）。"""
    cfg = parse_api._load_cfg()  # noqa: SLF001
    key = "bili" if platform == "bilibili" else platform
    cfg["cookie"][key] = cookie
    parse_api._save_cfg(cfg)  # noqa: SLF001
    if platform == "bilibili":
        feed_cfg = feed_api._load_config()  # noqa: SLF001
        feed_cfg["cookie"] = cookie
        feed_api._save_config(feed_cfg)  # noqa: SLF001


def _clear_cookie(platform: str) -> None:
    _save_cookie(platform, "")


def _bili_uname(cookie: str) -> str:
    payload, _resp = _get_json(_BILI_NAV, {
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie,
    }, timeout=6)
    data = (payload or {}).get("data") or {}
    if (payload or {}).get("code") == 0 and data.get("isLogin"):
        return str(data.get("uname") or "")
    return ""


# ---------------- B站：官方扫码接口 ----------------

def _bili_poll(key: str) -> Tuple[str, str]:
    url = _BILI_QR_POLL + "?" + urllib.parse.urlencode({"qrcode_key": key})
    payload, response = _get_json(url)
    data = (payload or {}).get("data") or {}
    code = data.get("code")
    if code == 0:
        cookie = _cookie_from_url(str(data.get("url") or "")) or _cookie_from_set_cookie(
            _set_cookies(response)
        )
        return "ok", cookie
    if code == 86090:
        return "scanned", ""
    if code == 86038:
        return "expired", ""
    return "wait", ""


def _bili_worker(session: Dict[str, Any]) -> None:
    try:
        payload, _resp = _get_json(_BILI_QR_GEN)
        data = (payload or {}).get("data") or {}
        key = str(data.get("qrcode_key") or "")
        url = str(data.get("url") or "")
        if not key or not url:
            session.update(status="error", message="B站二维码没申请下来（网络或风控），稍后再试")
            return
        png = _qr_png(url)
        if not png:
            session.update(status="error", message="生成二维码图片失败：本机缺 qrcode 依赖")
            return
        session.update(key=key, qr=png, status="waiting", message="用 B站 App 扫码")

        deadline = time.time() + EXPIRES
        while time.time() < deadline:
            if session.get("cancel"):
                session.update(status="canceled", message="已取消")
                return
            flag, cookie = _bili_poll(key)
            if flag == "ok":
                if not cookie:
                    session.update(status="error", message="登录成功但没取到 Cookie，重试一次")
                    return
                _save_cookie("bilibili", cookie)
                name = _bili_uname(cookie)
                session.update(
                    status="ok", qr=b"",
                    message=f"已登录：{name}" if name else "登录成功，Cookie 已保存",
                )
                return
            if flag == "scanned":
                session.update(status="scanned", message="已扫码，请在手机上点确认")
            elif flag == "expired":
                session.update(status="expired", message="二维码过期了，重新点一次扫码登录")
                return
            time.sleep(1.5)
        session.update(status="expired", message="二维码过期了，重新点一次扫码登录")
    except Exception as exc:  # noqa: BLE001 - 扫码失败只提示，不带崩服务
        session.update(status="error", message=f"B站扫码失败：{type(exc).__name__}")
    finally:
        session["done"] = True


# ---------------- 抖音 / 快手：Playwright 取二维码 ----------------

def _launch_browser(playwright: Any, headless: bool = True):  # noqa: ANN201
    args = ["--disable-blink-features=AutomationControlled", "--no-first-run"]
    if not headless:
        args.append("--start-maximized")
    errors: List[str] = []
    for kwargs in (
        {"channel": "msedge", "headless": headless},
        {"channel": "chrome", "headless": headless},
        {"headless": headless},
    ):
        try:
            return playwright.chromium.launch(args=args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{kwargs.get('channel') or 'chromium'}:{type(exc).__name__}")
    raise RuntimeError("没有可用浏览器（" + "；".join(errors) + "），请先 playwright install")


def _grab_qr(page: Any) -> bytes:
    try:
        src = str(page.evaluate(_Q) or "")
    except Exception:  # noqa: BLE001
        return b""
    if not src.startswith("data:image") or "," not in src:
        return b""
    head, data = src.split(",", 1)
    if "base64" not in head:
        return b""
    try:
        return base64.b64decode(data)
    except Exception:  # noqa: BLE001
        return b""


def _cookie_string(cookies: List[Dict[str, Any]], platform: str) -> str:
    keyword = "douyin" if platform == "douyin" else "kuaishou"
    parts = []
    for item in cookies:
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "").lower()
        if not name or not value or keyword not in domain:
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _page_hint(page: Any) -> str:
    """扫码后页面会给出确认/验证提示，抓出来让前端别一直显示“用手机扫码”。"""
    hints = (
        ("扫描成功", "已扫码，请在手机上确认"),
        ("请在手机", "已扫码，请在手机上确认"),
        ("确认登录", "已扫码，请在手机上确认"),
        ("安全验证", "抖音要求安全验证，请在弹出的浏览器窗口里完成"),
        ("身份验证", "抖音要求身份验证，请在弹出的浏览器窗口里完成"),
    )
    for needle, message in hints:
        try:
            if page.locator(f"text={needle}").count():
                return message
        except Exception:  # noqa: BLE001
            continue
    return ""


def _browser_worker(session: Dict[str, Any]) -> None:
    platform = str(session["platform"])
    playwright = browser = context = None
    try:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        if platform == "douyin":
            # 抖音风控对无头浏览器很敏感：换成有头窗口 + 持久化配置目录，
            # 既能让用户完成安全验证，扫码后的 Cookie 也能留住。
            profile = _HERE.parent / "data" / "browser" / "douyin"
            profile.mkdir(parents=True, exist_ok=True)
            errors: List[str] = []
            for kwargs in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
                try:
                    context = playwright.chromium.launch_persistent_context(
                        str(profile), headless=False,
                        viewport={"width": 1280, "height": 900},
                        locale="zh-CN", timezone_id="Asia/Shanghai",
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
        else:
            browser = _launch_browser(playwright)
            context = browser.new_context(
                user_agent=_UA, viewport={"width": 1280, "height": 900},
                locale="zh-CN", timezone_id="Asia/Shanghai",
            )
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            page = context.new_page()
        page.goto(_BROWSER_URLS[platform], wait_until="domcontentloaded", timeout=45000)
        if session.get("cancel"):
            session.update(status="canceled", message="已取消")
            return
        page.wait_for_timeout(1600)
        if platform == "kuaishou":
            try:
                tab = page.locator("text=扫码登录").first
                if tab.count():
                    tab.click(timeout=5000)
            except Exception:  # noqa: BLE001 - 已经是扫码页就跳过
                pass

        qr = b""
        grab_until = time.time() + 45
        while time.time() < grab_until and not session.get("cancel"):
            qr = _grab_qr(page)
            if qr:
                break
            page.wait_for_timeout(700)
        if not qr:
            session.update(status="error", message="没等到二维码（页面可能改了），先用手动 Cookie 兜底")
            return
        session.update(qr=qr, status="waiting", message="用手机 App 扫码")
        print(f"[login] {platform} 二维码就绪，等待扫码", flush=True)

        deadline = time.time() + EXPIRES
        ticks = 0
        while time.time() < deadline:
            if session.get("cancel"):
                session.update(status="canceled", message="已取消")
                return
            fresh = _grab_qr(page)
            if fresh:
                session["qr"] = fresh
            try:
                cookies = context.cookies()
            except Exception:  # noqa: BLE001
                cookies = []
            names = {item.get("name"): item.get("value") for item in cookies}
            if any(names.get(name) for name in _LOGIN_COOKIES[platform]):
                cookie = _cookie_string(cookies, platform)
                if cookie:
                    _save_cookie(platform, cookie)
                    session.update(status="ok", qr=b"", message="登录成功，Cookie 已保存")
                    print(f"[login] {platform} 登录成功，已保存 cookie", flush=True)
                    return
            hint = _page_hint(page)
            if hint and session.get("status") != "scanned":
                session.update(status="scanned", message=hint)
                print(f"[login] {platform} {hint}", flush=True)
            ticks += 1
            if ticks % 8 == 0:
                print(f"[login] {platform} 等待中 cookies={len(names)} url={str(page.url)[:90]}",
                      flush=True)
            page.wait_for_timeout(1200)
        session.update(status="expired", message="二维码过期了，重新点一次扫码登录")
        print(f"[login] {platform} 二维码过期", flush=True)
    except Exception as exc:  # noqa: BLE001
        session.update(status="error", message=f"打开登录页失败：{type(exc).__name__}: {str(exc)[:100]}")
    finally:
        for closer in (lambda: context and context.close(),
                       lambda: browser and browser.close(),
                       lambda: playwright and playwright.stop()):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        session["done"] = True


# ---------------- 会话管理 ----------------

def _gc() -> None:
    now = time.time()
    with _LOCK:
        dead = [
            sid for sid, item in _SESSIONS.items()
            if now - float(item.get("created") or 0) > EXPIRES + 180
        ]
        for sid in dead:
            _SESSIONS.pop(sid, None)


def _cancel_active(platform: str) -> None:
    """把同平台还没结束的会话标成取消（旧浏览器会在后台自己退出）。

    不在这里等它退干净：Playwright 打开登录页可能要好几十秒，
    等下去只会让用户干等着点不动按钮。
    """
    with _LOCK:
        olds = [
            item for item in _SESSIONS.values()
            if item.get("platform") == platform and not item.get("done")
        ]
    for item in olds:
        item["cancel"] = True


def _session_of(req: Any) -> Optional[Dict[str, Any]]:  # noqa: ANN001
    sid = str(req.q("sid") or "").strip()
    if not sid:
        return None
    return _SESSIONS.get(sid)


# ---------------- handlers ----------------

def handle_start(req: Any) -> Any:  # noqa: ANN001
    platform = str(_payload(req).get("platform") or "").strip().lower()
    if platform not in PLATFORMS:
        return fail("platform 只能是 bilibili / douyin / kuaishou", 400)
    _cancel_active(platform)

    sid = uuid.uuid4().hex[:16]
    session: Dict[str, Any] = {
        "sid": sid,
        "platform": platform,
        "status": "starting",
        "message": "正在生成二维码…",
        "qr": b"",
        "created": time.time(),
        "cancel": False,
        "done": False,
    }
    with _LOCK:
        _SESSIONS[sid] = session
    _gc()

    worker = _bili_worker if platform == "bilibili" else _browser_worker
    threading.Thread(target=worker, args=(session,), daemon=True,
                     name=f"login-{platform}").start()
    return ok({
        "sid": sid,
        "platform": platform,
        "name": PLATFORMS[platform],
        "expires_in": EXPIRES,
    })


def handle_qr(req: Any) -> Any:  # noqa: ANN001
    session = _session_of(req)
    if session is None:
        return fail("扫码会话不存在或已过期", 404)
    # 二维码还在生成（浏览器唤起要几秒）时先等一会儿，避免前端拿到一张坏图
    deadline = time.time() + 8
    while not session.get("qr") and time.time() < deadline:
        if session.get("status") in ("error", "expired", "canceled", "ok"):
            break
        time.sleep(0.2)
    data = session.get("qr") or b""
    if not data:
        status = str(session.get("status") or "")
        if status in ("error", "expired", "canceled"):
            return fail(str(session.get("message") or "二维码不可用"), 410)
        return fail("二维码还没准备好", 425)
    return 200, Blob(data, "image/png")


def handle_status(req: Any) -> Any:  # noqa: ANN001
    session = _session_of(req)
    if session is None:
        return fail("扫码会话不存在或已过期", 404)
    return ok({
        "sid": session.get("sid"),
        "platform": session.get("platform"),
        "status": session.get("status"),
        "message": session.get("message") or "",
    })


def handle_cancel(req: Any) -> Any:  # noqa: ANN001
    sid = str(_payload(req).get("sid") or "").strip()
    session = _SESSIONS.get(sid)
    if session is None:
        return ok({"canceled": False})
    session["cancel"] = True
    if session.get("status") not in ("ok", "error"):
        session["status"] = "canceled"
        session["message"] = "已取消"
    return ok({"canceled": True})


def handle_state(_req: Any) -> Any:  # noqa: ANN001
    with _LOCK:
        active = [
            {"platform": item["platform"], "status": item.get("status")}
            for item in _SESSIONS.values() if not item.get("done")
        ]
    return ok({"cookies": _cookie_flags(), "platforms": PLATFORMS, "active": active})


def handle_logout(req: Any) -> Any:  # noqa: ANN001
    platform = str(_payload(req).get("platform") or "").strip().lower()
    if platform not in PLATFORMS:
        return fail("platform 只能是 bilibili / douyin / kuaishou", 400)
    _cancel_active(platform)
    _clear_cookie(platform)
    return ok({"platform": platform, "cookies": _cookie_flags()})


def status() -> Dict[str, Any]:
    """给 /health 用：模块就绪、支持平台、Cookie 是否已设置。"""
    return {
        "ready": True,
        "platforms": list(PLATFORMS.values()),
        "cookies": _cookie_flags(),
    }


routes: Dict[str, Any] = {
    "POST /video/login/start": handle_start,
    "GET /video/login/qr": handle_qr,
    "GET /video/login/status": handle_status,
    "GET /video/login/state": handle_state,
    "POST /video/login/cancel": handle_cancel,
    "POST /video/login/logout": handle_logout,
}
