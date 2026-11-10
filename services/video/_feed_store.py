"""feed_api 的持久化状态：feed.json 配置读写与 pool.json 片单池维护。

从 feed_api.py 按职责拆出（纯移动，逻辑与签名不变）；
锁使用 _feed_common._LOCK（与原文件同一把全局锁）。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from _feed_common import (
    DATA_DIR,
    PLATFORMS,
    POOL_MAX,
    _BVID_RE,
    _LOCK,
    _clamp_float,
    _clamp_int,
    _clean_title,
    _parse_duration,
    _read_json,
    _split_tags,
    _write_json,
)

FEED_FILE = DATA_DIR / "feed.json"
POOL_FILE = DATA_DIR / "pool.json"


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


def _item_url(platform: str, vid: str) -> str:
    if platform == "douyin":
        return f"https://www.douyin.com/video/{vid}"
    if platform == "kuaishou":
        return f"https://www.kuaishou.com/short-video/{vid}"
    return f"https://www.bilibili.com/video/{vid}"


def _item_key(item: Dict[str, Any]) -> str:
    """片单池去重键：平台 + 视频 id（兼容只有 bvid 的旧数据）。"""
    platform = str(item.get("platform") or "bilibili").strip() or "bilibili"
    vid = str(item.get("vid") or item.get("bvid") or "").strip()
    return f"{platform}:{vid}"


def _normalize_item(
    raw: Any, default_tag: str = "", platform: str = "bilibili"
) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    plat = str(raw.get("platform") or platform or "bilibili").strip().lower()
    if plat not in PLATFORMS:
        plat = "bilibili"
    try:
        picked_at = int(raw.get("picked_at") or time.time())
    except (TypeError, ValueError):
        picked_at = int(time.time())

    if plat == "bilibili":
        bvid = str(raw.get("bvid") or raw.get("vid") or "").strip()
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
        item: Dict[str, Any] = {
            "platform": "bilibili",
            "vid": bvid,
            "bvid": bvid,
            "title": title[:160],
            "author": str(raw.get("author") or owner.get("name") or "").strip()[:80],
            "duration": _parse_duration(raw.get("duration")),
            "play": max(0, play),
            "play_label": "播放",
            "url": _item_url("bilibili", bvid),
            "tag": str(default_tag or raw.get("tag") or "").strip()[:40],
            "picked_at": picked_at,
            "liked": bool(raw.get("liked")),
        }
    else:
        vid = str(raw.get("vid") or "").strip()
        title = _clean_title(raw.get("title"))
        if not vid or not title:
            return None
        try:
            play = int(raw.get("play") or 0)
        except (TypeError, ValueError):
            play = 0
        item = {
            "platform": plat,
            "vid": vid,
            "title": title[:160],
            "author": str(raw.get("author") or "").strip()[:80],
            "duration": _parse_duration(raw.get("duration")),
            "play": max(0, play),
            "play_label": "点赞" if plat == "douyin" else "播放",
            "url": str(raw.get("url") or "").strip() or _item_url(plat, vid),
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
    """按「平台:vid」去重、新条目在前、保留旧的 liked/picked_at，超上限裁最旧。"""
    with _LOCK:
        old = _load_pool()
        old_map = {_item_key(item): item for item in old}
        for item in items:
            prev = old_map.get(_item_key(item))
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
            key = _item_key(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= POOL_MAX:
                break
        return _save_pool(merged)


def _mark_liked(platform: str, vid: str, why: str, liked: bool) -> bool:
    """把片单池里的 liked 标记落盘；条目不在池里返回 False。"""
    key = f"{platform}:{vid}"
    with _LOCK:
        rows = _load_pool()
        found = False
        for item in rows:
            if _item_key(item) == key:
                item["liked"] = bool(liked)
                if why:
                    item["why"] = why
                found = True
                break
        if found:
            _save_pool(rows)
        return found
