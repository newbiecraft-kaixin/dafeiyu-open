"""feed_api：刷视频（B站 / 抖音 / 快手，按标签取片单 / 片单池 / 记录喜欢）。

属于 video 微服务（23368），由 service.py 装配：
  GET  /video/feed?platform=bilibili&tags=搞笑,pvz&count=5
                                          按标签取一批片单（B站搜不到用热门兜底）
  GET  /video/pool                         片单池（?tag=/?platform= 过滤，?limit= 限制条数）
  POST /video/pool                         {action:"add"|"clear", item?, keep?}
  POST /video/like                         {platform, vid, bvid?, why} 喜欢
  GET  /video/config                       读取配置（cookie 只回是否设置）
  POST /video/config                       保存配置（支持部分字段）

实现按职责拆在同包子模块（本文件只保留编排 / handlers 与转发，逻辑不变）：
  _feed_common   常量 / 接口地址 / 共享锁 / HTTP 与数据小工具
  _feed_store    feed.json 配置与 pool.json 片单池
  _feed_source   B站 / 抖音 / 快手 信息流抓取（含 wbi 签名与热榜兜底）

平台
  bilibili  搜索走 wbi 签名（nav 取 mixin，wts + w_rid），失败退回无签名接口；cookie 读 feed.json
  douyin    免签搜索 /aweme/v1/web/general/search/single/，需要 x-tt-argus + uifid + Cookie
  kuaishou  打开搜索页让页面自己发 /rest/v/search/feed（页面内置 __NS_hxfalcon 签名），
            截获响应；被限流（result:2）时退到 graphql 热榜
  抖音 / 快手 的 Cookie 都来自 parse.json（扫码登录写入，与「看视频」共用）

数据（data/ 下）
  feed.json   {tags,count,dwell_ratio,dwell_max,dwell_total_max,cookie}
  pool.json   {updated, items:[{platform,vid,bvid?,title,author,duration,play,url,tag,picked_at,liked}]}

所有外部请求 9 秒超时、浏览器 UA；断网/被风控/解析异常一律 fail(...)，不抛 500。
"""
from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import fail, ok  # noqa: E402

from _feed_common import (  # noqa: E402  转发常量与工具，保持 feed_api.X 可用
    DATA_DIR,
    PLATFORMS,
    POOL_MAX,
    TIMEOUT,
    UA,
    _BILI_LIKE,
    _BILI_VIEW,
    _BVID_RE,
    _FeedError,
    _LOCK,
    _clamp_int,
    _http_json,
    _payload,
    _split_tags,
)
from _feed_source import (  # noqa: E402
    _bili_headers,
    _douyin_search_tag,
    _kuaishou_hot,
    _kuaishou_search_batches,
    _platform_cookie,
    _popular,
    _search_tag,
)
from _feed_store import (  # noqa: E402  转发配置 / 片单池
    FEED_FILE,
    POOL_FILE,
    _default_config,
    _item_key,
    _load_config,
    _load_pool,
    _mark_liked,
    _masked_config,
    _merge_pool,
    _normalize_item,
    _save_config,
    _save_pool,
)


# ---------------- handlers：片单 / 片单池 ----------------

def _error_detail(errors: List[str]) -> str:
    parts: List[str] = []
    for error in errors:
        for part in str(error).split("；"):
            part = part.strip()
            if part and part not in parts:
                parts.append(part)
    return "；".join(parts[:2]) or "接口没有返回可用视频，稍后再试"


