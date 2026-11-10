"""B站扫码登录与登录状态检查。"""
from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import parse_qs, urlsplit

import aiohttp

from .bili_api import _BILI_NAV, _bili_headers
from .models import DEFAULT_CFG, HEADERS

_BILI_QR_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"


_BILI_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"


_COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")


def _cookie_from_login_url(url: str) -> str:
    """登录成功的回调 URL 里带着 SESSDATA 等，抠出来拼成 Cookie 头。"""
    q = parse_qs(urlsplit(str(url or "")).query)
    parts = []
    for k in _COOKIE_KEYS:
        v = q.get(k) or []
        if v and v[0]:
            parts.append(f"{k}={v[0]}")
    return "; ".join(parts)


def _cookie_from_set_cookie(items: list[str]) -> str:
    """扫码成功时 B站把 SESSDATA 等放在 Set-Cookie 头里，这里抠出来。"""
    got: dict[str, str] = {}
    for raw in items:
        head = str(raw or "").split(";", 1)[0].strip()
        if "=" not in head:
            continue
        k, v = head.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in _COOKIE_KEYS and v:
            got[k] = v
    return "; ".join(f"{k}={got[k]}" for k in _COOKIE_KEYS if k in got)


async def bili_qr_generate() -> dict | None:
    """申请一个登录二维码：返回 {"key": qrcode_key, "url": 二维码内容}，失败 None。"""
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as sess:
            async with sess.get(
                _BILI_QR_GEN, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    return None
                p = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    data = (p or {}).get("data") or {}
    key = str(data.get("qrcode_key") or "")
    url = str(data.get("url") or "")
    return {"key": key, "url": url} if key and url else None


async def bili_qr_poll(key: str) -> tuple[str, str]:
    """查一次扫码状态：('ok', cookie) / ('scanned', '') / ('expired', '') / ('wait', '')。"""
    setc: list[str] = []
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as sess:
            async with sess.get(
                _BILI_QR_POLL,
                params={"qrcode_key": key},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                p = await r.json(content_type=None)
                setc = list(r.headers.getall("Set-Cookie", []))
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return "wait", ""
    data = (p or {}).get("data") or {}
    status = data.get("code")
    if status == 0:
        # 回调 URL 有时带参数、有时只在 Set-Cookie 里给，两处都取
        return "ok", (
            _cookie_from_login_url(str(data.get("url") or ""))
            or _cookie_from_set_cookie(setc)
        )
    if status == 86090:
        return "scanned", ""
    if status == 86038:
        return "expired", ""
    return "wait", ""


def bili_qr_png(text: str, path: str) -> str:
    """把二维码内容画成 PNG（qrcode 依赖已装）；失败抛异常由调用方兜底。"""
    import qrcode

    img = qrcode.make(text)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)
    return path


async def bili_login_status(cfg: dict | None = None) -> str:
    """用 nav 接口看当前 cookie 还活着没：'已登录：昵称（UID xxx）' 或 '未登录'。"""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    headers = _bili_headers(cfg, "https://www.bilibili.com/")
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as sess:
            async with sess.get(
                _BILI_NAV, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                p = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return "未登录（接口没通）"
    d = (p or {}).get("data") or {}
    if (p or {}).get("code") == 0 and d.get("isLogin"):
        return f"已登录：{d.get('uname') or '?'}（UID {d.get('mid')}）"
    return "未登录"
