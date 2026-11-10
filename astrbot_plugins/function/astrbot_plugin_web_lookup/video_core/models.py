"""视频解析共享配置、缓存、平台识别与通用小工具。"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


UA_M = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


DEFAULT_CFG = {
    "bili_cookie": "",
    "bili_subtitle_chars": 4000,
    "video_comment_n": 10,
    "video_audio_max_s": 300,
    "video_parse_timeout_s": 180,
    "video_transcript_chars": 4000,
    "video_audio_max_mb": 60,
    "video_ocr_max_images": 6,
    "video_ocr_chars": 1500,
}


NAMES = {"bilibili": "B站", "douyin": "抖音", "kuaishou": "快手", "ixigua": "西瓜"}


AUTHOR_LABEL = {"bilibili": "UP主"}


_LOCKS: dict[str, asyncio.Lock] = {}


_RESULT_CACHE: dict[str, tuple[float, dict]] = {}


_RESULT_CACHE_TTL_S = 600.0


_RESULT_CACHE_MAX = 48


def _cache_key(url: str) -> str:
    """短链和长链尽量落到同一个 key 上。"""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url or "", re.I)
    if m:
        return "bili:" + m.group(1).upper()
    return (url or "").strip().split("#")[0].split("?")[0].lower()


def _lock(platform: str) -> asyncio.Lock:
    lock = _LOCKS.get(platform)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[platform] = lock
    return lock


def detect_platform(url: str) -> str | None:
    """按域名判断平台；短链（b23.tv / v.douyin / v.kuaishou / v.ixigua）也算。"""
    if not re.match(r"^https?://", (url or "").strip(), re.I):
        return None
    host = urlsplit(url.strip()).netloc.lower()
    if re.search(r"(b23\.tv|bili2233\.cn|bilibili\.com)$", host):
        return "bilibili"
    if re.search(r"(douyin\.com|iesdouyin\.com)$", host):
        return "douyin"
    if re.search(r"(kuaishou\.com|gifshow\.com|chenzhongtech\.com)$", host):
        return "kuaishou"
    if re.search(r"(ixigua\.com|toutiao\.com)$", host):
        return "ixigua"
    return None


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _num(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _fmt_num(v) -> str:
    n = _num(v)
    if n <= 0:
        return "0"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def _fmt_dur(v) -> str:
    sec = _num(v)
    if sec <= 0:
        return ""
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _dur_sec(v) -> int:
    if isinstance(v, (int, float)):
        return max(0, int(v))
    sec = 0
    for p in re.split(r"[:：]", str(v or "")):
        if p.strip().isdigit():
            sec = sec * 60 + int(p.strip())
    return sec


def _duration_from_html(html: str) -> int:
    """从分享页 HTML 里抠时长（SEO 页没有 aweme JSON 时的兜底）。秒。"""
    text = str(html or "").replace("\\u002F", "/").replace("\\/", "/")
    for pat in (
        r'<meta[^>]+property="og:video:duration"[^>]+content="(\d+)"',
        r'<meta[^>]+content="(\d+)"[^>]+property="og:video:duration"',
        r'"video_duration"\s*:\s*(\d+)',
        r'"duration"\s*:\s*(\d+)',
    ):
        m = re.search(pat, text, re.I)
        if not m:
            continue
        dur = _num(m.group(1))
        if dur > 6000:  # 抖音给的常是毫秒
            dur //= 1000
        if dur > 0:
            return dur
    return 0


def _empty(platform: str | None, url: str, note: str) -> dict:
    return {
        "platform": platform,
        "url": url,
        "title": "",
        "author": "",
        "duration": 0,
        "stats": {},
        "desc": "",
        "tags": [],
        "extra": [],
        "subtitle": "",
        "comments": [],
        "transcript": "",
        "transcript_truncated": False,
        "notes": [note] if note else [],
        "media_urls": [],
        "image_urls": [],
    }


async def _resolve(sess, url: str) -> str:
    try:
        async with sess.get(url, allow_redirects=True, max_redirects=8) as r:
            return str(r.url or url)
    except Exception:  # noqa: BLE001
        return url


async def _get_text(sess, url: str, ua: str, referer: str = "") -> str:
    headers = {"User-Agent": ua}
    if referer:
        headers["Referer"] = referer
    try:
        async with sess.get(url, headers=headers, allow_redirects=True, max_redirects=8) as r:
            return await r.text(errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def _window_json(html: str, name: str):
    """取 window.<name> = {...}; 解析失败返回 None。"""
    m = re.search(rf"window\.{re.escape(name)}\s*=\s*", html or "")
    if not m:
        return None
    dec = json.JSONDecoder()
    try:
        return dec.raw_decode(html[m.end():])[0]
    except Exception:  # noqa: BLE001
        return None


def _find_dict(obj, pred, max_nodes: int = 30000):
    """在 JSON 里广度优先找第一个满足条件的 dict（页面结构常变，用这个兜底）。"""
    stack = [obj]
    seen = 0
    while stack and seen < max_nodes:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, dict):
            if pred(cur):
                return cur
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _og(html: str) -> dict:
    out: dict[str, str] = {}
    for m in re.finditer(r'<meta[^>]+>', html or "", re.I):
        tag = m.group(0)
        n = re.search(r'(?:name|property)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        c = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if n and c:
            out.setdefault(n.group(1).lower(), c.group(1))
    t = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    if t:
        out.setdefault("title", _clean(t.group(1)))
    return out


def _title_from_html(html: str) -> str:
    og = _og(html)
    title = _clean(og.get("og:title") or og.get("title") or "")
    for junk in ("在抖音记录美好生活", "短视频-快手", "抖音", "快手", "西瓜视频"):
        if title.startswith(junk) and len(title) <= len(junk) + 6:
            return ""
    return re.sub(r"\s*[|｜-]\s*(抖音|快手|西瓜视频).*$", "", title).strip()


def _ld_items(html: str) -> list[dict]:
    """页面里的 schema.org JSON-LD（抖音 SEO 页把标题/作者/图片/点赞都塞在这里）。"""
    out: list[dict] = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
        html or "",
        re.I,
    ):
        try:
            obj = json.loads(m.group(1).strip())
        except Exception:  # noqa: BLE001
            continue
        for it in (obj if isinstance(obj, list) else [obj]):
            if not isinstance(it, dict):
                continue
            out.append(it)
            graph = it.get("@graph")
            if isinstance(graph, list):
                out.extend(x for x in graph if isinstance(x, dict))
    return out


def _ld_article(html: str) -> dict:
    """取第一条 article / video 类型的 JSON-LD；图文作品 SEO 页就靠它。"""
    for it in _ld_items(html):
        if str(it.get("@type") or "").lower() in ("article", "videoobject", "imageobject"):
            return it
    return {}


def _ld_count(art: dict, action: str) -> int:
    """JSON-LD 里的互动数：interactionType 可能是字符串，也可能是 {"@type": ...}。"""
    for it in art.get("interactionStatistic") or []:
        if not isinstance(it, dict):
            continue
        typ = it.get("interactionType")
        if isinstance(typ, dict):
            typ = typ.get("@type")
        if action and action in str(typ or "").lower():
            return _num(it.get("userInteractionCount"))
    return 0
