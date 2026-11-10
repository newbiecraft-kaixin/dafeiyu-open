"""片单 / 偏好 / 关注词 / 常驻页面等落盘工具。

从 main.py 拆出来的纯文件操作，保持同名导出，main.py 直接 import 使用。
"""

from __future__ import annotations

import json
import os
import re
import time

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_NAME = "astrbot_plugin_web_lookup"


def _bili_pool_path() -> str:
    """片单池固定落在 data/plugin_data/<插件名>/bili_pool.json。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        root = get_astrbot_plugin_data_path()
    except Exception:  # noqa: BLE001
        root = os.path.join(os.path.dirname(os.path.dirname(PLUGIN_DIR)), "plugin_data")
    return os.path.join(root, PLUGIN_NAME, "bili_pool.json")


def _bili_qr_path() -> str:
    """二维码落到共享目录，按本地文件直接发出去。"""
    share = os.path.join(
        os.path.dirname(os.path.dirname(PLUGIN_DIR)), "stickers", "_share"
    )
    os.makedirs(share, exist_ok=True)
    for fn in os.listdir(share):
        if fn.startswith("bili_qr_"):
            try:
                os.remove(os.path.join(share, fn))
            except OSError:
                pass
    return os.path.join(share, f"bili_qr_{int(time.time())}.png")


def _bili_qr_url(path: str) -> str:
    """二维码直接用本地文件发（旧 8712 静态服务已下线，回环链接好友打不开）。"""
    return os.path.abspath(path)


def _video_prefs_path() -> str:
    """刷视频偏好落到 data/plugin_data/<插件名>/video_prefs.json。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        root = get_astrbot_plugin_data_path()
    except Exception:  # noqa: BLE001
        root = os.path.join(os.path.dirname(os.path.dirname(PLUGIN_DIR)), "plugin_data")
    return os.path.join(root, PLUGIN_NAME, "video_prefs.json")


def _load_video_prefs() -> dict:
    """读偏好关键词（最多 10 个，去重）。"""
    try:
        with open(_video_prefs_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    rows = data.get("keywords") if isinstance(data, dict) else []
    out: list[str] = []
    for k in rows if isinstance(rows, list) else []:
        k = re.sub(r"\s+", " ", str(k or "")).strip()[:20]
        if k and k not in out:
            out.append(k)
    return {"keywords": out[:10]}


def _save_video_prefs(prefs: dict) -> None:
    path = _video_prefs_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"keywords": prefs.get("keywords") or []}, f, ensure_ascii=False, indent=2)


def _split_keywords(text: str) -> list[str]:
    """把「搞笑 pvz，沙雕」拆成关键词列表。"""
    rows = re.split(r"[\s,，、;；]+", str(text or "").strip())
    return [x.strip()[:20] for x in rows if x.strip()][:10]


def _watch_path() -> str:
    """看视频关注词落到 data/plugin_data/<插件名>/watchlist.json。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        root = get_astrbot_plugin_data_path()
    except Exception:  # noqa: BLE001
        root = os.path.join(os.path.dirname(os.path.dirname(PLUGIN_DIR)), "plugin_data")
    return os.path.join(root, PLUGIN_NAME, "watchlist.json")


def _load_watch() -> list[dict]:
    try:
        with open(_watch_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = []
    out: list[dict] = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        kw = re.sub(r"\s+", " ", str(row.get("kw") or "")).strip()[:30]
        if kw:
            out.append({
                "kw": kw,
                "platform": str(row.get("platform") or ""),
                "at": str(row.get("at") or ""),
            })
    return out[:50]


def _save_watch(rows: list[dict]) -> None:
    path = _watch_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _share_page_dir() -> str:
    share = os.path.join(
        os.path.dirname(os.path.dirname(PLUGIN_DIR)), "stickers", "_share"
    )
    os.makedirs(share, exist_ok=True)
    return share


def _write_feed_page(html_text: str, public_url: str = "") -> str:
    """所有刷到的视频只写进同一个常驻页面，返回可发的 URL。

    public_url 配了就用对外地址（和充值页共用同一条公网端口）；
    没配返回空字符串，调用方不再发链接 —— 不能退回 127.0.0.1:8712（已下线）。
    """
    path = os.path.join(_share_page_dir(), "bili_feed.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text)
    return str(public_url or "").strip()


def _feed_page_path() -> str:
    """常驻页面的数据落盘：data/plugin_data/<插件名>/bili_page.json。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        root = get_astrbot_plugin_data_path()
    except Exception:  # noqa: BLE001
        root = os.path.join(os.path.dirname(os.path.dirname(PLUGIN_DIR)), "plugin_data")
    return os.path.join(root, PLUGIN_NAME, "bili_page.json")


_FEED_PAGE_LIMIT = 200


def _load_feed_page() -> dict:
    """读常驻页面数据（条目 + 标签 + 更新时间）。"""
    try:
        with open(_feed_page_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
    tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]
    return {
        "items": items[:_FEED_PAGE_LIMIT],
        "tags": tags[:10],
        "updated": str(data.get("updated") or ""),
    }


def _merge_feed_page(items: list[dict], tags: list[str] | None = None) -> dict:
    """把新条目并进常驻页面：按链接去重，新的在前，最多留 200 条。"""
    page = _load_feed_page()

    def key_of(it: dict) -> str:
        return str(it.get("url") or it.get("bvid") or it.get("title") or "").strip()

    seen = {key_of(it) for it in page["items"]}
    fresh: list[dict] = []
    for it in items:
        k = key_of(it)
        if not k or k in seen:
            continue
        seen.add(k)
        row = dict(it)
        if not row.get("ts"):
            row["ts"] = int(time.time())
        fresh.append(row)
    page["items"] = (fresh + page["items"])[:_FEED_PAGE_LIMIT]

    merged: list[str] = []
    for t in list(tags or []) + page["tags"]:
        t = str(t or "").strip()
        if t and t not in merged:
            merged.append(t)
    page["tags"] = merged[:10]
    page["updated"] = time.strftime("%Y-%m-%d %H:%M")

    path = _feed_page_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(page, f, ensure_ascii=False, indent=2)
    return page