def _interleave(batches: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """各标签轮流出片，避免一个标签霸屏；顺手按「平台:vid」去重。"""
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    index = 0
    while any(index < len(batch) for batch in batches):
        for batch in batches:
            if index >= len(batch):
                continue
            item = batch[index]
            key = _item_key(item)
            if key in seen:
                continue
            seen.add(key)
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


def handle_feed(req: Any) -> Any:  # noqa: ANN001
    started = time.perf_counter()
    platform = str(req.q("platform") or "bilibili").strip().lower()
    if platform not in PLATFORMS:
        return fail("platform 只能是 bilibili / douyin / kuaishou", 400)
    cfg = _load_config()
    tags = _split_tags(req.q("tags")) or list(cfg.get("tags") or []) or ["热门"]
    tags = tags[:6]
    count = _clamp_int(req.q("count"), 1, 10, int(cfg.get("count") or 5))
    limit = max(count * 4, 10)

    errors: List[str] = []
    batches: List[List[Dict[str, Any]]] = []
    note = ""

    if platform == "kuaishou":
        cookie = _platform_cookie("kuaishou")
        if not cookie:
            return fail("还没登录快手：请在下方「扫码登录」里扫码后再刷", 400)
        batches, errors = _kuaishou_search_batches(cookie, tags, limit)
    elif platform == "douyin":
        cookie = _platform_cookie("douyin")
        if not cookie:
            return fail("还没登录抖音：请在下方「扫码登录」里扫码后再刷", 400)
        with ThreadPoolExecutor(max_workers=min(4, len(tags))) as pool:
            futures = [pool.submit(_douyin_search_tag, cookie, tag, limit) for tag in tags]
            for future in futures:
                try:
                    items, error = future.result()
                except Exception as exc:  # noqa: BLE001 - 单标签失败不影响其它标签
                    items, error = [], f"{type(exc).__name__}: {exc}"
                if items:
                    batches.append(items)
                if error:
                    errors.append(error)
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(tags))) as pool:
            futures = [pool.submit(_search_tag, cfg, tag, limit) for tag in tags]
            for future in futures:
                try:
                    items, error = future.result()
                except Exception as exc:  # noqa: BLE001
                    items, error = [], f"{type(exc).__name__}: {exc}"
                if items:
                    batches.append(items)
                if error:
                    errors.append(error)

    source = "tag"
    if not batches:
        source = "popular"
        if platform == "bilibili":
            items, error = _popular(cfg, max(count * 2, 10))
        elif platform == "kuaishou":
            items, error = _kuaishou_hot(max(count * 2, 10))
            note = "快手搜索被限流，这次给的是热榜"
        else:
            items, error = [], "抖音没有热门兜底，换个标签再试"
        if items:
            batches.append(items)
        elif error:
            errors.append(error)

    merged = _interleave(batches)
    if not merged:
        return fail(f"{PLATFORMS[platform]}没刷到内容：{_error_detail(errors)}", 502)

    picked = merged[:count]
    now = int(time.time())
    old_map = {_item_key(item): item for item in _load_pool()}
    for item in picked:
        prev = old_map.get(_item_key(item))
        if prev:
            item["liked"] = bool(prev.get("liked"))
            if prev.get("picked_at"):
                item["picked_at"] = int(prev["picked_at"])
        else:
            item["picked_at"] = now
    _merge_pool(picked)
    dwell_total = _dwell_plan(picked, cfg)

    payload: Dict[str, Any] = {
        "platform": platform,
        "platform_name": PLATFORMS[platform],
        "items": picked,
        "elapsed_s": round(time.perf_counter() - started, 2),
        "source": source,
        "tags": tags,
        "dwell_total_s": dwell_total,
    }
    if note:
        payload["note"] = note
    return ok(payload)


def handle_pool_get(req: Any) -> Any:  # noqa: ANN001
    rows = _load_pool()
    tag = str(req.q("tag") or "").strip()
    if tag:
        rows = [item for item in rows if item.get("tag") == tag]
    platform = str(req.q("platform") or "").strip().lower()
    if platform:
        rows = [item for item in rows if item.get("platform") == platform]
    total = len(rows)
    limit = _clamp_int(req.q("limit"), 1, 200, POOL_MAX)
    return ok({"items": rows[:limit], "total": total})


