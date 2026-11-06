"""酷狗扫码登录：Playwright 打开网页版，抠登录框里的二维码，登录后回传 Cookie。

只给 basic_tools 用：/扒歌 没登录时发二维码，扫完把 Cookie 交给 music_lib 取 VIP 直链。
"""

from __future__ import annotations

import asyncio
import base64
import time

from playwright.async_api import async_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

_SESS: dict[str, dict] = {}
_LOCK = asyncio.Lock()

_QR_DATA_URI = """() => {
  let best = null;
  for (const img of document.querySelectorAll('img')) {
    const s = img.src || '';
    if (!s.startsWith('data:image')) continue;
    const r = img.getBoundingClientRect();
    if (r.width < 100 || r.width > 400 || r.height < 100 || r.height > 400) continue;
    if (Math.abs(r.width - r.height) > 30) continue;
    if (!best || r.width > best.w) best = {src: s, w: r.width};
  }
  return best ? best.src : '';
}"""


def _cookie_header(cookies: list[dict]) -> str:
    """把 kugou.com 的 Cookie 拼成请求头；同名按域名更宽（点更少）的优先。"""
    picked: dict[str, tuple[str, str]] = {}
    for c in cookies:
        dom = str(c.get("domain") or "")
        if "kugou.com" not in dom:
            continue
        name = str(c.get("name") or "")
        val = c.get("value")
        if not name or val is None:
            continue
        old = picked.get(name)
        if old is None or dom.count(".") <= old[0].count("."):
            picked[name] = (dom, str(val))
    return "; ".join(f"{n}={v}" for n, (_d, v) in picked.items())


async def _close(browser, pw) -> None:
    try:
        await browser.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        await pw.stop()
    except Exception:  # noqa: BLE001
        pass


async def start_qr() -> tuple[bytes, str] | None:
    """返回 (二维码 PNG, 会话 id)；拿不到二维码返回 None。"""
    async with _LOCK:
        for old_sid in list(_SESS):
            await close(old_sid)
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=UA, viewport={"width": 1280, "height": 900}
        )
        page = await ctx.new_page()
        try:
            await page.goto(
                "https://www.kugou.com/", wait_until="domcontentloaded", timeout=60000
            )
            await page.wait_for_timeout(2500)
            try:
                await page.locator("text=登录").first.click(timeout=10000)
            except Exception:  # noqa: BLE001
                pass
            src = ""
            for _ in range(40):
                for fr in page.frames:
                    if "login-user.kugou.com" not in fr.url:
                        continue
                    try:
                        s = await fr.evaluate(_QR_DATA_URI)
                    except Exception:  # noqa: BLE001
                        s = ""
                    if isinstance(s, str) and s.startswith("data:image"):
                        src = s
                        break
                if src:
                    break
                await asyncio.sleep(0.5)
        except Exception:  # noqa: BLE001
            src = ""
        if not src:
            await _close(browser, pw)
            return None
        png = base64.b64decode(src.split(",", 1)[1])
        sid = str(int(time.time()))
        _SESS[sid] = {"pw": pw, "browser": browser, "context": ctx}
        return png, sid


async def wait_login(sid: str, timeout: int = 150) -> str:
    """等扫码完成；成功返回 Cookie 头，超时返回空串。"""
    s = _SESS.get(sid)
    if not s:
        return ""
    ctx = s["context"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(2)
        try:
            cookies = await ctx.cookies()
        except Exception:  # noqa: BLE001
            break
        if {c.get("name") for c in cookies} & {
            "token", "userid", "kg_userid", "KugooID", "kg_music_token"
        }:
            await asyncio.sleep(1.5)
            try:
                cookies = await ctx.cookies()
            except Exception:  # noqa: BLE001
                pass
            header = _cookie_header(cookies)
            await close(sid)
            return header
    await close(sid)
    return ""


async def close(sid: str) -> None:
    s = _SESS.pop(sid, None)
    if s:
        await _close(s["browser"], s["pw"])
