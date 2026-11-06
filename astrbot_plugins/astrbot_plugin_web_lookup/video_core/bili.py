"""B站视频解析：WBI、字幕、评论、音轨直链。"""
from __future__ import annotations

import re
import time
import uuid

import aiohttp

from .models import UA, _clean, _dur_sec, _empty, _num, _resolve
from .transcribe import transcribe_urls

_BILI_VIEW = "https://api.bilibili.com/x/web-interface/view"


_BILI_DETAIL = "https://api.bilibili.com/x/web-interface/view/detail"


_BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"


_BILI_TAGS = "https://api.bilibili.com/x/tag/archive/tags"


_BILI_PLAYURL = "https://api.bilibili.com/x/player/playurl"


_WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def _bili_headers(cfg: dict, referer: str = "https://www.bilibili.com/") -> dict:
    cookie = _clean(cfg.get("bili_cookie"))
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


def _wbi_mixin(img_url, sub_url) -> str:
    raw = ""
    for u in (img_url, sub_url):
        name = str(u or "").rsplit("/", 1)[-1]
        raw += name.split(".")[0]
    if len(raw) < 64:
        return ""
    return "".join(raw[i] for i in _WBI_TAB)[:32]


async def _wbi_mixin_get(sess, headers: dict) -> str:
    try:
        async with sess.get(_BILI_NAV, headers=headers) as r:
            p = await r.json(content_type=None)
        wbi = ((p or {}).get("data") or {}).get("wbi_img") or {}
        return _wbi_mixin(wbi.get("img_url"), wbi.get("sub_url"))
    except Exception:  # noqa: BLE001
        return ""


