"""抖音 / 快手 / 西瓜 扫码登录：真浏览器打开登录页，截二维码，扫完导出 Cookie。

这三家的扫码接口都有签名/风控，纯 HTTP 拿不到二维码，只能借 Playwright 跑真浏览器。
"""

from __future__ import annotations

import asyncio
import base64
import uuid

_PLATFORMS = {
    "douyin": {
        "name": "抖音",
        "home": "https://www.douyin.com/",
        "login": ['[data-e2e="login-button"]', "text=登录", "button:has-text('登录')"],
        "qr": [
            '[data-e2e*="qrcode"] img',
            '[data-e2e*="qrcode"] canvas',
            'img[src*="qrcode"]',
            'img[src*="qr"]',
            "canvas",
        ],
        "cookie_keys": ("sessionid", "sid_tt", "passport_csrf_token"),
    },
    "kuaishou": {
        "name": "快手",
        "home": "https://www.kuaishou.com/",
        "login": [
            "text=立即登录",
            "text=登录",
            "button:has-text('立即登录')",
            "button:has-text('登录')",
        ],
        "qr": ['img[src*="qrcode"]', 'img[src*="qr"]', "canvas"],
        "cookie_keys": ("kuaishou.server.web_st", "passToken", "userId"),
    },
    "ixigua": {
        "name": "西瓜",
        "home": "https://www.ixigua.com/",
        "login": ["text=登录", "button:has-text('登录')"],
        "qr": ['img[src*="qrcode"]', 'img[src*="qr"]', "canvas"],
        "cookie_keys": ("sessionid", "sid_tt", "uid_tt"),
        # 西瓜 PC 网页版已下线，站点只会跳 App 下载页，没有可扫码的登录入口
        "no_web_login": True,
        "reason": "西瓜网页版已经下线，没有网页扫码入口。",
    },
}

_SESS: dict[str, tuple] = {}
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

_QR_DATA_URI = """
() => {
  const best = [];
  document.querySelectorAll('img').forEach(el => {
    const src = el.src || el.getAttribute('src') || '';
    if (!src.startsWith('data:image')) return;
    const r = el.getBoundingClientRect();
    if (r.width < 120 || r.height < 120 || r.width > 420 || r.height > 420) return;
    const ratio = r.width / r.height;
    if (ratio < 0.85 || ratio > 1.15) return;
    best.push({src: src, area: r.width * r.height});
  });
  best.sort((a, b) => b.area - a.area);
  return best.length ? best[0].src : '';
}
"""


def supported(platform: str) -> bool:
    spec = _PLATFORMS.get(platform) or {}
    return bool(spec) and not spec.get("no_web_login")


def unavailable_reason(platform: str) -> str:
    return str((_PLATFORMS.get(platform) or {}).get("reason") or "")


def platform_name(platform: str) -> str:
    return _PLATFORMS.get(platform, {}).get("name", platform)


async def start_qr(platform: str) -> tuple[bytes, str] | None:
    """打开登录页截二维码，返回 (PNG 字节, 会话 id)；失败返回 None。"""
    spec = _PLATFORMS.get(platform)
    if not spec or spec.get("no_web_login"):
        return None
    # 只允许两个浏览器会话同时存在，超出先踢掉最老的，防止被刷爆内存
    while len(_SESS) >= 2:
        old_sid = next(iter(_SESS))
        try:
            await close(old_sid)
        except Exception:  # noqa: BLE001
            _SESS.pop(old_sid, None)
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = await browser.new_context(
        user_agent=_UA,
        viewport={"width": 1280, "height": 900},
        locale="zh-CN",
    )
    page = await ctx.new_page()
    try:
        await page.goto(spec["home"], wait_until="domcontentloaded", timeout=30000)
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(2)
    for sel in spec["login"]:
        try:
            await page.click(sel, timeout=3000)
            await asyncio.sleep(2)
            break
        except Exception:  # noqa: BLE001
            continue
    png: bytes | None = None
    # 抖音的码是 img 内嵌 base64，直接抠出来比整页截图清楚
    try:
        data_uri = await page.evaluate(_QR_DATA_URI)
        if isinstance(data_uri, str) and data_uri.startswith("data:image") and "," in data_uri:
            png = base64.b64decode(data_uri.split(",", 1)[1])
    except Exception:  # noqa: BLE001
        png = None
    if not png:
        for sel in spec["qr"]:
            try:
                el = await page.query_selector(sel)
                if el is not None:
                    box = await el.bounding_box()
                    if box and box["width"] >= 80 and box["height"] >= 80:
                        png = await el.screenshot()
                        break
            except Exception:  # noqa: BLE001
                continue
    if not png:
        try:
            png = await page.screenshot(full_page=False)
        except Exception:  # noqa: BLE001
            png = None
    if not png:
        await _close(browser, pw)
        return None
    sid = uuid.uuid4().hex
    _SESS[sid] = (browser, pw, ctx, page, platform)
    return png, sid


async def wait_login(sid: str, timeout: int = 180) -> str:
    """轮询浏览器 Cookie，扫完返回整条 Cookie；超时返回空串。"""
    item = _SESS.get(sid)
    if not item:
        return ""
    browser, pw, ctx, _page, platform = item
    spec = _PLATFORMS.get(platform) or {}
    keys = spec.get("cookie_keys") or ()
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    try:
        while loop.time() < end:
            ck = await ctx.cookies()
            names = {c.get("name") for c in ck}
            if any(k in names for k in keys):
                return _cookie_header(ck)
            await asyncio.sleep(3)
        return ""
    finally:
        await close(sid)


def _cookie_header(cookies: list) -> str:
    """同名 Cookie 只留最宽域那条，拼成请求头。"""
    picked: dict[str, tuple] = {}
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if not name or not value:
            continue
        domain = c.get("domain") or ""
        old = picked.get(name)
        # 域越宽（".douyin.com" 这种）优先级越高
        if old is None or len(domain) < len(old[0] or ""):
            picked[name] = (domain, value)
    return "; ".join(f"{k}={v}" for k, (_d, v) in picked.items())


async def _close(browser, pw) -> None:
    try:
        await browser.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        await pw.stop()
    except Exception:  # noqa: BLE001
        pass


async def close(sid: str) -> None:
    item = _SESS.pop(sid, None)
    if item:
        browser, pw, _ctx, _page, _p = item
        await _close(browser, pw)
