"""feed_api 的通用底座：常量、接口地址、共享锁与请求/数据小工具。

从 feed_api.py 按职责拆出（纯移动，逻辑与签名不变）；
配置与片单池（_feed_store）、平台抓取（_feed_source）和门面 feed_api.py
都从这里取工具。_LOCK 是 feed.json / pool.json / wbi 缓存共用的全局锁。
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve()
DATA_DIR = _HERE.parent / "data"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 9.0
POOL_MAX = 60

_BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"
_BILI_WBI_SEARCH = "https://api.bilibili.com/x/web-interface/wbi/search/type"
_BILI_SEARCH = "https://api.bilibili.com/x/web-interface/search/type"
_BILI_POPULAR = "https://api.bilibili.com/x/web-interface/popular"
_BILI_VIEW = "https://api.bilibili.com/x/web-interface/view"
_BILI_LIKE = "https://api.bilibili.com/x/web-interface/archive/like"

_DOUYIN_SEARCH = "https://www.douyin.com/aweme/v1/web/general/search/single/"

_KS_SEARCH_PAGE = "https://www.kuaishou.com/search/video?searchKey={}"
_KS_GRAPHQL = "https://www.kuaishou.com/graphql"
_KS_HOT_QUERY = (
    "query visionHotRank($page: String) { visionHotRank(page: $page) "
    "{ result items { id name photoIds hotValue } } }"
)

PLATFORMS = {"bilibili": "B站", "douyin": "抖音", "kuaishou": "快手"}

# 抖音搜索参数（MediaCrawler 免签分支：/v1/web/general/search 不要求 a_bogus）
_DOUYIN_PARAMS = {
    "search_channel": "aweme_video_web",
    "enable_history": "1",
    "search_source": "tab_search",
    "query_correct_type": "1",
    "is_filter_search": "0",
    "from_group_id": "7378810571505847586",
    "offset": "0",
    "count": "15",
    "need_filter_settings": "1",
    "list_type": "multi",
    "search_id": "",
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "version_code": "190600",
    "version_name": "19.6.0",
    "update_version_code": "170400",
    "pc_client_type": "1",
    "cookie_enabled": "true",
    "browser_language": "zh-CN",
    "browser_platform": "MacIntel",
    "browser_name": "Chrome",
    "browser_version": "125.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "os_name": "Mac OS",
    "os_version": "10.15.7",
    "cpu_core_num": "8",
    "device_memory": "8",
    "engine_version": "109.0",
    "platform": "PC",
    "screen_width": "2560",
    "screen_height": "1440",
    "effective_type": "4g",
    "round_trip_time": "50",
}

_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")

_LOCK = threading.RLock()


class _FeedError(Exception):
    """外部平台接口不可用（网络、风控、结构异常），调用方统一转成 fail(...)。"""


# ---------------- 通用小工具 ----------------

def _payload(req: Any) -> Dict[str, Any]:
    return req.payload if isinstance(req.payload, dict) else {}


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return round(max(low, min(high, number)), 3)


def _clean_title(value: Any) -> str:
    """搜索结果标题带 <em> 高亮，去标签并还原实体。"""
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return unescape(text).strip()


def _parse_duration(value: Any) -> int:
    """秒数 / "03:21" / "1:02:03" → 整数秒。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip()
    if not text:
        return 0
    if ":" in text:
        try:
            parts = [float(part) for part in text.split(":")]
        except ValueError:
            return 0
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return max(0, int(seconds))
    try:
        return max(0, int(float(text)))
    except ValueError:
        return 0


def _split_tags(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = re.split(r"[,，、;；\s]+", str(value or ""))
    out: List[str] = []
    for item in raw:
        tag = item.strip()[:40]
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= 12:
            break
    return out


# ---------------- B站 HTTP ----------------

def _http_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    form: Optional[Dict[str, Any]] = None,
    timeout: float = TIMEOUT,
) -> Any:
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)
    body = None
    hdrs = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise _FeedError(f"接口返回 HTTP {exc.code}（可能被风控）") from exc
    except urllib.error.URLError as exc:
        raise _FeedError(f"网络不可用：{exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise _FeedError(f"请求超时或连接失败：{exc}") from exc
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except ValueError as exc:
        raise _FeedError("接口返回的不是 JSON（可能被风控拦截）") from exc
