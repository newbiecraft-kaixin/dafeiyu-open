"""抖音视频/图文解析：分享页 SSR、播放地址、评论、图文 OCR。"""
from __future__ import annotations

import asyncio
import re
import threading
import time

import aiohttp

from .models import (
    UA, UA_M, _clean, _duration_from_html, _empty, _find_dict, _fmt_num,
    _get_text, _ld_article, _ld_count, _num, _og, _resolve, _title_from_html,
    _window_json,
)
from .transcribe import transcribe_urls

_DOUYIN_SPIDER_UA = (
    "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)"
)


async def _douyin_seo(sess, vid: str) -> dict:
    """抖音给爬虫 UA 会回带完整标题/简介/作者的 SEO 页（动态详情接口有签名风控）。"""
    try:
        async with sess.get(
            f"https://www.iesdouyin.com/share/video/{vid}/",
            headers={"User-Agent": _DOUYIN_SPIDER_UA, "Referer": "https://www.iesdouyin.com/"},
        ) as r:
            if r.status != 200:
                return {}
            html = await r.text()
    except Exception:  # noqa: BLE001
        return {}
    og = _og(html)
    title = _clean(og.get("lark:url:video_title") or og.get("og:title") or "")
    title = re.sub(r"\s*[|｜-]\s*抖音.*$", "", title).strip()
    desc = _clean(og.get("description") or og.get("og:description") or "")
    if "来抖音，记录美好生活" in desc and "发布在抖音" not in desc:
        desc = ""
    author = pub = ""
    like_text = ""
    m = re.search(r" - ([^\s]{1,40})于(\d{8})发布在抖音", desc)
    if m:
        author, pub = m.group(1), m.group(2)
        desc = desc[: m.start()].strip(" -")
    like = re.search(r"已经收获了([\d.]+(?:万|亿)?)个喜欢", og.get("description") or "")
    if like:
        like_text = like.group(1)
    tags = [
        t for t in re.split(r"[,，]", _clean(og.get("keywords") or ""))
        if t and t not in ("抖音", "抖音短视频", "抖音官网")
    ][:8]
    out: dict = {}
    if title:
        out["title"] = title
    if desc:
        out["desc"] = desc
    if author:
        out["author"] = author
    if tags:
        out["tags"] = tags
    if like_text:
        out.setdefault("extra", []).append("点赞：" + like_text)
    if pub:
        out.setdefault("extra", []).append(
            "发布：" + time.strftime("%Y-%m-%d", time.strptime(pub, "%Y%m%d"))
        )
    dur = _duration_from_html(html)
    if dur:
        out["duration"] = dur
    return out


def _douyin_play_urls(html: str) -> list[str]:
    """分享页里藏着 playwm 播放地址（JSON 里斜杠被转义成 \\u002F），换成 play 即无水印原片。"""
    text = (
        html.replace("\\u002F", "/").replace("\\/", "/")
        .replace("\\u0026", "&").replace("\\u003D", "=")
    )
    urls: list[str] = []
    for u in re.findall(r'https?://[^"\\\s]+', text):
        if "playwm" not in u and "/aweme/v1/play/" not in u:
            continue
        u = u.replace("playwm", "play").rstrip("',")
        if u.startswith("http") and u not in urls:
            urls.append(u)
    return urls


async def _douyin_comments(sess, vid: str, n: int, data: dict, cfg: dict | None = None) -> None:
    """抖音评论：视频和图文作品共用同一个接口（aweme_id 填作品 id）。"""
    try:
        api = (
            "https://www.iesdouyin.com/web/api/v2/comment/list/"
            f"?aweme_id={vid}&count={n}&cursor=0"
        )
        hdrs = {"User-Agent": UA_M,
                "Referer": f"https://www.iesdouyin.com/share/video/{vid}/"}
        ck = str((cfg or {}).get("douyin_cookie") or "").strip()
        if ck:
            hdrs["Cookie"] = ck
        async with sess.get(api, headers=hdrs) as r:
            p = await r.json(content_type=None)
        rows = (p or {}).get("comments") or []

        def _row_texts(c: dict) -> list[str]:
            """只取主评论；楼中楼（reply_comment）不算「前 N 条评论」。

            接口偶尔把正文塞在第二层：只有主评论为空时才拿子评论兜底。
            """
            base = _clean(c.get("text"))
            if base:
                return [base[:80]]
            kids = c.get("reply_comment")
            if isinstance(kids, dict):
                kids = [kids]
            for k in (kids or []):
                if isinstance(k, dict):
                    t = _clean(k.get("text"))
                    if t:
                        return [t[:80]]
            return []

        cmts: list[str] = []
        for c in rows:
            if isinstance(c, dict):
                for t in _row_texts(c):
                    if t not in cmts:
                        cmts.append(t)
        if cmts:
            data["comments"] = cmts[:n]
        elif not ck:
            data["notes"].append(
                "抖音评论没拿到：没登录态（不是风控）。跟我说一声我弹二维码，"
                "或把自己的 Cookie 填进 douyin_cookie"
            )
        else:
            data["notes"].append("抖音评论没拿到（登录态可能已过期，重新复制 Cookie）")
    except Exception as e:  # noqa: BLE001
        data["notes"].append(f"评论没拿到（{type(e).__name__}）")


