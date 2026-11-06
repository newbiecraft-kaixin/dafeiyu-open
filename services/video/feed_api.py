"""feed_api：刷B站（标签取片单 / 片单池 / 记录喜欢）。

属于 video 微服务（23368），由 service.py 装配：
  GET  /video/feed?tags=搞笑,pvz&count=5   按标签取一批片单（搜不到用热门兜底）
  GET  /video/pool                         片单池（?tag= 过滤，?limit= 限制条数）
  POST /video/pool                         {action:"add"|"clear", item?, keep?}
  POST /video/like                         {bvid, why} 点赞（无 cookie 只本地记录）
  GET  /video/config                       读取配置（cookie 只回是否设置）
  POST /video/config                       保存配置（支持部分字段）

数据（data/ 下）
  feed.json   {tags,count,dwell_ratio,dwell_max,dwell_total_max,cookie}
  pool.json   {updated, items:[{bvid,title,author,duration,play,url,tag,picked_at,liked}]}

所有外部请求 9 秒超时、浏览器 UA；断网/被风控/解析异常一律 fail(...)，不抛 500。
搜索接口走 wbi 签名（nav 取 mixin，wts + w_rid），失败再退回无签名接口。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import fail, ok  # noqa: E402

DATA_DIR = _HERE.parent / "data"
FEED_FILE = DATA_DIR / "feed.json"
POOL_FILE = DATA_DIR / "pool.json"

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

_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")

# nav 返回的 wbi_img 文件名拼成的 64 位串按此表重排取前 32 位
_WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_LOCK = threading.RLock()
_BUVID3 = uuid.uuid4().hex + "infoc"
_WBI_CACHE: Dict[str, Any] = {"mixin": "", "at": 0.0}


class _BiliError(Exception):
    """B站接口不可用（网络、风控、结构异常），调用方统一转成 fail(...)。"""


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


# ---------------- 配置与片单池 ----------------

def _default_config() -> Dict[str, Any]:
    return {
        "tags": ["搞笑", "pvz"],
        "count": 5,
        "dwell_ratio": 0.34,
        "dwell_max": 90,
        "dwell_total_max": 300,
        "cookie": "",
    }


def _sanitize_config(raw: Any) -> Dict[str, Any]:
    cfg = _default_config()
    if not isinstance(raw, dict):
        return cfg
    if "tags" in raw:
        cfg["tags"] = _split_tags(raw.get("tags"))
    cfg["count"] = _clamp_int(raw.get("count"), 1, 10, cfg["count"])
    cfg["dwell_ratio"] = _clamp_float(raw.get("dwell_ratio"), 0.05, 2.0, cfg["dwell_ratio"])
    cfg["dwell_max"] = _clamp_int(raw.get("dwell_max"), 1, 600, cfg["dwell_max"])
    cfg["dwell_total_max"] = _clamp_int(
        raw.get("dwell_total_max"), 1, 900, cfg["dwell_total_max"]
    )
    if "cookie" in raw:
        cfg["cookie"] = str(raw.get("cookie") or "").strip()
    return cfg


def _load_config() -> Dict[str, Any]:
    with _LOCK:
        return _sanitize_config(_read_json(FEED_FILE, None))


def _save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    clean = _sanitize_config(cfg)
    with _LOCK:
        _write_json(FEED_FILE, clean)
    return clean


def _masked_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = {key: value for key, value in cfg.items() if key != "cookie"}
    out["cookie_set"] = bool(cfg.get("cookie"))
    return out


def _normalize_item(raw: Any, default_tag: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    bvid = str(raw.get("bvid") or "").strip()
    if not _BVID_RE.match(bvid):
        found = re.search(r"/video/(BV[0-9A-Za-z]{10})", str(raw.get("url") or ""))
        bvid = found.group(1) if found else ""
    title = _clean_title(raw.get("title"))
    if not bvid or not title:
        return None
    owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
    stat = raw.get("stat") if isinstance(raw.get("stat"), dict) else {}
    try:
        play = int(raw.get("play") or stat.get("view") or 0)
    except (TypeError, ValueError):
        play = 0
    try:
        picked_at = int(raw.get("picked_at") or time.time())
    except (TypeError, ValueError):
        picked_at = int(time.time())
    item: Dict[str, Any] = {
        "bvid": bvid,
        "title": title[:160],
        "author": str(raw.get("author") or owner.get("name") or "").strip()[:80],
        "duration": _parse_duration(raw.get("duration")),
        "play": max(0, play),
        "url": f"https://www.bilibili.com/video/{bvid}",
        "tag": str(default_tag or raw.get("tag") or "").strip()[:40],
        "picked_at": picked_at,
        "liked": bool(raw.get("liked")),
    }
    if str(raw.get("why") or "").strip():
        item["why"] = str(raw.get("why")).strip()[:200]
    return item


def _load_pool() -> List[Dict[str, Any]]:
    with _LOCK:
        raw = _read_json(POOL_FILE, None)
        rows = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = _normalize_item(row)
            if item:
                out.append(item)
        return out


def _save_pool(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    with _LOCK:
        _write_json(POOL_FILE, {"updated": int(time.time()), "items": items})
    return items


def _merge_pool(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 bvid 去重、新条目在前、保留旧的 liked/picked_at，超上限裁最旧。"""
    with _LOCK:
        old = _load_pool()
        old_map = {item["bvid"]: item for item in old}
        for item in items:
            prev = old_map.get(item["bvid"])
            if not prev:
                continue
            item["liked"] = bool(prev.get("liked") or item.get("liked"))
            if prev.get("picked_at"):
                item["picked_at"] = int(prev["picked_at"])
            if prev.get("why") and not item.get("why"):
                item["why"] = prev["why"]
        seen: set = set()
        merged: List[Dict[str, Any]] = []
        for item in items + old:
            bvid = item["bvid"]
            if bvid in seen:
                continue
            seen.add(bvid)
            merged.append(item)
            if len(merged) >= POOL_MAX:
                break
        return _save_pool(merged)


