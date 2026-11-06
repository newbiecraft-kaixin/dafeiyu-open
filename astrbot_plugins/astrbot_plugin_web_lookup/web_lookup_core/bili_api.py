"""B站接口基础：请求头、WBI 签名、字幕、AI 总结与评论。"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from urllib.parse import urlencode

import aiohttp

from .models import UA

_BILI_VIEW = "https://api.bilibili.com/x/web-interface/view"


_BILI_TAGS = "https://api.bilibili.com/x/tag/archive/tags"


_BILI_DETAIL = "https://api.bilibili.com/x/web-interface/view/detail"


_BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"


_BILI_CONCLUSION = "https://api.bilibili.com/x/web-interface/view/conclusion/get"


_WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def _wbi_mixin(img_url, sub_url) -> str:
    """nav 返回的 img_url/sub_url 文件名（去扩展名）拼成 64 位串，按表重排取 32 位。"""
    raw = ""
    for u in (img_url, sub_url):
        name = str(u or "").rsplit("/", 1)[-1]
        raw += name.split(".")[0]
    if len(raw) < 64:
        return ""
    return "".join(raw[i] for i in _WBI_TAB)[:32]


async def _bili_wbi_mixin(sess: aiohttp.ClientSession, headers: dict) -> str:
    try:
        async with sess.get(_BILI_NAV, headers=headers) as r:
            if r.status != 200:
                return ""
            p = await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return ""
    wbi = ((p or {}).get("data") or {}).get("wbi_img") or {}
    return _wbi_mixin(wbi.get("img_url"), wbi.get("sub_url"))


def _wbi_signed(params: dict, mixin: str) -> dict:
    """加 wts 时间戳、排序 urlencode，再 md5(query + mixin) 作 w_rid。"""
    q = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in params.items()}
    q["wts"] = int(time.time())
    query = urlencode(sorted(q.items()))
    q["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return q


async def _bili_conclusion(
    sess: aiohttp.ClientSession, bvid: str, aid, cid, up_mid, headers: dict,
    max_chars: int, mixin: str = "",
) -> str:
    """B站官方 AI 视频总结；没登录态（-101）或不支持（-403）就返回空串。"""
    if not bvid or not cid:
        return ""
    mixin = mixin or await _bili_wbi_mixin(sess, headers)
    if not mixin:
        return ""
    params = _wbi_signed(
        {"bvid": bvid, "cid": cid, "up_mid": up_mid or 0}, mixin
    )
    try:
        async with sess.get(_BILI_CONCLUSION, params=params, headers=headers) as r:
            if r.status != 200:
                return ""
            p = await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(p, dict) or p.get("code") != 0:
        return ""
    mr = (p.get("data") or {}).get("model_result") or {}
    summary = re.sub(r"\s+", " ", str(mr.get("summary") or "")).strip()
    outline = [
        str(o.get("title") or "").strip()
        for o in (mr.get("outline") or [])
        if isinstance(o, dict) and str(o.get("title") or "").strip()
    ]
    text = summary
    if outline:
        text += ("；" if text else "") + "分段：" + "、".join(outline[:8])
    return text[: max(400, int(max_chars))]


def _bili_hot_replies(detail: dict, n: int = 3) -> list[str]:
    """view/detail 里的热门评论（Reply.replies），各截 80 字。"""
    rows = ((detail or {}).get("Reply") or {}).get("replies") or []
    out: list[str] = []
    for it in rows[: max(1, n)]:
        if not isinstance(it, dict):
            continue
        msg = re.sub(
            r"\s+", " ", str((it.get("content") or {}).get("message") or "")
        ).strip()
        if msg:
            out.append(msg[:80])
    return out


def _bili_date(v) -> str:
    try:
        t = int(v or 0)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(t))


async def _bili_hot_comments(
    sess: aiohttp.ClientSession, aid, headers: dict, mixin: str, n: int = 3
) -> list[str]:
    """热门评论（x/v2/reply/wbi/main，未登录也能拿前 3 条），返回去重后的短句。"""
    if not aid or not mixin:
        return []
    params = _wbi_signed(
        {
            "oid": aid,
            "type": 1,
            "mode": 3,
            "pagination_str": '{"offset":""}',
            "plat": 1,
            "seek_rpid": "",
            "web_location": 1315875,
        },
        mixin,
    )
    try:
        async with sess.get(
            "https://api.bilibili.com/x/v2/reply/wbi/main", params=params, headers=headers
        ) as r:
            if r.status != 200:
                return []
            p = await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(p, dict) or p.get("code") != 0:
        return []
    out: list[str] = []
    for it in ((p.get("data") or {}).get("replies") or [])[: max(1, n)]:
        if not isinstance(it, dict):
            continue
        msg = re.sub(
            r"\s+", " ", str((it.get("content") or {}).get("message") or "")
        ).strip()
        if msg:
            out.append(msg[:80])
    return out


async def _bili_reply_list(sess, aid, headers: dict, n: int = 10) -> list[str]:
    """老评论接口（x/v2/reply）：带登录 Cookie 一次能拿 20 条，优先用它凑前 N 条。"""
    if not aid:
        return []
    try:
        async with sess.get(
            "https://api.bilibili.com/x/v2/reply",
            params={"type": 1, "oid": aid, "pn": 1, "ps": max(10, n * 2), "sort": 2},
            headers=headers,
        ) as r:
            if r.status != 200:
                return []
            p = await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(p, dict) or p.get("code") != 0:
        return []
    out: list[str] = []
    for it in ((p.get("data") or {}).get("replies") or []):
        if not isinstance(it, dict):
            continue
        msg = re.sub(
            r"\s+", " ", str((it.get("content") or {}).get("message") or "")
        ).strip()
        if msg and msg not in out:
            out.append(msg[:80])
    return out[: max(1, n)]


async def _bili_subtitles(
    sess: aiohttp.ClientSession, aid, cid, cookie: str, max_chars: int,
    bvid: str = "", duration: float = 0.0,
) -> str:
    """拉 B站字幕并拼成一段纯文本；没登录态/没字幕就返回空串。

    只给 aid 时 B站 会把别的视频的 AI 字幕串过来（实测 33 秒视频返回几分钟的烧烤字幕），
    所以优先 bvid+cid，并用「字幕总时长不能超出视频时长」做二次校验。
    """
    if not cid or not (bvid or aid):
        return ""
    ref = (
        f"https://www.bilibili.com/video/{bvid}"
        if bvid
        else f"https://www.bilibili.com/video/av{aid}"
    )
    headers = {"Referer": ref}
    if cookie:
        headers["Cookie"] = cookie
    params = {"bvid": bvid, "cid": cid} if bvid else {"aid": aid, "cid": cid}
    try:
        async with sess.get(
            "https://api.bilibili.com/x/player/v2",
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return ""
            p = await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return ""
    subinfo = ((p or {}).get("data") or {}).get("subtitle") or {}
    subs = subinfo.get("subtitles") or []
    if not isinstance(subs, list) or not subs:
        return ""
    # 中文优先，其次英文，最后随便挑一条
    def rank(s):
        lan = str(s.get("lan") or "")
        return 0 if lan.startswith("zh") else 1 if lan.startswith("en") else 2
    usable = [s for s in subs if isinstance(s, dict) and s.get("subtitle_url")]
    if not usable:
        return ""
    usable.sort(key=rank)
    for best in usable:
        u = str(best["subtitle_url"])
        if u.startswith("//"):
            u = "https:" + u
        try:
            async with sess.get(u, headers=headers) as r:
                if r.status != 200:
                    continue
                body = await r.json(content_type=None)
        except Exception:  # noqa: BLE001
            continue
        rows = [x for x in (body.get("body") or []) if isinstance(x, dict)]
        if not rows:
            continue
        if duration and rows:
            last = max(float(x.get("to") or 0) for x in rows)
            if last > float(duration) + 30:
                continue  # 字幕比视频长一大截 → 串台了
        lines = [str(x.get("content") or "").strip() for x in rows]
        text = re.sub(r"\s+", " ", " ".join(x for x in lines if x)).strip()
        if text:
            return text[: max(200, int(max_chars))]
    return ""


def _bili_headers(cfg: dict, referer: str = "https://www.bilibili.com/") -> dict:
    """B站接口要 UA + Referer + 带 buvid3 的 Cookie，否则 search 直接回 -412。"""
    cookie = str(cfg.get("bili_cookie") or "").strip()
    if cookie and "=" not in cookie:
        cookie = f"SESSDATA={cookie}"
    if "buvid3" not in cookie:
        cookie = (cookie + "; " if cookie else "") + "buvid3=" + str(uuid.uuid4()) + "infoc"
    return {
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie,
    }
