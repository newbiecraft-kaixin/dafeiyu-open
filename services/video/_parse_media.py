"""video.parse_api 的抖音 / 快手 / 西瓜解析：分享页 SSR、SEO 兜底与 H5 REST 接口。

从 parse_api.py 按职责拆出（纯移动，逻辑与签名不变）。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Tuple

from _parse_common import (
    UA,
    UA_M,
    _DOUYIN_SPIDER_UA,
    _clean,
    _date,
    _duration_from_html,
    _empty_result,
    _find_dict,
    _get_text,
    _http,
    _json_get,
    _num,
    _og,
    _remain,
    _resolve,
    _title_from_html,
    _window_json,
)
from _parse_transcribe import _transcribe_audio


# ---------------- 抖音 ----------------

def _douyin_share_res(html: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """分享页 SSR 里的作品详情：返回 (item, filter)；拿不到就是 ({}, {})。"""
    for name in ("_ROUTER_DATA", "_SSR_DATA"):
        obj = _window_json(html, name)
        if not obj:
            continue
        result = _find_dict(
            obj,
            lambda node: isinstance(node.get("item_list"), list)
            and isinstance(node.get("filter_list"), list),
        )
        if not result:
            continue
        items = [item for item in (result.get("item_list") or []) if isinstance(item, dict)]
        filters = [item for item in (result.get("filter_list") or []) if isinstance(item, dict)]
        return (items[0] if items else {}), (filters[0] if filters else {})
    return {}, {}


def _douyin_media_urls(item: Dict[str, Any]) -> List[str]:
    video = item.get("video") or {}
    if not isinstance(video, dict):
        return []
    urls: List[str] = []
    for key in ("play_addr", "play_addr_h264", "download_addr", "play_addr_265"):
        addr = video.get(key) or {}
        if not isinstance(addr, dict):
            continue
        for url in addr.get("url_list") or addr.get("urlList") or []:
            url = str(url).replace("playwm", "play")
            if url.startswith("http") and url not in urls:
                urls.append(url)
    return urls[:3]


def _douyin_play_urls(html: str) -> List[str]:
    """分享页里藏着 playwm 播放地址（斜杠常被转义），换成 play 即无水印原片。"""
    text = (
        html.replace("\\u002F", "/").replace("\\/", "/")
        .replace("\\u0026", "&").replace("\\u003D", "=")
    )
    urls: List[str] = []
    for url in re.findall(r'https?://[^"\\\s]+', text):
        if "playwm" not in url and "/aweme/v1/play/" not in url:
            continue
        url = url.replace("playwm", "play").rstrip("',")
        if url.startswith("http") and url not in urls:
            urls.append(url)
    return urls


def _douyin_seo(html: str) -> Dict[str, Any]:
    """抖音给爬虫 UA 会回带完整标题/简介/作者的 SEO 页。"""
    og = _og(html)
    title = _clean(og.get("lark:url:video_title") or og.get("og:title") or "")
    title = re.sub(r"\s*[|｜-]\s*抖音.*$", "", title).strip()
    desc = _clean(og.get("description") or og.get("og:description") or "")
    if "来抖音，记录美好生活" in desc and "发布在抖音" not in desc:
        desc = ""
    out: Dict[str, Any] = {}
    match = re.search(r" - ([^\s]{1,40})于(\d{8})发布在抖音", desc)
    if match:
        out["author"] = match.group(1)
        try:
            out["publish_time"] = time.strftime("%Y-%m-%d", time.strptime(match.group(2), "%Y%m%d"))
        except ValueError:
            pass
        desc = desc[: match.start()].strip(" -")
    if title:
        out["title"] = title
    if desc:
        out["desc"] = desc[:1200]
    duration = _duration_from_html(html)
    if duration:
        out["duration"] = duration
    return out


def _douyin_comments(vid: str, n: int, cfg: Dict[str, Any], deadline: float) -> Tuple[List[str], str]:
    headers = {
        "User-Agent": UA_M,
        "Referer": f"https://www.iesdouyin.com/share/video/{vid}/",
        "Accept": "application/json, text/plain, */*",
    }
    cookie = str(cfg["cookie"].get("douyin") or "").strip()
    if cookie:
        headers["Cookie"] = cookie
    query = urllib.parse.urlencode({"aweme_id": vid, "count": n, "cursor": 0})
    payload = _json_get(
        "https://www.iesdouyin.com/web/api/v2/comment/list/?" + query, headers, deadline, 10
    )
    comments: List[str] = []
    for row in (payload or {}).get("comments") or []:
        if not isinstance(row, dict):
            continue
        text = _clean(row.get("text"))
        if not text:
            kids = row.get("reply_comment")
            kids = [kids] if isinstance(kids, dict) else (kids or [])
            for kid in kids:
                if isinstance(kid, dict) and _clean(kid.get("text")):
                    text = _clean(kid.get("text"))
                    break
        text = text[:80]
        if text and text not in comments:
            comments.append(text)
    if not comments:
        if cookie:
            return [], "评论没拿到（登录态可能已过期，重新复制 Cookie）"
        return [], "评论没拿到：没登录态（不是风控）；填了 douyin Cookie 再试"
    return comments[:n], ""


def _parse_douyin(url: str, cfg: Dict[str, Any], want_comments: bool,
                  want_transcript: bool, deadline: float) -> Dict[str, Any]:
    data = _empty_result("douyin", url)
    is_note = bool(re.search(r"/(?:share/)?note/", url, re.I))
    headers_m = {"User-Agent": UA_M, "Accept-Language": "zh-CN,zh;q=0.9"}
    real = _resolve(url, headers_m, deadline) if re.search(r"v\.douyin\.com", url, re.I) else url
    match = (
        re.search(r"/(?:share/)?(?:video|note)/(\d+)", real)
        or re.search(r"[?&](?:modal_id|aweme_id|item_id)=(\d+)", real)
        or re.search(r"(\d{15,20})", real)
    )
    if not match:
        data["notes"].append("链接里没找到抖音作品 ID（短链可能已失效）")
        return data
    vid = match.group(1)
    if is_note:
        data["notes"].append("这是图文作品，没有音轨可以转写")

    page = f"https://www.iesdouyin.com/share/{'note' if is_note else 'video'}/{vid}/?from_ssr=1"
    html = _get_text(page, headers_m, deadline, 15)
    item, _filter = _douyin_share_res(html)
    media: List[str] = []
    if item:
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        desc = _clean(item.get("desc"))
        data.update({
            "title": _clean(re.split(r"[\r\n]", desc, maxsplit=1)[0])[:60] if desc else "",
            "author": _clean(author.get("nickname")),
            "desc": desc[:1200],
            "publish_time": _date(item.get("create_time")),
        })
        video = item.get("video") if isinstance(item.get("video"), dict) else {}
        duration = _num(video.get("duration") or item.get("duration"))
        if duration > 6000:
            duration //= 1000
        data["duration"] = duration
        media = _douyin_media_urls(item)

    if not data["title"] or not data["author"] or not data["duration"]:
        seo_html = _get_text(
            f"https://www.iesdouyin.com/share/video/{vid}/",
            {"User-Agent": _DOUYIN_SPIDER_UA, "Referer": "https://www.iesdouyin.com/"},
            deadline,
            15,
        )
        for key, value in _douyin_seo(seo_html).items():
            if value and not data.get(key):
                data[key] = value

    if not media and not is_note:
        for _ in range(2):
            share = _get_text(
                f"https://www.iesdouyin.com/share/video/{vid}/", headers_m, deadline, 12
            )
            if not data["duration"]:
                data["duration"] = _duration_from_html(share)
            media = _douyin_play_urls(share)
            if media:
                break
        if not media:
            data["notes"].append("没拿到可下载的音频，视频里说的话看不到")

    if not data["title"] and not data["desc"]:
        data["notes"].append("抖音网页把详情改成了前端动态加载（空壳/风控），没拿到视频信息")

    if want_comments:
        comments, why = _douyin_comments(vid, int(cfg["comment_n"]), cfg, deadline)
        data["comments"] = comments
        if why:
            data["notes"].append(why)

    if want_transcript and not is_note and media:
        text, truncated, why = _transcribe_audio(
            media,
            {"User-Agent": UA_M, "Referer": "https://www.douyin.com/"},
            cfg,
            float(data["duration"]),
            deadline,
        )
        if text:
            data["subtitle"] = text
            data["subtitle_source"] = "音频转写"
            if truncated:
                data["notes"].append(
                    f"只转写了前 {cfg['audio_max_s']} 秒音频（可在配置里调大 audio_max_s）"
                )
        else:
            data["notes"].append("音频转写没成功" + (f"（{why}）" if why else ""))
    return data


# ---------------- 快手 ----------------

_KS_INFO_API = "https://m.gifshow.com/rest/wd/ugH5App/photo/simple/info"
_KS_COMMENT_API = "https://m.gifshow.com/rest/wd/photo/comment/list"


def _ks_post(api: str, payload: Dict[str, Any], pid: str,
             headers_extra: Dict[str, str], deadline: float) -> Dict[str, Any]:
    """m.gifshow.com 的 H5 REST 接口（www 域走 GraphQL + 滑块，这里绕开）。"""
    did = "did=web_" + uuid.uuid4().hex
    cookie = str(headers_extra.get("Cookie") or "").strip().rstrip(";")
    headers = {
        "User-Agent": UA_M,
        "Referer": f"https://m.gifshow.com/fw/photo/{pid}",
        "Origin": "https://m.gifshow.com",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
        "Cookie": (cookie + "; " if cookie else "") + did,
    }
    try:
        body = json.dumps(payload).encode("utf-8")
        with _http(api, headers, data=body, timeout=_remain(deadline, 15)) as response:
            data = json.loads(response.read().decode("utf-8", "ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _parse_kuaishou(url: str, cfg: Dict[str, Any], want_comments: bool,
                    want_transcript: bool, deadline: float) -> Dict[str, Any]:
    data = _empty_result("kuaishou", url)
    headers_m = {"User-Agent": UA_M}
    real = _resolve(url, headers_m, deadline) if re.search(r"v\.kuaishou\.com", url, re.I) else url
    pid = ""
    for pattern in (
        r"/short-video/([0-9a-zA-Z]{5,32})",
        r"/fw/photo/([0-9a-zA-Z]{5,32})",
        r"[?&]photoId=([0-9a-zA-Z]{5,32})",
        r"/photo/([0-9a-zA-Z]{5,32})",
    ):
        match = re.search(pattern, real)
        if match:
            pid = match.group(1)
            break
    if not pid:
        data["notes"].append("链接里没找到快手作品 ID（短链可能已失效）")
        return data

    cookie = str(cfg["cookie"].get("kuaishou") or "").strip()
    extra = {"Cookie": cookie} if cookie else {}
    body = _ks_post(_KS_INFO_API, {"photoId": pid}, pid, extra, deadline)
    photo = body.get("photo") if _num(body.get("result")) == 1 else {}
    photo = photo if isinstance(photo, dict) else {}
    media: List[str] = []
    if photo:
        caption = _clean(photo.get("caption") or photo.get("title"))
        duration = _num(photo.get("duration"))
        if duration > 6000:
            duration //= 1000
        urls: List[str] = []
        for item in photo.get("mainMvUrls") or []:
            url_text = str((item or {}).get("url") or "") if isinstance(item, dict) else ""
            if url_text.startswith("http") and url_text not in urls:
                urls.append(url_text)
        manifest = photo.get("manifest") if isinstance(photo.get("manifest"), dict) else {}
        for adaptation in manifest.get("adaptationSet") or manifest.get("adaptation_set") or []:
            if not isinstance(adaptation, dict):
                continue
            for representation in adaptation.get("representation") or []:
                if not isinstance(representation, dict):
                    continue
                url_text = str(representation.get("url") or "")
                if url_text.startswith("http") and url_text not in urls:
                    urls.append(url_text)
        media = urls[:3]
        data.update({
            "title": caption,
            "author": _clean(photo.get("userName") or photo.get("userNameText")),
            "duration": duration,
            "publish_time": _date(photo.get("timestamp")),
            "desc": caption[:1200],
        })
    else:
        html = _get_text(f"https://www.kuaishou.com/short-video/{pid}", {"User-Agent": UA}, deadline, 15)
        title = _title_from_html(html)
        if title:
            data["title"] = title
        data["notes"].append("快手详情接口没返回数据（作品可能已删除/受限）")

    if want_comments:
        comment_body = _ks_post(
            _KS_COMMENT_API,
            {"photoId": pid, "pageSize": int(cfg["comment_n"]), "pcursor": ""},
            pid,
            extra,
            deadline,
        )
        rows = comment_body.get("rootComments") or []
        comments = [
            _clean(row.get("content"))
            for row in rows
            if isinstance(row, dict) and _clean(row.get("content"))
        ]
        data["comments"] = comments[: int(cfg["comment_n"])]
        if not data["comments"]:
            if cookie:
                data["notes"].append("快手评论没拿到（登录态可能已过期，重新复制 Cookie）")
            else:
                data["notes"].append("快手评论没拿到：没登录态（不是风控）；填了 kuaishou Cookie 再试")

    if want_transcript and media:
        text, truncated, why = _transcribe_audio(
            media,
            {"User-Agent": UA_M, "Referer": "https://m.gifshow.com/"},
            cfg,
            float(data["duration"]),
            deadline,
        )
        if text:
            data["subtitle"] = text
            data["subtitle_source"] = "音频转写"
            if truncated:
                data["notes"].append(
                    f"只转写了前 {cfg['audio_max_s']} 秒音频（可在配置里调大 audio_max_s）"
                )
        else:
            data["notes"].append("音频转写没成功" + (f"（{why}）" if why else ""))
    return data


# ---------------- 西瓜 ----------------

def _parse_ixigua(url: str, cfg: Dict[str, Any], want_comments: bool,
                  want_transcript: bool, deadline: float) -> Dict[str, Any]:
    data = _empty_result("ixigua", url)
    headers_m = {"User-Agent": UA_M, "Accept-Language": "zh-CN,zh;q=0.9"}
    real = _resolve(url, headers_m, deadline) if re.search(r"v\.ixigua\.com", url, re.I) else url
    match = re.search(r"/(?:video/)?(\d{15,20})", real) or re.search(r"(\d{15,20})", url)
    if not match:
        data["notes"].append("链接里没找到西瓜视频 ID（短链可能已跳到首页）")
        return data
    vid = match.group(1)
    cookie = str(cfg["cookie"].get("ixigua") or "").strip()
    headers = dict(headers_m)
    if cookie:
        headers["Cookie"] = cookie
    html = _get_text(f"https://m.ixigua.com/video/{vid}", headers, deadline, 15)

    detail: Dict[str, Any] = {}
    ssr = _window_json(html, "_SSR_DATA")
    if ssr:
        detail = (((ssr.get("data") or {}).get("storeState") or {}).get("detail") or {})
    video_data = ((detail.get("videoData") or {}).get("result") or {})
    if isinstance(video_data, dict) and video_data:
        user = video_data.get("media_user") or {}
        data.update({
            "title": _clean(video_data.get("title")),
            "author": _clean(user.get("screen_name") or user.get("name")),
            "duration": _num(video_data.get("duration")),
            "publish_time": _date(video_data.get("publish_time")),
            "desc": _clean(video_data.get("abstract"))[:1200],
        })
    if not data["title"]:
        og = _og(html)
        title = _clean(og.get("og:title"))
        title = re.sub(r"\s*[|｜]\s*西瓜视频\s*$", "", title).strip()
        if title:
            data["title"] = title
        if og.get("description") and not data["desc"]:
            desc = _clean(og["description"])
            if "西瓜视频为您提供" not in desc:
                data["desc"] = desc[:1200]
    if not data["title"]:
        data["notes"].append("西瓜移动端页面没给可读的视频信息")

    if want_comments:
        rows = (((detail.get("comment") or {}).get("result") or {}).get("list") or [])
        comments: List[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = _clean(row.get("text") or row.get("content") or row.get("comment_text"))
            if text and text not in comments:
                comments.append(text[:80])
        data["comments"] = comments[: int(cfg["comment_n"])]
        if not comments:
            data["notes"].append("评论没拿到（西瓜页面没带初始评论，评论接口要签名）")

    if want_transcript:
        data["notes"].append("西瓜移动端没给可下载的音频直链，没法转写")
    return data