_OCR_ENGINE = None


_OCR_LOCK = threading.Lock()


def _ocr_lines(content: bytes) -> list[str]:
    """同步 OCR 一张图（在 to_thread 里跑，别卡事件循环）。"""
    import io

    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(content)).convert("RGB")
    if img.width > 1280:  # 先缩到宽 1280，够辨认又不慢
        img = img.resize((1280, max(1, int(img.height * 1280 / img.width))))
    arr = np.asarray(img)[:, :, ::-1]  # RGB -> BGR，RapidOCR 按 OpenCV 习惯吃数组
    global _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is None:
            from rapidocr_onnxruntime import RapidOCR

            _OCR_ENGINE = RapidOCR()
        out = _OCR_ENGINE(arr)
    rows = out[0] if isinstance(out, tuple) else out
    lines: list[str] = []
    for it in rows or []:
        try:
            text, score = str(it[1]).strip(), float(it[2])
        except (IndexError, TypeError, ValueError):
            continue
        if text and score >= 0.5:
            lines.append(text)
    return lines


async def _douyin_ocr_images(
    sess, urls: list[str], n: int, max_chars: int, data: dict
) -> None:
    """抖音图文：下载前 n 张图做 OCR，图片里的文字并入资料。"""
    texts: list[str] = []
    tried = 0
    for idx, u in enumerate(urls[:n], 1):
        u = str(u or "").strip()
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith("http"):
            continue
        tried += 1
        try:
            async with sess.get(
                u, headers={"User-Agent": UA_M, "Referer": "https://www.douyin.com/"}
            ) as r:
                if r.status != 200:
                    data["notes"].append(f"第{idx}张图下载失败（HTTP {r.status}）")
                    continue
                content = await r.read()
            lines = await asyncio.wait_for(
                asyncio.to_thread(_ocr_lines, content), timeout=30
            )
            texts.extend(lines)
        except asyncio.TimeoutError:
            data["notes"].append(f"第{idx}张图 OCR 超时")
        except Exception as e:  # noqa: BLE001
            data["notes"].append(f"第{idx}张图 OCR 失败（{type(e).__name__}）")
    uniq: list[str] = []
    for t in texts:
        t = _clean(t)
        if t and t not in uniq:
            uniq.append(t)
    if uniq:
        data["ocr_text"] = " ".join(uniq)[:max_chars]
    elif tried:
        data["notes"].append("图片里的字没读出来（可能是纯画面或字太小）")


def _douyin_share_res(html: str) -> tuple[dict, dict]:
    """分享页 SSR 里的作品详情：返回 (item, filter)；拿不到就是 ({}, {})。"""
    for name in ("_ROUTER_DATA", "_SSR_DATA"):
        obj = _window_json(html, name)
        if not obj:
            continue
        res = _find_dict(
            obj,
            lambda d: isinstance(d.get("item_list"), list)
            and isinstance(d.get("filter_list"), list),
        )
        if not res:
            continue
        items = [x for x in (res.get("item_list") or []) if isinstance(x, dict)]
        flts = [x for x in (res.get("filter_list") or []) if isinstance(x, dict)]
        return (items[0] if items else {}), (flts[0] if flts else {})
    return {}, {}


def _douyin_note_images(item: dict) -> list[str]:
    """图文作品的图片地址：兼容 images 和 image_post_info.images 两种字段。"""
    raw = item.get("images")
    if not raw:
        post = item.get("image_post_info") or {}
        raw = (post.get("images") if isinstance(post, dict) else None) or []
    urls: list[str] = []
    for im in raw:
        if not isinstance(im, dict):
            continue
        disp = im.get("display_image") or {}
        cand = (
            im.get("url_list")
            or im.get("download_url_list")
            or (disp.get("url_list") if isinstance(disp, dict) else None)
            or []
        )
        for u in cand:
            u = str(u)
            if u.startswith("http"):
                urls.append(u)
                break
    return urls