def _mark_liked(bvid: str, why: str, liked: bool) -> bool:
    """把片单池里的 liked 标记落盘；条目不在池里返回 False。"""
    with _LOCK:
        rows = _load_pool()
        found = False
        for item in rows:
            if item.get("bvid") == bvid:
                item["liked"] = bool(liked)
                if why:
                    item["why"] = why
                found = True
                break
        if found:
            _save_pool(rows)
        return found


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
        raise _BiliError(f"接口返回 HTTP {exc.code}（可能被风控）") from exc
    except urllib.error.URLError as exc:
        raise _BiliError(f"网络不可用：{exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise _BiliError(f"请求超时或连接失败：{exc}") from exc
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except ValueError as exc:
        raise _BiliError("接口返回的不是 JSON（可能被风控拦截）") from exc


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
        raise _BiliError("接口返回结构异常")
    code = payload.get("code")
    if code != 0:
        message = str(payload.get("message") or "").strip()
        raise _BiliError(f"接口返回 {code} {message}".strip())
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
    except _BiliError as exc:
        errors.append(str(exc))

    if not rows:
        try:
            rows = _search_rows(_http_json(_BILI_SEARCH, params=params, headers=headers))
        except _BiliError as exc:
            errors.append(str(exc))

    items = [item for item in (_normalize_item(row, tag) for row in rows) if item]
    return items[:limit], ("" if items else "；".join(errors))


def _popular(cfg: Dict[str, Any], limit: int) -> Tuple[List[Dict[str, Any]], str]:
    headers = _bili_headers(cfg, "https://www.bilibili.com/v/popular/all")
    try:
        payload = _http_json(
            _BILI_POPULAR, params={"ps": max(10, min(50, limit)), "pn": 1}, headers=headers
        )
    except _BiliError as exc:
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


def _interleave(batches: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """各标签轮流出片，避免一个标签霸屏；顺手按 bvid 去重。"""
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    index = 0
    while any(index < len(batch) for batch in batches):
        for batch in batches:
            if index >= len(batch):
                continue
            item = batch[index]
            if item["bvid"] in seen:
                continue
            seen.add(item["bvid"])
            merged.append(item)
        index += 1
    return merged


def _dwell_plan(items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> int:
    """按视频时长 × 比例给每条一个建议停留秒数（不真的 sleep，避免请求被长时间占住）。"""
    ratio = float(cfg.get("dwell_ratio") or 0.34)
    per_max = max(1, int(cfg.get("dwell_max") or 90))
    total_max = max(1, int(cfg.get("dwell_total_max") or 300))
    spent = 0
    for item in items:
        duration = int(item.get("duration") or 0)
        seconds = int(round(duration * ratio)) if duration else 10
        seconds = min(per_max, max(3, seconds))
        seconds = max(0, min(seconds, total_max - spent))
        spent += seconds
        item["dwell_s"] = seconds
    return spent


# ---------------- handlers：片单 / 片单池 ----------------

def handle_feed(req: Any) -> Any:  # noqa: ANN001
    started = time.perf_counter()
    cfg = _load_config()
    tags = _split_tags(req.q("tags")) or list(cfg.get("tags") or []) or ["热门"]
    tags = tags[:6]
    count = _clamp_int(req.q("count"), 1, 10, int(cfg.get("count") or 5))

    errors: List[str] = []
    batches: List[List[Dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=min(4, len(tags))) as pool:
        futures = [pool.submit(_search_tag, cfg, tag, max(count * 4, 10)) for tag in tags]
        for future in futures:
            try:
                items, error = future.result()
            except Exception as exc:  # noqa: BLE001 - 单标签失败不影响其它标签
                items, error = [], f"{type(exc).__name__}: {exc}"
            if items:
                batches.append(items)
            if error:
                errors.append(error)

    source = "tag"
    if not batches:
        source = "popular"
        items, error = _popular(cfg, max(count * 2, 10))
        if items:
            batches.append(items)
        elif error:
            errors.append(error)

    merged = _interleave(batches)
    if not merged:
        parts: List[str] = []
        for error in errors:
            for part in str(error).split("；"):
                part = part.strip()
                if part and part not in parts:
                    parts.append(part)
        detail = "；".join(parts[:2]) or "接口没有返回可用视频，稍后再试"
        return fail(f"B站没刷到内容：{detail}", 502)

    picked = merged[:count]
    now = int(time.time())
    old_map = {item["bvid"]: item for item in _load_pool()}
    for item in picked:
        prev = old_map.get(item["bvid"])
        if prev:
            item["liked"] = bool(prev.get("liked"))
            if prev.get("picked_at"):
                item["picked_at"] = int(prev["picked_at"])
        else:
            item["picked_at"] = now
    _merge_pool(picked)
    dwell_total = _dwell_plan(picked, cfg)

    return ok({
        "items": picked,
        "elapsed_s": round(time.perf_counter() - started, 2),
        "source": source,
        "tags": tags,
        "dwell_total_s": dwell_total,
    })


def handle_pool_get(req: Any) -> Any:  # noqa: ANN001
    rows = _load_pool()
    tag = str(req.q("tag") or "").strip()
    if tag:
        rows = [item for item in rows if item.get("tag") == tag]
    total = len(rows)
    limit = _clamp_int(req.q("limit"), 1, 200, POOL_MAX)
    return ok({"items": rows[:limit], "total": total})


def handle_pool_post(req: Any) -> Any:  # noqa: ANN001
    body = _payload(req)
    action = str(body.get("action") or "").strip().lower()
    if action == "add":
        item = _normalize_item(body.get("item"))
        if item is None:
            return fail("item 里缺少合法的 bvid / title", 400)
        pool = _merge_pool([item])
        return ok({"added": item["bvid"], "total": len(pool)})
    if action == "clear":
        keep = body.get("keep")
        with _LOCK:
            rows = _load_pool()
            if keep is None or keep == "":
                kept: List[Dict[str, Any]] = []
            elif isinstance(keep, list):
                wanted = {str(value).strip() for value in keep if str(value).strip()}
                kept = [item for item in rows if item["bvid"] in wanted]
            else:
                number = _clamp_int(keep, 0, POOL_MAX, 0)
                kept = rows[:number]
            _save_pool(kept)
        return ok({"total": len(kept), "kept": len(kept)})
    return fail("action 只能是 add 或 clear", 400)


def handle_like(req: Any) -> Any:  # noqa: ANN001
    body = _payload(req)
    bvid = str(body.get("bvid") or "").strip()
    if not _BVID_RE.match(bvid):
        return fail("bvid 不合法（形如 BV1xx411c7mD）", 400)
    why = str(body.get("why") or "").strip()[:200]
    cfg = _load_config()
    cookie = str(cfg.get("cookie") or "")
    match = re.search(r"bili_jct=([^;]+)", cookie)
    csrf = match.group(1).strip() if match else ""

    if not csrf:
        in_pool = _mark_liked(bvid, why, True)
        note = "片单池里没有这条，只返回了结果" if not in_pool else "已在片单池记为喜欢"
        return ok({
            "bvid": bvid,
            "liked": True,
            "real": False,
            "in_pool": in_pool,
            "message": f"没配置登录 cookie（缺 bili_jct），没有真点赞；{note}",
        })

    referer = f"https://www.bilibili.com/video/{bvid}"
    headers = _bili_headers(cfg, referer)
    try:
        aid = 0
        try:
            view = _http_json(_BILI_VIEW, params={"bvid": bvid}, headers=headers)
            if isinstance(view, dict) and view.get("code") == 0:
                aid = int((view.get("data") or {}).get("aid") or 0)
        except (TypeError, ValueError, _BiliError):
            aid = 0
        form: Dict[str, Any] = {"like": 1, "csrf": csrf}
        if aid:
            form["aid"] = aid
        else:
            form["bvid"] = bvid
        payload = _http_json(_BILI_LIKE, headers=headers, form=form, timeout=10.0)
    except _BiliError as exc:
        return fail(f"点赞失败：{exc}", 502)

    if not isinstance(payload, dict):
        return fail("点赞失败：接口返回结构异常", 502)
    code = payload.get("code")
    message = str(payload.get("message") or "").strip()
    if code == 0 or code == 65006 or "已经" in message:
        _mark_liked(bvid, why, True)
        return ok({
            "bvid": bvid,
            "liked": True,
            "real": True,
            "in_pool": True,
            "message": "已点赞" if code == 0 else "之前就点过赞了",
        })
    return fail(f"点赞失败（{code} {message}）", 502)


# ---------------- handlers：配置 / 状态 ----------------

def handle_config_get(_req: Any) -> Any:  # noqa: ANN001
    return ok(_masked_config(_load_config()))


def handle_config_post(req: Any) -> Any:  # noqa: ANN001
    body = _payload(req)
    current = _load_config()
    if "tags" in body and not isinstance(body.get("tags"), (list, tuple, str)):
        return fail("tags 必须是标签数组或逗号分隔的字符串", 400)
    merged = dict(current)
    merged.update({key: value for key, value in body.items() if key in _default_config()})
    if body.get("clear_cookie"):
        merged["cookie"] = ""
    saved = _save_config(merged)
    return ok(_masked_config(saved))


def status() -> Dict[str, Any]:
    """给 /health 用：片单池条数、标签、默认条数、cookie 是否已设置。"""
    try:
        cfg = _load_config()
        pool = _load_pool()
    except Exception:  # noqa: BLE001 - health 不允许把服务带崩
        return {"pool": 0, "tags": [], "count": 5, "cookie_set": False}
    return {
        "pool": len(pool),
        "tags": list(cfg.get("tags") or []),
        "count": int(cfg.get("count") or 5),
        "cookie_set": bool(cfg.get("cookie")),
    }


def _guard(handler: Any) -> Any:
    """兜底：任何未预料的异常都转成 fail(...)，不让 HTTP 基座回 500。"""
    def wrapper(req: Any) -> Any:
        try:
            return handler(req)
        except Exception as exc:  # noqa: BLE001
            name = getattr(handler, "__name__", "handler").replace("handle_", "")
            return fail(f"{name} 处理失败：{type(exc).__name__}: {exc}", 502)
    return wrapper


routes: Dict[str, Any] = {
    "GET /video/feed": _guard(handle_feed),
    "GET /video/pool": _guard(handle_pool_get),
    "POST /video/pool": _guard(handle_pool_post),
    "POST /video/like": _guard(handle_like),
    "GET /video/feed/config": _guard(handle_config_get),
    "POST /video/feed/config": _guard(handle_config_post),
}
