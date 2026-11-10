"""feed_api 的信息流抓取：B站搜索/wbi 签名/热门、抖音免签搜索、快手页面截流与热榜。

从 feed_api.py 按职责拆出（纯移动，逻辑与签名不变）；
wbi 缓存（_WBI_CACHE）与 buvid3（_BUVID3）等模块级状态归属本模块。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Tuple

from _feed_common import (
    DATA_DIR,
    TIMEOUT,
    UA,
    _BILI_NAV,
    _BILI_POPULAR,
    _BILI_SEARCH,
    _BILI_WBI_SEARCH,
    _DOUYIN_PARAMS,
    _DOUYIN_SEARCH,
    _FeedError,
    _KS_GRAPHQL,
    _KS_HOT_QUERY,
    _KS_SEARCH_PAGE,
    _LOCK,
    _clean_title,
    _http_json,
    _read_json,
)
from _feed_store import _item_url, _load_config, _normalize_item

_BUVID3 = uuid.uuid4().hex + "infoc"
_WBI_CACHE: Dict[str, Any] = {"mixin": "", "at": 0.0}

# nav 返回的 wbi_img 文件名拼成的 64 位串按此表重排取前 32 位
_WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


# ---------------- B站 HTTP / wbi 签名 ----------------

def _bili_headers(cfg: Dict[str, Any], referer: str = "https://www.bilibili.com/") -> Dict[str, str]:
    cookie = str(cfg.get("cookie") or "").strip()
    if cookie and "=" not in cookie:
        cookie = f"SESSDATA={cookie}"
    if "buvid3" not in cookie:
        cookie = (cookie + "; " if cookie else "") + "buvid3=" + _BUVID3
    return {
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Cookie": cookie,
    }


def _wbi_mixin_from(img_url: Any, sub_url: Any) -> str:
    raw = ""
    for url in (img_url, sub_url):
        name = str(url or "").rsplit("/", 1)[-1]
        raw += name.split(".")[0]
    if len(raw) < 64:
        return ""
    return "".join(raw[index] for index in _WBI_TAB)[:32]


def _wbi_mixin(cfg: Dict[str, Any]) -> str:
    """nav 接口取 wbi_img 拼 mixin；缓存 30 分钟，失败返回空串（调用方退回无签名接口）。"""
    now = time.time()
    with _LOCK:
        if _WBI_CACHE["mixin"] and now - float(_WBI_CACHE["at"]) < 1800:
            return str(_WBI_CACHE["mixin"])
    payload = _http_json(_BILI_NAV, headers=_bili_headers(cfg))
    data = payload.get("data") if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return ""
    wbi = data.get("wbi_img") if isinstance(data.get("wbi_img"), dict) else {}
    mixin = _wbi_mixin_from(wbi.get("img_url"), wbi.get("sub_url"))
    if mixin:
        with _LOCK:
            _WBI_CACHE["mixin"] = mixin
            _WBI_CACHE["at"] = now
    return mixin


def _wbi_signed(params: Dict[str, Any], mixin: str) -> Dict[str, Any]:
    """加 wts 时间戳、排序 urlencode，再 md5(query + mixin) 作 w_rid。"""
    query_params = {key: re.sub(r"[!'()*]", "", str(value)) for key, value in params.items()}
    query_params["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(query_params.items()))
    query_params["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return query_params


def _search_rows(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise _FeedError("接口返回结构异常")
    code = payload.get("code")
    if code != 0:
        message = str(payload.get("message") or "").strip()
        raise _FeedError(f"接口返回 {code} {message}".strip())
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    rows = data.get("result")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _search_tag(cfg: Dict[str, Any], tag: str, limit: int) -> Tuple[List[Dict[str, Any]], str]:
    """按标签搜索：优先 wbi 签名接口，没结果再退回无签名接口。"""
    params = {"search_type": "video", "keyword": tag, "page": 1, "order": "totalrank"}
    referer = "https://search.bilibili.com/video?keyword=" + urllib.parse.quote_plus(tag)
    headers = _bili_headers(cfg, referer)
    errors: List[str] = []
    rows: List[Dict[str, Any]] = []

    try:
        mixin = _wbi_mixin(cfg)
        if mixin:
            signed = _wbi_signed({**params, "web_location": 1430654}, mixin)
            rows = _search_rows(_http_json(_BILI_WBI_SEARCH, params=signed, headers=headers))
    except _FeedError as exc:
        errors.append(str(exc))

    if not rows:
        try:
            rows = _search_rows(_http_json(_BILI_SEARCH, params=params, headers=headers))
        except _FeedError as exc:
            errors.append(str(exc))

    items = [item for item in (_normalize_item(row, tag) for row in rows) if item]
    return items[:limit], ("" if items else "；".join(errors))


def _popular(cfg: Dict[str, Any], limit: int) -> Tuple[List[Dict[str, Any]], str]:
    headers = _bili_headers(cfg, "https://www.bilibili.com/v/popular/all")
    try:
        payload = _http_json(
            _BILI_POPULAR, params={"ps": max(10, min(50, limit)), "pn": 1}, headers=headers
        )
    except _FeedError as exc:
        return [], str(exc)
    if not isinstance(payload, dict):
        return [], "接口返回结构异常"
    if payload.get("code") != 0:
        return [], f"接口返回 {payload.get('code')} {payload.get('message') or ''}".strip()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    rows = data.get("list")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    items = [item for item in (_normalize_item(row, "热门") for row in rows) if item]
    return items[:limit], ""


# ---------------- 抖音 ----------------

def _platform_cookie(platform: str) -> str:
    """B站 Cookie 在 feed.json；抖音 / 快手与「看视频」共用 parse.json。"""
    if platform == "bilibili":
        return str(_load_config().get("cookie") or "").strip()
    raw = _read_json(DATA_DIR / "parse.json", None)
    cookie = raw.get("cookie") if isinstance(raw, dict) else None
    if isinstance(cookie, dict):
        return str(cookie.get(platform) or "").strip()
    return ""


def _cookie_dict(cookie: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in cookie.split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            name = name.strip()
            if name:
                out[name] = value.strip()
    return out


def _douyin_search_tag(cookie: str, tag: str, limit: int) -> Tuple[List[Dict[str, Any]], str]:
    """免签搜索：/aweme/v1/web/general/search/single/ 不要求 a_bogus，
    但必须带 x-tt-argus、uifid 以及登录 Cookie。"""
    jar = _cookie_dict(cookie)
    params = dict(_DOUYIN_PARAMS)
    params["keyword"] = tag
    params["count"] = str(max(15, min(30, limit)))
    params["webid"] = (jar.get("ttwid") or "")[:19] or "7525082444551310602"
    params["msToken"] = jar.get("msToken", "")
    referer = urllib.parse.quote(
        f"https://www.douyin.com/search/{urllib.parse.quote(tag)}"
        "?aid=f594bbd9-a0e2-4651-9319-ebe3cb6298c1&type=general",
        safe=":/",
    )
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
        "Cookie": cookie,
        "x-tt-argus": "1",
        "Origin": "https://www.douyin.com",
    }
    uifid = jar.get("UIFID") or jar.get("UIFID_TEMP")
    if uifid:
        headers["uifid"] = uifid
    try:
        payload = _http_json(_DOUYIN_SEARCH, params=params, headers=headers, timeout=15.0)
    except _FeedError as exc:
        return [], str(exc)
    if not isinstance(payload, dict):
        return [], "接口返回结构异常"
    if payload.get("status_code") not in (0, None):
        detail = str(payload.get("status_msg") or "").strip()
        return [], f"接口返回 {payload.get('status_code')} {detail}".strip()
    rows = payload.get("data")
    if not isinstance(rows, list):
        return [], "接口没有返回搜索结果"

    items: List[Dict[str, Any]] = []
    for row in rows:
        info = row.get("aweme_info") if isinstance(row, dict) else None
        if not isinstance(info, dict):
            continue
        vid = str(info.get("aweme_id") or "").strip()
        desc = _clean_title(info.get("desc"))
        if not vid or not desc:
            continue
        video = info.get("video") if isinstance(info.get("video"), dict) else {}
        author = info.get("author") if isinstance(info.get("author"), dict) else {}
        stat = info.get("statistics") if isinstance(info.get("statistics"), dict) else {}
        try:
            duration = int(video.get("duration") or 0) // 1000
        except (TypeError, ValueError):
            duration = 0
        item = _normalize_item({
            "platform": "douyin",
            "vid": vid,
            "title": desc,
            "author": author.get("nickname"),
            "duration": duration,
            "play": stat.get("digg_count") or stat.get("play_count"),
            "tag": tag,
            "url": _item_url("douyin", vid),
        }, tag, "douyin")
        if item:
            items.append(item)
    return items[:limit], ("" if items else "没有搜到抖音视频")


# ---------------- 快手 ----------------

def _ks_collect(page: Any, tag: str, limit: int) -> Tuple[List[Dict[str, Any]], str]:
    """打开快手搜索页，截获页面自己发出的 /rest/v/search/feed 响应（页面已带 __NS_hxfalcon）。"""
    captured: List[Dict[str, Any]] = []

    def on_response(response: Any) -> None:
        if "/rest/v/search/feed" not in response.url:
            return
        try:
            captured.append(json.loads(response.body().decode("utf-8", "ignore")))
        except Exception:  # noqa: BLE001 - 非 JSON / 已释放的响应直接忽略
            pass

    page.on("response", on_response)
    try:
        page.goto(
            _KS_SEARCH_PAGE.format(urllib.parse.quote(tag)),
            wait_until="domcontentloaded",
            timeout=45000,
        )
        for _ in range(4):
            page.wait_for_timeout(2200)
            if any(data.get("result") == 1 and data.get("feeds") for data in captured):
                break
    except Exception as exc:  # noqa: BLE001
        return [], f"快手页面打开失败：{exc}"
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:  # noqa: BLE001
            pass

    good = next((data for data in captured if data.get("result") == 1), None)
    if good is None:
        code = captured[0].get("result") if captured else "无响应"
        if code == 2:
            return [], "快手搜索被限流（登录可能已失效，请重新扫码登录快手）"
        return [], f"快手搜索返回 {code}"

    items: List[Dict[str, Any]] = []
    for feed in good.get("feeds") or []:
        if not isinstance(feed, dict):
            continue
        photo = feed.get("photo") if isinstance(feed.get("photo"), dict) else {}
        vid = str(photo.get("id") or feed.get("photo_id") or "").strip()
        title = _clean_title(photo.get("caption") or feed.get("caption"))
        if not vid or not title:
            continue
        author = feed.get("author") if isinstance(feed.get("author"), dict) else {}
        try:
            duration = int(photo.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration >= 1000:  # 快手时长是毫秒
            duration //= 1000
        try:
            play = int(photo.get("viewCount") or photo.get("likeCount") or 0)
        except (TypeError, ValueError):
            play = 0
        item = _normalize_item({
            "platform": "kuaishou",
            "vid": vid,
            "title": title,
            "author": author.get("name") or photo.get("userName"),
            "duration": duration,
            "play": play,
            "tag": tag,
        }, tag, "kuaishou")
        if item:
            items.append(item)
    return items[:limit], ("" if items else "没有搜到快手视频")


def _kuaishou_search_batches(
    cookie: str, tags: List[str], limit: int
) -> Tuple[List[List[Dict[str, Any]]], List[str]]:
    """快手签名环境在页面里，必须开浏览器；一次请求只开一个，多个标签顺序复用同一页。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return [], [f"没装 playwright：{exc}"]
    batches: List[List[Dict[str, Any]]] = []
    errors: List[str] = []
    pw = None
    browser = None
    try:
        pw = sync_playwright().start()
        last: Any = None
        for kwargs in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
                    **kwargs,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
        if browser is None:
            return [], [f"启动浏览器失败：{last}"]
        context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai", user_agent=UA)
        pairs = [
            {"name": name, "value": value, "domain": ".kuaishou.com", "path": "/"}
            for name, value in _cookie_dict(cookie).items()
        ]
        if pairs:
            try:
                context.add_cookies(pairs)
            except Exception:  # noqa: BLE001
                pass
        page = context.new_page()
        for tag in tags:
            items, error = _ks_collect(page, tag, limit)
            if items:
                batches.append(items)
            if error:
                errors.append(error)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"快手浏览器异常：{exc}")
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass
    return batches, errors


def _kuaishou_hot(limit: int) -> Tuple[List[Dict[str, Any]], str]:
    """热榜兜底：GraphQL visionHotRank 免签可用，只有话题名和 photoIds。"""
    body = json.dumps({
        "operationName": "visionHotRank",
        "variables": {"page": "home"},
        "query": _KS_HOT_QUERY,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _KS_GRAPHQL,
        data=body,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Origin": "https://www.kuaishou.com",
            "Referer": "https://www.kuaishou.com/new-reco",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return [], f"快手热榜不可用：{exc}"
    rank = ((payload or {}).get("data") or {}).get("visionHotRank") or {}
    if rank.get("result") != 1:
        return [], f"快手热榜返回 {rank.get('result')}"
    items: List[Dict[str, Any]] = []
    for topic in rank.get("items") or []:
        if not isinstance(topic, dict):
            continue
        name = _clean_title(topic.get("name"))
        for vid in topic.get("photoIds") or []:
            item = _normalize_item({
                "platform": "kuaishou",
                "vid": str(vid),
                "title": name or str(vid),
                "tag": "热门",
            }, "热门", "kuaishou")
            if item:
                items.append(item)
            if len(items) >= limit:
                return items, ""
    return items, ("" if items else "快手热榜为空")
