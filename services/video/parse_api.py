"""video.parse_api：看视频（B站 / 抖音 / 快手 / 西瓜 链接解析）。

属于「视频能力域」：贴一个链接，取标题、作者、时长、发布时间、简介、
字幕（没有字幕时可选择音频转写）和前 N 条评论。由 service.py 装配到 23368 端口。

接口
  POST /video/parse         解析链接
  GET  /video/config        读配置（Cookie 只回显是否已设置，不回显原值）
  POST /video/config        写配置
  GET  /video/parse/status  最近一次解析概况（页面顶部用）

所有第三方接口都 fail-soft：断网 / 超时 / 风控 / 结构变化只往 notes 里写中文说明，
或返回 fail(...)，绝不抛异常导致服务 500。视频解析参考了 AstrBot 插件
astrbot_plugin_web_lookup 的 video_core，但不依赖其运行时。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import fail, ok  # noqa: E402

DATA_DIR = _HERE.parent / "data"
CONFIG_FILE = DATA_DIR / "parse.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
UA_M = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_DOUYIN_SPIDER_UA = (
    "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)"
)

COOKIE_KEYS = ("bili", "douyin", "kuaishou", "ixigua")
PLATFORM_NAMES = {
    "bilibili": "B站",
    "douyin": "抖音",
    "kuaishou": "快手",
    "ixigua": "西瓜",
}
SUBTITLE_PORT = 23405

# 解析服务直连，不吃系统代理（本机常见 Clash 会把 B站接口带偏）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_CFG_LOCK = threading.RLock()
_LAST_LOCK = threading.Lock()
_LAST: Dict[str, Any] = {}

_WHISPER = None
_WHISPER_LOCK = threading.Lock()


# ---------------- 通用小工具 ----------------

def _payload(req) -> Dict[str, Any]:  # noqa: ANN001
    return req.payload if isinstance(req.payload, dict) else {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("0", "false", "off", "no", "否", "")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _num(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _dur_sec(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, int(value))
    sec = 0
    for part in re.split(r"[:：]", str(value or "")):
        if part.strip().isdigit():
            sec = sec * 60 + int(part.strip())
    return sec


def _date(ts: Any) -> str:
    seconds = _num(ts)
    if seconds <= 0:
        return ""
    if seconds > 10_000_000_000:  # 毫秒时间戳
        seconds //= 1000
    try:
        return time.strftime("%Y-%m-%d", time.localtime(seconds))
    except (OSError, ValueError, OverflowError):
        return ""


def _remain(deadline: float, default: float = 15.0, minimum: float = 2.0) -> float:
    left = float(deadline) - time.time()
    if left <= 1.0:
        return minimum
    return max(minimum, min(float(default), left))


def _http(url: str, headers: Optional[Dict[str, str]] = None, data: Optional[bytes] = None,
          timeout: float = 15.0):  # noqa: ANN201
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return _OPENER.open(req, timeout=max(1.0, float(timeout)))


def _get_text(url: str, headers: Optional[Dict[str, str]], deadline: float,
              timeout: float = 15.0) -> str:
    try:
        with _http(url, headers, timeout=_remain(deadline, timeout)) as response:
            return response.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 - 网络问题交给调用方写 notes
        return ""


def _json_get(url: str, headers: Optional[Dict[str, str]], deadline: float,
              timeout: float = 15.0) -> Optional[Dict[str, Any]]:
    text = _get_text(url, headers, deadline, timeout)
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _resolve(url: str, headers: Optional[Dict[str, str]], deadline: float) -> str:
    """短链跟跳：返回最终地址，失败就原样返回。"""
    try:
        with _http(url, headers, timeout=_remain(deadline, 10)) as response:
            return str(response.geturl() or url)
    except Exception:  # noqa: BLE001
        return url


def _window_json(html: str, name: str) -> Any:
    """取 window.<name> = {...}; 解析失败返回 None。"""
    match = re.search(rf"window\.{re.escape(name)}\s*=\s*", html or "")
    if not match:
        return None
    try:
        return json.JSONDecoder().raw_decode(html[match.end():])[0]
    except Exception:  # noqa: BLE001
        return None


def _find_dict(obj: Any, pred, max_nodes: int = 30000) -> Optional[Dict[str, Any]]:
    """在 JSON 里广度优先找第一个满足条件的 dict（页面结构常变，用这个兜底）。"""
    stack = [obj]
    seen = 0
    while stack and seen < max_nodes:
        current = stack.pop()
        seen += 1
        if isinstance(current, dict):
            if pred(current):
                return current
            stack.extend(v for v in current.values() if isinstance(v, (dict, list)))
        elif isinstance(current, list):
            stack.extend(current)
    return None


def _og(html: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for match in re.finditer(r"<meta[^>]+>", html or "", re.I):
        tag = match.group(0)
        name = re.search(r'(?:name|property)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        content = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if name and content:
            out.setdefault(name.group(1).lower(), content.group(1))
    title = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    if title:
        out.setdefault("title", _clean(title.group(1)))
    return out


def _title_from_html(html: str) -> str:
    og = _og(html)
    title = _clean(og.get("og:title") or og.get("title") or "")
    for junk in ("在抖音记录美好生活", "短视频-快手", "抖音", "快手", "西瓜视频"):
        if title.startswith(junk) and len(title) <= len(junk) + 6:
            return ""
    return re.sub(r"\s*[|｜-]\s*(抖音|快手|西瓜视频).*$", "", title).strip()


def _duration_from_html(html: str) -> int:
    """从分享页 HTML 里抠时长（秒）；抖音接口偶尔给毫秒，超过 6000 就换算。"""
    text = str(html or "").replace("\\u002F", "/").replace("\\/", "/")
    for pattern in (
        r'<meta[^>]+property="og:video:duration"[^>]+content="(\d+)"',
        r'<meta[^>]+content="(\d+)"[^>]+property="og:video:duration"',
        r'"video_duration"\s*:\s*(\d+)',
        r'"duration"\s*:\s*(\d+)',
    ):
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        seconds = _num(match.group(1))
        if seconds > 6000:
            seconds //= 1000
        if seconds > 0:
            return seconds
    return 0


def _detect_platform(url: str) -> Optional[str]:
    """按域名判断平台；b23.tv / v.douyin / v.kuaishou / v.ixigua 等短链也算。"""
    if not re.match(r"^https?://", (url or "").strip(), re.I):
        return None
    host = urllib.parse.urlsplit(url.strip()).netloc.lower()
    if re.search(r"(b23\.tv|bili2233\.cn|bilibili\.com)$", host):
        return "bilibili"
    if re.search(r"(douyin\.com|iesdouyin\.com)$", host):
        return "douyin"
    if re.search(r"(kuaishou\.com|gifshow\.com|chenzhongtech\.com)$", host):
        return "kuaishou"
    if re.search(r"(ixigua\.com|toutiao\.com)$", host):
        return "ixigua"
    return None


def _empty_result(platform: str, url: str, note: str = "") -> Dict[str, Any]:
    return {
        "platform": platform,
        "title": "",
        "author": "",
        "duration": 0,
        "publish_time": "",
        "desc": "",
        "subtitle": "",
        "subtitle_source": "",
        "comments": [],
        "url": url,
        "notes": [note] if note else [],
    }


# ---------------- 配置读写 ----------------

def _normalize_cfg(raw: Any) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    cookie_raw = raw.get("cookie")
    cookie = {key: "" for key in COOKIE_KEYS}
    if isinstance(cookie_raw, dict):
        for key in COOKIE_KEYS:
            cookie[key] = str(cookie_raw.get(key) or "").strip()
    cfg: Dict[str, Any] = {"cookie": cookie}
    bounds = (
        ("comment_n", 1, 50, 10),
        ("transcript_chars", 500, 20000, 4000),
        ("audio_max_s", 30, 1800, 300),
        ("timeout_s", 10, 600, 180),
    )
    for key, low, high, default in bounds:
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        cfg[key] = max(low, min(high, value))
    return cfg


def _load_cfg() -> Dict[str, Any]:
    with _CFG_LOCK:
        raw: Any = {}
        try:
            if CONFIG_FILE.exists():
                raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raw = {}
        return _normalize_cfg(raw)


def _save_cfg(cfg: Dict[str, Any]) -> None:
    with _CFG_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)


def _public_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """对外只给「是否已设置」，绝不回显 Cookie 原值。"""
    return {
        "cookie": {key: bool(cfg["cookie"].get(key)) for key in COOKIE_KEYS},
        "comment_n": cfg["comment_n"],
        "transcript_chars": cfg["transcript_chars"],
        "audio_max_s": cfg["audio_max_s"],
        "timeout_s": cfg["timeout_s"],
    }


def status() -> Dict[str, Any]:
    """给 /health 用：模块就绪、平台列表、Cookie 是否已设置、最近一次解析概况。"""
    try:
        cfg = _load_cfg()
    except Exception:  # noqa: BLE001 - health 不允许把服务带崩
        cfg = _normalize_cfg({})
    with _LAST_LOCK:
        last = dict(_LAST) if _LAST else None
    return {
        "ready": True,
        "platforms": list(PLATFORM_NAMES.values()),
        "cookies": {key: bool(cfg["cookie"].get(key)) for key in COOKIE_KEYS},
        "transcript": "faster-whisper/small（本地 CPU）",
        "last": last,
    }


def _remember_last(data: Dict[str, Any], elapsed_ms: int) -> None:
    with _LAST_LOCK:
        _LAST.clear()
        _LAST.update({
            "at": time.strftime("%H:%M:%S"),
            "platform": data.get("platform") or "",
            "title": data.get("title") or "",
            "url": data.get("url") or "",
            "has_subtitle": bool(data.get("subtitle")),
            "comments": len(data.get("comments") or []),
            "notes": len(data.get("notes") or []),
            "elapsed_ms": elapsed_ms,
        })


# ---------------- B站 ----------------

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


# ---------------- 音频下载与转写 ----------------

def _ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


def _download_audio(urls: List[str], headers: Dict[str, str], deadline: float,
                    cap_mb: int = 60) -> str:
    cap = max(5, int(cap_mb)) * 1024 * 1024
    for url in urls[:3]:
        path = os.path.join(tempfile.gettempdir(), f"dfy_video_{uuid.uuid4().hex}.bin")
        size = 0
        try:
            request = urllib.request.Request(url, headers=headers)
            with _OPENER.open(request, timeout=_remain(deadline, 60)) as response:
                with open(path, "wb") as handle:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        handle.write(chunk)
                        size += len(chunk)
                        if size >= cap or time.time() > deadline:
                            break
            if size > 0:
                return path
        except Exception:  # noqa: BLE001 - 换下一个直链
            pass
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    return ""


def _ffmpeg_trim(path: str, max_s: int, deadline: float) -> str:
    """把任意音频/视频抽成 16k 单声道 wav，并截到前 max_s 秒。"""
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return ""
    wav = path + ".16k.wav"
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", path, "-t", str(int(max_s)), "-ar", "16000", "-ac", "1", wav],
            capture_output=True,
            timeout=max(20, min(180, int(deadline - time.time()))),
        )
        if os.path.exists(wav) and os.path.getsize(wav) > 44:
            return wav
    except Exception:  # noqa: BLE001
        pass
    return ""


def _whisper_model():
    global _WHISPER
    with _WHISPER_LOCK:
        if _WHISPER is None:
            from faster_whisper import WhisperModel

            _WHISPER = WhisperModel("small", device="cpu", compute_type="int8")
        return _WHISPER


def _transcribe_sync(wav: str) -> str:
    import numpy as np

    with wave.open(wav, "rb") as handle:
        audio = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    audio = audio.astype(np.float32) / 32768.0
    if audio.size == 0:
        return ""
    options = dict(
        language="zh",
        beam_size=1,
        initial_prompt="以下是普通话视频内容，使用简体中文。",
    )
    try:
        segments, _info = _whisper_model().transcribe(audio, vad_filter=True, **options)
    except AttributeError:
        # onnxruntime 版本比 faster-whisper 旧时没有 Silero VAD，关掉照样能转写
        segments, _info = _whisper_model().transcribe(audio, vad_filter=False, **options)
    return _clean("".join(segment.text for segment in segments))


def _run_timed(func, timeout: float, *args) -> Tuple[str, str]:
    """在守护线程里跑（whisper 无法中断），超时就放弃并返回原因。"""
    box: Dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = func(*args)
        except Exception as exc:  # noqa: BLE001
            box["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(max(5.0, float(timeout)))
    if thread.is_alive():
        return "", "转写超时"
    if "error" in box:
        reason = str(box["error"])
        if reason.startswith("ModuleNotFoundError"):
            return "", "本地没装 faster-whisper"
        return "", reason
    return str(box.get("value") or ""), ""


def _subtitle_service_asr(wav: str, deadline: float) -> str:
    """本机 subtitle 服务（23405）在线时优先用它，省得再加载一份模型。"""
    try:
        with _http("http://127.0.0.1:23405/health", timeout=0.8) as response:
            if response.status != 200:
                return ""
    except Exception:  # noqa: BLE001
        return ""
    try:
        body = json.dumps({"path": wav}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        with _http(
            "http://127.0.0.1:23405/asr", headers, data=body,
            timeout=_remain(deadline, 180),
        ) as response:
            payload = json.loads(response.read().decode("utf-8", "ignore"))
        if isinstance(payload, dict) and payload.get("ok"):
            return _clean((payload.get("data") or {}).get("text"))
    except Exception:  # noqa: BLE001
        pass
    return ""


def _transcribe_audio(urls: List[str], headers: Dict[str, str], cfg: Dict[str, Any],
                      duration: float, deadline: float) -> Tuple[str, bool, str]:
    """返回 (文本, 是否截断, 失败原因)；任何异常都不往外抛。"""
    if not urls:
        return "", False, "没有可下载的音频直链"
    max_s = max(30, int(cfg["audio_max_s"]))
    source = ""
    wav = ""
    try:
        source = _download_audio(urls, headers, deadline)
        if not source:
            return "", False, "音频下载失败"
        wav = _ffmpeg_trim(source, max_s, deadline)
        if not wav:
            return "", False, "音频抽轨失败（缺 ffmpeg）"

        text = _subtitle_service_asr(wav, deadline)
        why = ""
        if not text:
            text, why = _run_timed(_transcribe_sync, max(30.0, deadline - time.time()), wav)
        if not text:
            return "", False, why or "whisper 没识别出文字"

        chinese = len(re.findall(r"[\u4e00-\u9fa5]", text))
        if len(text) >= 60 and chinese / len(text) < 0.35:
            return "", False, "转写出来不像中文讲解（视频可能只有 BGM）"
        truncated = bool(duration and float(duration) > float(max_s))
        return text[: max(500, int(cfg["transcript_chars"]))], truncated, ""
    except Exception as exc:  # noqa: BLE001 - 转写是兜底能力，绝不影响主流程
        return "", False, f"{type(exc).__name__}"
    finally:
        for path in (wav, source):
            if path:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass


# ---------------- handlers ----------------

def handle_parse(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    url = str(body.get("url") or "").strip()
    if not url:
        return fail("请先贴一个视频链接", 400)
    want_comments = _as_bool(body.get("with_comments"), True)
    want_transcript = _as_bool(body.get("with_transcript"), False)

    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^BV[0-9A-Za-z]{10}$", url, re.I) or re.match(r"^av\d+$", url, re.I):
            url = "https://www.bilibili.com/video/" + url
        elif re.search(
            r"(bilibili\.com|b23\.tv|douyin\.com|kuaishou\.com|gifshow\.com|ixigua\.com|toutiao\.com)",
            url, re.I,
        ):
            url = "https://" + url.lstrip("/")

    platform = _detect_platform(url)
    if platform is None:
        return fail("暂不支持这个站点：目前支持 B站 / 抖音 / 快手 / 西瓜 的视频链接", 400)

    cfg = _load_cfg()
    deadline = time.time() + float(cfg["timeout_s"])
    started = time.time()
    parser = {
        "bilibili": _parse_bili,
        "douyin": _parse_douyin,
        "kuaishou": _parse_kuaishou,
        "ixigua": _parse_ixigua,
    }[platform]
    try:
        data = parser(url, cfg, want_comments, want_transcript, deadline)
    except Exception as exc:  # noqa: BLE001 - 兜底，不让服务 500
        data = _empty_result(platform, url, f"解析失败（{type(exc).__name__}），稍后再试或换个链接")
    data["url"] = url
    elapsed_ms = int((time.time() - started) * 1000)
    _remember_last(data, elapsed_ms)
    return ok(data)


def handle_config_get(_req) -> Any:  # noqa: ANN001
    return ok(_public_cfg(_load_cfg()))


def handle_config_post(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    cfg = _load_cfg()
    cookie = body.get("cookie")
    if isinstance(cookie, dict):
        for key in COOKIE_KEYS:
            if key in cookie:
                cfg["cookie"][key] = str(cookie.get(key) or "").strip()
    raw = dict(cfg)
    for key in ("comment_n", "transcript_chars", "audio_max_s", "timeout_s"):
        if key in body:
            raw[key] = body.get(key)
    cfg = _normalize_cfg(raw)
    try:
        _save_cfg(cfg)
    except OSError as exc:
        return fail(f"配置保存失败：{exc}", 500)
    payload = _public_cfg(cfg)
    payload["saved"] = True
    return ok(payload)


def handle_parse_status(_req) -> Any:  # noqa: ANN001
    with _LAST_LOCK:
        last = dict(_LAST) if _LAST else None
    return ok({"last": last, "platforms": list(PLATFORM_NAMES.values())})


routes: Dict[str, Any] = {
    "POST /video/parse": handle_parse,
    "GET /video/config": handle_config_get,
    "POST /video/config": handle_config_post,
    "GET /video/parse/status": handle_parse_status,
}
