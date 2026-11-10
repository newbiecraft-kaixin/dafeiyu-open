"""video.parse_api 的通用底座：常量、UA、直连 opener 与页面/JSON 小工具。

从 parse_api.py 按职责拆出（纯移动，逻辑与签名不变）；
平台解析器（_parse_bili / _parse_media）、配置与存档（_parse_config）、
音频转写（_parse_transcribe）与门面 parse_api.py 都从这里取工具。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

_HERE = Path(__file__).resolve()
DATA_DIR = _HERE.parent / "data"

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
