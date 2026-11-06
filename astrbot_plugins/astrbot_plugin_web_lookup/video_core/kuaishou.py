"""快手视频解析：H5 REST 接口、播放地址、评论。"""
from __future__ import annotations

import re
import time
import uuid

import aiohttp

from .models import UA, UA_M, _clean, _empty, _get_text, _num, _resolve, _title_from_html
from .transcribe import transcribe_urls

_KS_INFO_API = "https://m.gifshow.com/rest/wd/ugH5App/photo/simple/info"


_KS_COMMENT_API = "https://m.gifshow.com/rest/wd/photo/comment/list"


async def _ks_photo_id(sess, url: str) -> str:
    """短链先跟跳，再从 /fw/photo/<id>、/short-video/<id>、photoId= 里取作品 ID。"""
    real = url
    if re.search(r"v\.kuaishou\.com", url, re.I):
        real = await _resolve(sess, url)
    for pat in (
        r"/short-video/([0-9a-zA-Z]{5,32})",
        r"/fw/photo/([0-9a-zA-Z]{5,32})",
        r"[?&]photoId=([0-9a-zA-Z]{5,32})",
        r"/photo/([0-9a-zA-Z]{5,32})",
    ):
        m = re.search(pat, real)
        if m:
            return m.group(1)
    return ""


async def _ks_post(sess, api: str, payload: dict) -> dict:
    """m.gifshow.com 的 H5 REST 接口（www 域走 GraphQL + 滑块，这里绕开）。

    info 接口裸请求就行，评论接口必须带 did cookie + X-Requested-With，否则 result=2。
    """
    pid = str(payload.get("photoId") or "")
    try:
        async with sess.post(
            api,
            json=payload,
            headers={
                "Referer": f"https://m.gifshow.com/fw/photo/{pid}",
                "Origin": "https://m.gifshow.com",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Cookie": "did=web_" + uuid.uuid4().hex,
            },
        ) as r:
            if r.status != 200:
                return {}
            p = await r.json(content_type=None)
            return p if isinstance(p, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def _parse_kuaishou(url: str, cfg: dict, on_meta=None) -> dict:
    data = _empty("kuaishou", url, "")
    ks_ck = str((cfg or {}).get("kuaishou_cookie") or "").strip()
    ks_hdrs = {"User-Agent": UA_M}
    if ks_ck:
        ks_hdrs["Cookie"] = ks_ck
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=25), headers=ks_hdrs, trust_env=False
    ) as sess:
        pid = await _ks_photo_id(sess, url)
        if not pid:
            data["notes"].append("链接里没找到快手作品 ID（短链可能已失效）")
            return data
        body = await _ks_post(sess, _KS_INFO_API, {"photoId": pid})
        photo = body.get("photo") if _num(body.get("result")) == 1 else {}
        photo = photo if isinstance(photo, dict) else {}
        if photo:
            caption = _clean(photo.get("caption") or photo.get("title"))
            dur = _num(photo.get("duration"))
            if dur > 6000:  # 接口给的是毫秒
                dur //= 1000
            urls: list[str] = []
            for mv in (photo.get("mainMvUrls") or []):
                u = str((mv or {}).get("url") or "") if isinstance(mv, dict) else str(mv or "")
                if u.startswith("http") and u not in urls:
                    urls.append(u)
            manifest = photo.get("manifest") or {}
            for a in (manifest.get("adaptationSet") or manifest.get("adaptation_set") or []):
                for rep in (a.get("representation") or []):
                    if not isinstance(rep, dict):
                        continue
                    u = str(rep.get("url") or "")
                    if u.startswith("http") and u not in urls:
                        urls.append(u)
            data.update({
                "title": caption,
                "author": _clean(photo.get("userName") or photo.get("userNameText")),
                "duration": dur,
                "stats": {
                    "view": _num(photo.get("viewCount")),
                    "like": _num(photo.get("likeCount")),
                    "comment": _num(photo.get("commentCount")),
                },
                "desc": caption,
                "tags": re.findall(r"#([^#\s]{1,16})", caption)[:8],
                "media_urls": urls[:3],
            })
            if _num(photo.get("timestamp")):
                data["extra"].append("发布：" + time.strftime(
                    "%Y-%m-%d", time.localtime(_num(photo.get("timestamp")) / 1000)
                ))
        else:
            html = await _get_text(
                sess, f"https://www.kuaishou.com/short-video/{pid}", UA,
                "https://www.kuaishou.com/",
            )
            title = _title_from_html(html)
            if title:
                data["title"] = title
            data["notes"].append("快手详情接口没返回数据（作品可能已删除/受限）")

        if on_meta:
            try:
                await on_meta(data)
            except Exception:  # noqa: BLE001
                pass

        n = int(cfg["video_comment_n"])
        body2 = await _ks_post(sess, _KS_COMMENT_API, {"photoId": pid, "pageSize": n, "pcursor": ""})
        rows = body2.get("rootComments") or []
        cmts = [_clean(c.get("content")) for c in rows if isinstance(c, dict) and _clean(c.get("content"))]
        if cmts:
            data["comments"] = cmts[:n]
        elif not ks_ck:
            data["notes"].append(
                "快手评论没拿到：没登录态（不是风控）。跟我说一声我弹二维码，"
                "或把 Cookie 填进 kuaishou_cookie"
            )
        else:
            data["notes"].append("快手评论没拿到（登录态可能已过期，重新扫码）")

    if not data["subtitle"] and data["media_urls"]:
        text, truncated, why = await transcribe_urls(
            data["media_urls"],
            headers={"User-Agent": UA_M, "Referer": "https://m.gifshow.com/"},
            cfg=cfg,
            duration=float(data.get("duration") or 0),
        )
        if text:
            data["transcript"], data["transcript_truncated"] = text, truncated
        else:
            data["notes"].append("音频转写没成功" + (f"（{why}）" if why else ""))
    return data