async def _parse_douyin(url: str, cfg: dict, on_meta=None) -> dict:
    data = _empty("douyin", url, "")
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=25),
        headers={"User-Agent": UA_M, "Accept-Language": "zh-CN,zh;q=0.9"},
        trust_env=False,
    ) as sess:
        real = await _resolve(sess, url) if re.search(r"v\.douyin\.com", url, re.I) else url
        m = (
            re.search(r"/(?:share/)?(?:video|note)/(\d+)", real)
            or re.search(r"[?&](?:modal_id|aweme_id|item_id)=(\d+)", real)
            or re.search(r"(\d{15,20})", real)
        )
        if not m:
            data["notes"].append("链接里没找到抖音视频 ID")
            return data
        vid = m.group(1)
        # 图文作品走 /share/note/<id>：先读分享页 SSR 里的作品详情（正文/作者/图片都在这），
        # 爬虫 SEO 页现在经常直接 404，JSON-LD 只当兜底。
        if re.search(r"/(?:share/)?note/", real, re.I):
            data["is_note"] = True
            item: dict = {}
            flt: dict = {}
            art: dict = {}
            # from_ssr=1 才会服务端渲染出作品详情；不带这个参数多半是空壳
            share_note = f"https://www.iesdouyin.com/share/note/{vid}/?from_ssr=1"
            for page, ua in ((real, UA_M), (share_note, UA_M), (share_note, _DOUYIN_SPIDER_UA)):
                # 冷会话偶尔拿到不带数据的壳页，再拉一次多半就有了
                for _ in range(2):
                    html = await _get_text(sess, page, ua, "https://www.iesdouyin.com/")
                    got, bad = _douyin_share_res(html)
                    if got:
                        item, flt = got, bad
                        break
                    flt = flt or bad
                    art = _ld_article(html)
                    if art:
                        break
                if item or art:
                    break
            if item:
                author = item.get("author") or {}
                desc = _clean(item.get("desc"))
                title = _clean(re.split(r"[\r\n]", desc, maxsplit=1)[0])[:60] if desc else ""
                tags = [
                    _clean(t.get("hashtag_name"))
                    for t in (item.get("text_extra") or [])
                    if isinstance(t, dict) and _clean(t.get("hashtag_name"))
                ]
                if not tags:
                    tags = [_clean(t) for t in re.findall(r"#([^#\s]{1,20})", desc)]
                stats = item.get("statistics") or {}
                imgs = _douyin_note_images(item)
                data.update({
                    "title": title,
                    "author": _clean(author.get("nickname")),
                    "desc": desc,
                    "tags": tags[:8],
                    "stats": {
                        "like": _num(stats.get("digg_count")),
                        "comment": _num(stats.get("comment_count")),
                    },
                    "image_urls": imgs,
                })
                if imgs:
                    data["extra"].append(f"图片：{len(imgs)}张")
                if _num(stats.get("collect_count")):
                    data["extra"].append(f"收藏：{_fmt_num(stats.get('collect_count'))}")
                if _num(stats.get("share_count")):
                    data["extra"].append(f"转发：{_fmt_num(stats.get('share_count'))}")
                ssr_cmts = [
                    _clean(c.get("text"))
                    for c in (item.get("comment_list") or [])
                    if isinstance(c, dict) and _clean(c.get("text"))
                ]
                if ssr_cmts:
                    data["comments"] = ssr_cmts[: int(cfg["video_comment_n"])]
                data["notes"].append("这是图文作品，没有音轨可以转写")
            elif art:
                author = art.get("author") or {}
                title = _clean(art.get("headline"))
                body = _clean(art.get("articleBody") or art.get("description"))
                if "来抖音，记录美好生活" in body:
                    body = re.sub(r"\s*-\s*.{0,60}于\d{8}发布在抖音.*$", "", body).strip()
                imgs = [
                    str(u) for u in (art.get("image") or [])
                    if str(u).startswith("http")
                ]
                data.update({
                    "title": title,
                    "author": _clean(author.get("name")) if isinstance(author, dict) else "",
                    "desc": body,
                    "tags": [
                        _clean(t) for t in re.split(r"[,，]", _clean(art.get("keywords") or ""))
                        if _clean(t) and _clean(t) not in ("抖音", "抖音短视频", "抖音官网")
                    ][:8],
                    "stats": {
                        "like": _ld_count(art, "likeaction"),
                        "comment": _num(art.get("commentCount")),
                    },
                    "image_urls": imgs,
                })
                if _num(art.get("imageCount")):
                    data["extra"].append(f"图片：{_num(art.get('imageCount'))}张")
                if _num(art.get("collectCount")):
                    data["extra"].append(f"收藏：{_fmt_num(art.get('collectCount'))}")
                if _num(art.get("repostCount")):
                    data["extra"].append(f"转发：{_fmt_num(art.get('repostCount'))}")
                ld_cmts = [
                    _clean(c.get("text"))
                    for c in (art.get("comment") or [])
                    if isinstance(c, dict) and _clean(c.get("text"))
                ]
                if ld_cmts:
                    data["comments"] = ld_cmts[: int(cfg["video_comment_n"])]
                data["notes"].append("这是图文作品，没有音轨可以转写")
            else:
                why = _clean(flt.get("detail_msg") or flt.get("notice"))
                if why:
                    data["notes"].append("作品看不了：" + why)
                else:
                    data["notes"].append("分享页没返回图文详情（可能被风控或作品不存在）")
            if on_meta:
                try:
                    await on_meta(data)
                except Exception:  # noqa: BLE001
                    pass
            # 等提示发出去之后再逐张 OCR，别把「请等待」也拖慢
            if data.get("image_urls"):
                await _douyin_ocr_images(
                    sess, data["image_urls"],
                    int(cfg.get("video_ocr_max_images", 6)),
                    int(cfg.get("video_ocr_chars", 1500)),
                    data,
                )
            await _douyin_comments(sess, vid, int(cfg["video_comment_n"]), data, cfg)
            return data
        html = await _get_text(
            sess, f"https://www.douyin.com/share/video/{vid}/?region=CN&mid={vid}", UA_M,
            "https://www.douyin.com/",
        )
        aweme = None
        for name in ("_SSR_DATA", "_ROUTER_DATA", "__INITIAL_STATE__"):
            obj = _window_json(html, name)
            if obj:
                aweme = _find_dict(
                    obj,
                    lambda d: isinstance(d.get("video"), dict)
                    and ("desc" in d or "aweme_id" in d or "item_id" in d),
                )
                if aweme:
                    break
        if aweme:
            video = aweme.get("video") or {}
            raw_dur = video.get("duration") or aweme.get("duration") or 0
            dur = _num(raw_dur)
            if dur > 6000:  # 抖音 app 接口给毫秒
                dur //= 1000
            author = aweme.get("author") or {}
            stats = aweme.get("statistics") or aweme.get("stats") or {}
            urls: list[str] = []
            for key in ("play_addr", "play_addr_h264", "download_addr", "play_addr_265"):
                addr = video.get(key) or {}
                for u in (addr.get("url_list") or addr.get("urlList") or []):
                    u = str(u).replace("playwm", "play")
                    if u and u not in urls:
                        urls.append(u)
            tags = [
                _clean(t.get("hashtag_name"))
                for t in (aweme.get("text_extra") or [])
                if isinstance(t, dict) and _clean(t.get("hashtag_name"))
            ]
            data.update({
                "title": _clean(aweme.get("desc")) or _clean(aweme.get("title")),
                "author": _clean(author.get("nickname")),
                "duration": dur,
                "stats": {
                    "view": _num(stats.get("play_count")),
                    "like": _num(stats.get("digg_count")),
                    "comment": _num(stats.get("comment_count")),
                },
                "desc": _clean(aweme.get("desc")),
                "tags": tags[:8],
                "media_urls": urls[:3],
            })
        else:
            title = _title_from_html(html)
            # 空壳页的 <title> 是「在抖音记录美好生活2026xxxx」这类站点占位，不能当视频标题
            if title and "记录美好生活" not in title:
                data["title"] = title
            seo = await _douyin_seo(sess, vid)
            if seo:
                data.update(seo)
            else:
                data["notes"].append("抖音网页把详情改成了前端动态加载（空壳/风控），没拿到视频信息")
        if not data["media_urls"]:
            # 详情 JSON 被 Argus 签名封死，但手机 UA 的分享页里还留着 playwm 播放地址。
            # 冷会话第一次常拿到不带播放地址的壳页，重试几次即可。
            share_url = f"https://www.iesdouyin.com/share/video/{vid}/"
            for _ in range(3):
                share = await _get_text(sess, share_url, UA_M, "https://www.iesdouyin.com/")
                if not data.get("duration"):
                    dur = _duration_from_html(share)
                    if dur:
                        data["duration"] = dur
                urls = _douyin_play_urls(share)
                if urls:
                    data["media_urls"] = urls[:2]
                    break
        if not data["media_urls"]:
            data["notes"].append("没拿到可下载的音频，视频里说的话看不到")

        if on_meta:
            try:
                await on_meta(data)
            except Exception:  # noqa: BLE001
                pass

        n = int(cfg["video_comment_n"])
        await _douyin_comments(sess, vid, n, data, cfg)

    if not data["subtitle"] and data["media_urls"]:
        text, truncated, why = await transcribe_urls(
            data["media_urls"],
            headers={"User-Agent": UA_M, "Referer": "https://www.douyin.com/"},
            cfg=cfg,
            duration=float(data.get("duration") or 0),
        )
        if text:
            data["transcript"], data["transcript_truncated"] = text, truncated
        else:
            data["notes"].append("音频转写没成功" + (f"（{why}）" if why else ""))
    return data
