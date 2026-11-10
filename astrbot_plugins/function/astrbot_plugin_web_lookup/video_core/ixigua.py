"""西瓜视频解析：移动页 SSR 数据与初始评论。"""
from __future__ import annotations

import re
import time

import aiohttp

from .models import UA_M, _clean, _empty, _get_text, _num, _og, _resolve, _window_json

async def _parse_ixigua(url: str, cfg: dict, on_meta=None) -> dict:
    data = _empty("ixigua", url, "")
    xg_ck = str((cfg or {}).get("ixigua_cookie") or "").strip()
    xg_hdrs = {"User-Agent": UA_M, "Accept-Language": "zh-CN,zh;q=0.9"}
    if xg_ck:
        xg_hdrs["Cookie"] = xg_ck
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=25),
        headers=xg_hdrs,
        trust_env=False,
    ) as sess:
        real = await _resolve(sess, url) if re.search(r"v\.ixigua\.com", url, re.I) else url
        m = re.search(r"/(?:video/)?(\d{15,20})", real) or re.search(r"(\d{15,20})", url)
        if not m:
            data["notes"].append("链接里没找到西瓜视频 ID（短链可能已跳到首页）")
            return data
        vid = m.group(1)
        html = await _get_text(sess, f"https://m.ixigua.com/video/{vid}", UA_M, "https://m.ixigua.com/")
        detail: dict = {}
        ssr = _window_json(html, "_SSR_DATA")
        if ssr:
            detail = ((ssr.get("data") or {}).get("storeState") or {}).get("detail") or {}
        vd = ((detail.get("videoData") or {}).get("result") or {})
        if vd:
            user = vd.get("media_user") or {}
            cat = ""
            for c in (vd.get("video_categories") or []):
                if isinstance(c, dict):
                    cat = cat or _clean(c.get("level2") or c.get("level1"))
            data.update({
                "title": _clean(vd.get("title")),
                "author": _clean(user.get("screen_name") or user.get("name")),
                "duration": _num(vd.get("duration")),
                "stats": {
                    "view": _num(vd.get("play_count")),
                    "comment": _num(vd.get("comment_count")),
                },
                "desc": _clean(vd.get("abstract")),
                "tags": [_clean(t) for t in (vd.get("video_tags") or []) if _clean(t)][:8],
            })
            if cat:
                data["extra"].append(f"分类：{cat}")
            if _num(vd.get("publish_time")):
                data["extra"].append("发布：" + time.strftime(
                    "%Y-%m-%d", time.localtime(_num(vd.get("publish_time")))
                ))
        if not data["title"]:
            og = _og(html)
            title = _clean(og.get("og:title"))
            title = re.sub(r"\s*[|｜]\s*西瓜视频\s*$", "", title).strip()
            if title:
                data["title"] = title
            if og.get("description") and not data["desc"]:
                d = _clean(og["description"])
                if "西瓜视频为您提供" not in d:
                    data["desc"] = d
        if not data["title"]:
            data["notes"].append("西瓜移动端页面没给可读的视频信息")

        # 页面自带的初始评论（大多数视频这里是空的）
        rows = (((detail.get("comment") or {}).get("result") or {}).get("list") or [])
        cmts: list[str] = []
        for c in rows:
            if not isinstance(c, dict):
                continue
            txt = _clean(c.get("text") or c.get("content") or c.get("comment_text") or c.get("reply_text"))
            if txt:
                cmts.append(txt[:80])
        data["comments"] = cmts[: int(cfg["video_comment_n"])]
        if not data["comments"]:
            data["notes"].append("评论没拿到（西瓜页面没带初始评论，评论接口要签名）")
        # 移动端只给了加密 url 字段，没拿到可下载的音频直链
        if not data["media_urls"]:
            data["notes"].append("没拿到音频直链，没法转写")

        if on_meta:
            try:
                await on_meta(data)
            except Exception:  # noqa: BLE001
                pass
    return data