def handle_pool_post(req: Any) -> Any:  # noqa: ANN001
    body = _payload(req)
    action = str(body.get("action") or "").strip().lower()
    if action == "add":
        item = _normalize_item(body.get("item"))
        if item is None:
            return fail("item 里缺少合法的视频 id / title", 400)
        pool = _merge_pool([item])
        return ok({"added": item["vid"], "platform": item["platform"], "total": len(pool)})
    if action == "clear":
        keep = body.get("keep")
        with _LOCK:
            rows = _load_pool()
            if keep is None or keep == "":
                kept: List[Dict[str, Any]] = []
            elif isinstance(keep, list):
                wanted = {str(value).strip() for value in keep if str(value).strip()}
                kept = [item for item in rows if _item_key(item) in wanted or item.get("vid") in wanted]
            else:
                number = _clamp_int(keep, 0, POOL_MAX, 0)
                kept = rows[:number]
            _save_pool(kept)
        return ok({"total": len(kept), "kept": len(kept)})
    return fail("action 只能是 add 或 clear", 400)


def handle_like(req: Any) -> Any:  # noqa: ANN001
    body = _payload(req)
    bvid = str(body.get("bvid") or "").strip()
    platform = str(body.get("platform") or ("bilibili" if bvid else "")).strip().lower()
    if platform not in PLATFORMS:
        return fail("platform 只能是 bilibili / douyin / kuaishou", 400)
    why = str(body.get("why") or "").strip()[:200]

    if platform != "bilibili":
        vid = str(body.get("vid") or body.get("bvid") or "").strip()
        if not vid:
            return fail("缺少视频 id", 400)
        in_pool = _mark_liked(platform, vid, why, True)
        note = "片单池里没有这条，只返回了结果" if not in_pool else "已在片单池记为喜欢"
        return ok({
            "platform": platform,
            "vid": vid,
            "liked": True,
            "real": False,
            "in_pool": in_pool,
            "message": f"{PLATFORMS[platform]}暂不支持自动真点赞；{note}",
        })

    if not _BVID_RE.match(bvid):
        vid = str(body.get("vid") or "").strip()
        if _BVID_RE.match(vid):
            bvid = vid
    if not _BVID_RE.match(bvid):
        return fail("bvid 不合法（形如 BV1xx411c7mD）", 400)
    cfg = _load_config()
    cookie = str(cfg.get("cookie") or "")
    match = re.search(r"bili_jct=([^;]+)", cookie)
    csrf = match.group(1).strip() if match else ""

    if not csrf:
        in_pool = _mark_liked("bilibili", bvid, why, True)
        note = "片单池里没有这条，只返回了结果" if not in_pool else "已在片单池记为喜欢"
        return ok({
            "platform": "bilibili",
            "bvid": bvid,
            "vid": bvid,
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
        except (TypeError, ValueError, _FeedError):
            aid = 0
        form: Dict[str, Any] = {"like": 1, "csrf": csrf}
        if aid:
            form["aid"] = aid
        else:
            form["bvid"] = bvid
        payload = _http_json(_BILI_LIKE, headers=headers, form=form, timeout=10.0)
    except _FeedError as exc:
        return fail(f"点赞失败：{exc}", 502)

    if not isinstance(payload, dict):
        return fail("点赞失败：接口返回结构异常", 502)
    code = payload.get("code")
    message = str(payload.get("message") or "").strip()
    if code == 0 or code == 65006 or "已经" in message:
        _mark_liked("bilibili", bvid, why, True)
        return ok({
            "platform": "bilibili",
            "bvid": bvid,
            "vid": bvid,
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
    """给 /health 用：片单池条数、标签、默认条数、各平台 cookie 是否已设置。"""
    try:
        cfg = _load_config()
        pool = _load_pool()
        cookies = {name: bool(_platform_cookie(name)) for name in PLATFORMS}
    except Exception:  # noqa: BLE001 - health 不允许把服务带崩
        return {"pool": 0, "tags": [], "count": 5, "cookie_set": False,
                "platforms": list(PLATFORMS), "cookies": {}}
    return {
        "pool": len(pool),
        "tags": list(cfg.get("tags") or []),
        "count": int(cfg.get("count") or 5),
        "cookie_set": bool(cfg.get("cookie")),
        "platforms": list(PLATFORMS),
        "cookies": cookies,
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
