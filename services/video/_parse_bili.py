"""video.parse_api 的 B站解析：view/player/playurl/reply 接口、wbi 签名与音轨直链。

从 parse_api.py 按职责拆出（纯移动，逻辑与签名不变）。
"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
import uuid
from typing import Any, Dict, List

from _parse_common import UA, _clean, _date, _dur_sec, _empty_result, _json_get, _num, _resolve
from _parse_transcribe import _transcribe_audio

_BILI_VIEW = "https://api.bilibili.com/x/web-interface/view"
_BILI_PLAYER = "https://api.bilibili.com/x/player/v2"
_BILI_PLAYURL = "https://api.bilibili.com/x/player/playurl"
_BILI_REPLY = "https://api.bilibili.com/x/v2/reply"
_BILI_REPLY_MAIN = "https://api.bilibili.com/x/v2/reply/wbi/main"
_BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"

_WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def _bili_headers(cfg: Dict[str, Any], referer: str = "https://www.bilibili.com/") -> Dict[str, str]:
    cookie = str(cfg["cookie"].get("bili") or "").strip()
    if cookie and "=" not in cookie:
        cookie = "SESSDATA=" + cookie
    if "buvid3" not in cookie:
        cookie = (cookie + "; " if cookie else "") + "buvid3=" + str(uuid.uuid4()) + "infoc"
    return {
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
    }


def _wbi_mixin(img_url: Any, sub_url: Any) -> str:
    raw = ""
    for item in (img_url, sub_url):
        name = str(item or "").rsplit("/", 1)[-1]
        raw += name.split(".")[0]
    if len(raw) < 64:
        return ""
    return "".join(raw[index] for index in _WBI_TAB)[:32]


def _wbi_mixin_get(headers: Dict[str, str], deadline: float) -> str:
    payload = _json_get(_BILI_NAV, headers, deadline, timeout=8)
    wbi = ((payload or {}).get("data") or {}).get("wbi_img") or {}
    return _wbi_mixin(wbi.get("img_url"), wbi.get("sub_url"))


def _wbi_signed(params: Dict[str, Any], mixin: str) -> Dict[str, Any]:
    query_params = {key: re.sub(r"[!'()*]", "", str(value)) for key, value in params.items()}
    query_params["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(query_params.items()))
    query_params["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return query_params


def _bili_subtitle(headers: Dict[str, str], aid: Any, bvid: str, cid: Any,
                   duration: float, deadline: float) -> str:
    """优先中文字幕；明显串台（时长对不上）的一律判无效。"""
    if not cid:
        return ""
    params = {"bvid": bvid, "cid": cid} if bvid else {"aid": aid, "cid": cid}
    payload = _json_get(_BILI_PLAYER + "?" + urllib.parse.urlencode(params), headers, deadline, 10)
    subs = (((payload or {}).get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    if not isinstance(subs, list):
        return ""
    usable = [s for s in subs if isinstance(s, dict) and s.get("subtitle_url")]
    if not usable:
        return ""

    def rank(item: Dict[str, Any]) -> int:
        lan = str(item.get("lan") or "")
        return 0 if lan.startswith("zh") else 1 if lan.startswith("en") else 2

    usable.sort(key=rank)
    for best in usable:
        url = str(best["subtitle_url"])
        if url.startswith("//"):
            url = "https:" + url
        body = _json_get(url, headers, deadline, 10)
        if not body:
            continue
        rows = [row for row in (body.get("body") or []) if isinstance(row, dict)]
        if not rows:
            continue
        if duration:
            try:
                last = max(float(row.get("to") or 0) for row in rows)
            except (TypeError, ValueError):
                last = 0.0
            if last > duration + 5:
                continue
            if last < duration * 0.55:
                continue
        text = _clean(" ".join(str(row.get("content") or "") for row in rows))
        if text:
            return text
    return ""


def _bili_comments(headers: Dict[str, str], aid: Any, n: int, deadline: float) -> List[str]:
    """只取主楼（热门排序）前 n 条，楼中楼不算。"""
    out: List[str] = []

    def add(message: Any) -> None:
        text = _clean(message)[:80]
        if text and text not in out:
            out.append(text)

    if aid:
        query = urllib.parse.urlencode({
            "type": 1, "oid": aid, "pn": 1, "ps": max(10, n * 2), "sort": 2,
        })
        payload = _json_get(_BILI_REPLY + "?" + query, headers, deadline, 10)
        for item in ((payload or {}).get("data") or {}).get("replies") or []:
            if isinstance(item, dict):
                add((item.get("content") or {}).get("message"))
    if len(out) < n and aid:
        mixin = _wbi_mixin_get(headers, deadline)
        if mixin:
            params = _wbi_signed({
                "oid": aid, "type": 1, "mode": 3, "pagination_str": '{"offset":""}',
                "plat": 1, "seek_rpid": "", "web_location": 1315875,
            }, mixin)
            payload = _json_get(
                _BILI_REPLY_MAIN + "?" + urllib.parse.urlencode(params), headers, deadline, 10
            )
            for item in ((payload or {}).get("data") or {}).get("replies") or []:
                if isinstance(item, dict):
                    add((item.get("content") or {}).get("message"))
    return out[:n]


def _bili_audio_urls(headers: Dict[str, str], bvid: str, aid: Any, cid: Any,
                     deadline: float) -> List[str]:
    """B站音轨直链：优先 DASH 最小码率音频，其次 durl（整段 mp4）。"""
    if not cid:
        return []
    params: Dict[str, Any] = {"cid": cid, "fnval": 16, "fnver": 0, "fourk": 1}
    if bvid:
        params["bvid"] = bvid
    else:
        params["avid"] = aid
    payload = _json_get(_BILI_PLAYURL + "?" + urllib.parse.urlencode(params), headers, deadline, 12)
    detail = (payload or {}).get("data") or {}
    urls: List[str] = []
    audio = detail.get("dash", {}).get("audio") if isinstance(detail.get("dash"), dict) else None
    if isinstance(audio, list) and audio:
        try:
            audio = sorted(audio, key=lambda item: _num(item.get("bandwidth")))
        except Exception:  # noqa: BLE001
            pass
        for item in audio[:2]:
            if not isinstance(item, dict):
                continue
            for key in ("baseUrl", "base_url"):
                url = str(item.get(key) or "")
                if url:
                    urls.append(url)
            for url in item.get("backupUrl") or item.get("backup_url") or []:
                if url:
                    urls.append(str(url))
    if not urls:
        for item in detail.get("durl") or []:
            url = str((item or {}).get("url") or "")
            if url:
                urls.append(url)
    return urls[:3]


def _parse_bili(url: str, cfg: Dict[str, Any], want_comments: bool,
                want_transcript: bool, deadline: float) -> Dict[str, Any]:
    data = _empty_result("bilibili", url)
    headers = _bili_headers(cfg)
    if not cfg["cookie"].get("bili"):
        data["notes"].append("B站未登录：AI 字幕可能拿不到；要拿可在下方登录配置里填 SESSDATA")

    real = url
    if re.search(r"(b23\.tv|bili2233\.cn)", url, re.I):
        real = _resolve(url, headers, deadline)
    match = re.search(r"(BV[0-9A-Za-z]{10})", real) or re.search(r"/video/(av\d+)", real, re.I)
    if not match:
        data["notes"].append("链接里没找到 BV / av 号")
        return data
    token = match.group(1)
    bvid = token if token[:2].lower() == "bv" else ""
    aid: Any = "" if bvid else token[2:]
    params = {"bvid": bvid} if bvid else {"aid": aid}

    payload = _json_get(_BILI_VIEW + "?" + urllib.parse.urlencode(params), headers, deadline, 15)
    if not isinstance(payload, dict) or payload.get("code") != 0:
        code = payload.get("code") if isinstance(payload, dict) else "网络异常"
        data["notes"].append(f"B站 view 接口返回 {code}，可能被风控或视频不存在")
        return data
    detail = payload.get("data") or {}
    bvid = str(detail.get("bvid") or bvid)
    aid = detail.get("aid") or aid
    cid = detail.get("cid")
    pages = detail.get("pages") or []
    if not cid and isinstance(pages, list) and pages and isinstance(pages[0], dict):
        cid = pages[0].get("cid")
    duration = _dur_sec(detail.get("duration"))
    owner = detail.get("owner") or {}
    data.update({
        "title": _clean(detail.get("title")),
        "author": _clean(owner.get("name")),
        "duration": duration,
        "publish_time": _date(detail.get("pubdate")),
        "desc": _clean(detail.get("desc"))[:1200],
    })

    subtitle = _bili_subtitle(headers, aid, bvid, cid, float(duration), deadline)
    if subtitle:
        data["subtitle"] = subtitle
        data["subtitle_source"] = "字幕"
    elif not cfg["cookie"].get("bili"):
        data["notes"].append("没拿到字幕（未登录时 B站 AI 字幕不可用）")
    else:
        data["notes"].append("没拿到字幕（这个视频可能没有 CC / AI 字幕）")

    if want_comments and aid:
        comments = _bili_comments(headers, aid, int(cfg["comment_n"]), deadline)
        data["comments"] = comments
        if not comments:
            data["notes"].append("评论没拿到（B站评论接口没返回，可能被风控）")

    if want_transcript and not data["subtitle"]:
        referer = f"https://www.bilibili.com/video/{bvid}" if bvid else f"https://www.bilibili.com/video/av{aid}"
        audio_headers = _bili_headers(cfg, referer=referer)
        urls = _bili_audio_urls(audio_headers, bvid, aid, cid, deadline)
        if not urls:
            data["notes"].append("没拿到音频直链，没法转写")
        else:
            text, truncated, why = _transcribe_audio(
                urls, audio_headers, cfg, float(duration), deadline
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