def _wbi_signed(params: dict, mixin: str) -> dict:
    import hashlib
    from urllib.parse import urlencode

    q = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in params.items()}
    q["wts"] = int(time.time())
    query = urlencode(sorted(q.items()))
    q["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return q


async def _bili_subtitle(sess, aid, cid, bvid, cfg, duration: float) -> str:
    """B站 AI 字幕会串台：同一个 cid 两次请求，内容甚至时长都可能不一样。
    明显不可信就重取一次，还不行返回空，交给音频转写。"""
    for _ in range(2):
        text = await _bili_subtitle_once(sess, aid, cid, bvid, cfg, duration)
        if text:
            return text
    return ""


async def _bili_subtitle_once(sess, aid, cid, bvid, cfg, duration: float) -> str:
    if not cid:
        return ""
    headers = _bili_headers(cfg)
    params = {"bvid": bvid, "cid": cid} if bvid else {"aid": aid, "cid": cid}
    try:
        async with sess.get(
            "https://api.bilibili.com/x/player/v2", params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            p = await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return ""
    subs = (((p or {}).get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    if not isinstance(subs, list) or not subs:
        return ""

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
        if duration:
            last = max(float(x.get("to") or 0) for x in rows)
            if last > float(duration) + 5:
                continue
            # B站偶尔会给同一个 cid 挂错 AI 字幕（内容属于别的视频，
            # 时长还明显短于本条）：覆盖不到大半就直接判无效，改走音频转写。
            if last < float(duration) * 0.55:
                continue
        text = _clean(" ".join(str(x.get("content") or "") for x in rows))
        if text:
            return text[: max(200, int(cfg["bili_subtitle_chars"]))]
    return ""


async def _bili_comments(sess, aid, bvid, headers, n: int) -> list[str]:
    """评论：只取主楼（热门排序），前 n 条。

    WBI main 的 data.replies 里每一条主楼都挂着自己的楼中楼（replies），
    之前把它们也拼进来，结果前 10 条被第一条主楼和它的子评论占满。
    子评论不算「前十条评论」，一律不要。
    """
    out: list[str] = []

    def add(msg) -> None:
        text = _clean(msg)[:80]
        if text and text not in out:
            out.append(text)

    if aid:
        try:
            async with sess.get(
                "https://api.bilibili.com/x/v2/reply",
                params={"type": 1, "oid": aid, "pn": 1, "ps": max(10, n * 2), "sort": 2},
                headers=headers,
            ) as r:
                p = await r.json(content_type=None)
            for it in ((p or {}).get("data") or {}).get("replies") or []:
                if isinstance(it, dict):
                    add((it.get("content") or {}).get("message"))
        except Exception:  # noqa: BLE001
            pass
    if len(out) < n and aid:
        mixin = await _wbi_mixin_get(sess, headers)
        if mixin:
            params = _wbi_signed({
                "oid": aid, "type": 1, "mode": 3, "pagination_str": '{"offset":""}',
                "plat": 1, "seek_rpid": "", "web_location": 1315875,
            }, mixin)
            try:
                async with sess.get(
                    "https://api.bilibili.com/x/v2/reply/wbi/main", params=params, headers=headers
                ) as r:
                    p = await r.json(content_type=None)
                for it in ((p or {}).get("data") or {}).get("replies") or []:
                    if isinstance(it, dict):
                        add((it.get("content") or {}).get("message"))
            except Exception:  # noqa: BLE001
                pass
    return out[:n]


async def bili_audio_urls(sess, bvid, aid, cid, cfg: dict) -> list[str]:
    """B站音轨直链：优先 DASH 最小码率音频，其次 durl（整段 mp4）。"""
    if not cid:
        return []
    headers = _bili_headers(cfg, referer=f"https://www.bilibili.com/video/{bvid or 'av' + str(aid)}")
    params = {"cid": cid, "fnval": 16, "fnver": 0, "fourk": 1}
    if bvid:
        params["bvid"] = bvid
    else:
        params["avid"] = aid
    try:
        async with sess.get(_BILI_PLAYURL, params=params, headers=headers) as r:
            p = await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return []
    d = (p or {}).get("data") or {}
    urls: list[str] = []
    audio = ((d.get("dash") or {}).get("audio") or [])
    if audio:
        try:
            audio = sorted(audio, key=lambda x: _num(x.get("bandwidth")))
        except Exception:  # noqa: BLE001
            pass
        for a in audio[:2]:
            for key in ("baseUrl", "base_url"):
                u = str(a.get(key) or "")
                if u:
                    urls.append(u)
            for u in a.get("backupUrl") or a.get("backup_url") or []:
                if u:
                    urls.append(str(u))
    if not urls:
        for x in (d.get("durl") or []):
            u = str((x or {}).get("url") or "")
            if u:
                urls.append(u)
    return urls[:3]


async def _parse_bili(url: str, cfg: dict, on_meta=None) -> dict:
    headers = _bili_headers(cfg)
    data = _empty("bilibili", url, "")
    if not str(cfg.get("bili_cookie") or "").strip():
        data["notes"].append(
            "B站未登录：AI/CC 字幕和外层评论可能拿不到；需要就扫码登录"
        )
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20), headers=headers, trust_env=False
        ) as sess:
            real = url
            if re.search(r"(b23\.tv|bili2233\.cn)", url, re.I):
                real = await _resolve(sess, url)
            m = re.search(r"(BV[0-9A-Za-z]{10})", real) or re.search(r"/video/(av\d+)", real, re.I)
            if not m:
                data["notes"].append("链接里没找到 BV/av 号")
                return data
            g = m.group(1)
            params = {"bvid": g} if g[:2].lower() == "bv" else {"aid": g[2:]}
            async with sess.get(_BILI_VIEW, params=params) as r:
                payload = await r.json(content_type=None)
            if not isinstance(payload, dict) or payload.get("code") != 0:
                code = payload.get("code") if isinstance(payload, dict) else str(payload)[:40]
                data["notes"].append(f"B站 view 接口返回 {code}")
                return data
            d = payload.get("data") or {}
            bvid = str(d.get("bvid") or (g if g[:2].lower() == "bv" else ""))
            aid = d.get("aid")
            cid = d.get("cid")
            pages = d.get("pages") or []
            if not cid and isinstance(pages, list) and pages and isinstance(pages[0], dict):
                cid = pages[0].get("cid")
            dur = _dur_sec(d.get("duration"))
            stat = d.get("stat") or {}
            data.update({
                "title": _clean(d.get("title")),
                "author": _clean((d.get("owner") or {}).get("name")),
                "duration": dur,
                "stats": {
                    "view": _num(stat.get("view")),
                    "like": _num(stat.get("like")),
                    "danmaku": _num(stat.get("danmaku")),
                },
                "desc": _clean(d.get("desc")),
            })
            if d.get("tname"):
                data["extra"].append(f"分区：{d['tname']}")
            if _num(d.get("pubdate")):
                data["extra"].append("发布：" + time.strftime("%Y-%m-%d", time.localtime(_num(d.get("pubdate")))))
            if isinstance(pages, list) and len(pages) > 1:
                data["extra"].append("分P：" + "、".join(
                    str(p.get("part") or "")[:16] for p in pages[:5] if isinstance(p, dict)
                ))

            detail: dict = {}
            if bvid:
                try:
                    async with sess.get(_BILI_DETAIL, params={"bvid": bvid}) as r:
                        dp = await r.json(content_type=None)
                    if isinstance(dp, dict) and dp.get("code") == 0:
                        detail = dp.get("data") or {}
                except Exception:  # noqa: BLE001
                    detail = {}
            tags = [
                _clean(t.get("tag_name")) for t in (detail.get("Tags") or [])
                if isinstance(t, dict) and _clean(t.get("tag_name"))
            ][:8]
            if tags:
                data["tags"] = tags
            elif bvid:
                try:
                    async with sess.get(_BILI_TAGS, params={"bvid": bvid}, headers=headers) as r:
                        tp = await r.json(content_type=None)
                    if isinstance(tp, dict) and tp.get("code") == 0:
                        data["tags"] = [
                            _clean(t.get("tag_name")) for t in (tp.get("data") or [])
                            if isinstance(t, dict) and _clean(t.get("tag_name"))
                        ][:8]
                except Exception:  # noqa: BLE001
                    pass

            sub = await _bili_subtitle(sess, aid, cid, bvid, cfg, float(dur or 0))
            if sub:
                data["subtitle"] = sub
            data["media_urls"] = await bili_audio_urls(sess, bvid, aid, cid, cfg)

            if on_meta:
                try:
                    await on_meta(data)
                except Exception:  # noqa: BLE001
                    pass

            data["comments"] = await _bili_comments(
                sess, aid, bvid, headers, int(cfg["video_comment_n"])
            )
    except Exception as e:  # noqa: BLE001
        data["notes"].append(f"B站解析失败（{type(e).__name__}）")

    if not data["subtitle"] and data["media_urls"]:
        text, truncated, why = await transcribe_urls(
            data["media_urls"],
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
            cfg=cfg,
            duration=float(data.get("duration") or 0),
        )
        if text:
            data["transcript"], data["transcript_truncated"] = text, truncated
        else:
            data["notes"].append("音频转写没成功" + (f"（{why}）" if why else ""))
    return data
