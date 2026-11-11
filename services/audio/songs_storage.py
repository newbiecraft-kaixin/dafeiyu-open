"""songs（歌曲库）拆分：数据落盘与曲库聚合。

从 songs_api.py 按职责拆出；songs_api.py 保留为门面转发。
sources.json / songs.json 的读写、内置音源自愈、机器人（插件）曲库的
映射与合并、/health 用的 status 都在这里。_LOCK 是全局可变状态的唯一归属。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import astrbot_dir  # noqa: E402

from songs_common import (  # noqa: E402
    BUILTIN_LOGIN,
    BUILTIN_SOURCES,
    SONGS_FILE,
    SOURCES_FILE,
    _as_bool,
    _parse_duration,
    _read_json,
    _write_json,
)

# 可重入锁：handler 里会嵌套调用 _load_*（同样要拿锁）
_LOCK = threading.RLock()


# ---------------- 数据落盘 ----------------

def _load_sources() -> List[Dict[str, Any]]:
    with _LOCK:
        rows = _read_json(SOURCES_FILE, None)
        if not isinstance(rows, list):
            rows = []
        clean = [dict(row) for row in rows if isinstance(row, dict) and row.get("id")]
        ids = {str(row["id"]) for row in clean}
        changed = not SOURCES_FILE.exists()
        # 内置源缺失时自愈（用户手改文件删掉也能回到可用状态）
        for item in BUILTIN_SOURCES:
            if item["id"] not in ids:
                clean.append(dict(item))
                changed = True
        for row in clean:
            row["enabled"] = _as_bool(row.get("enabled"), True)
            row["type"] = str(row.get("type") or "remote")
            # 内置源的登录要求以代码为准，防止旧文件里缺字段
            row["login"] = BUILTIN_LOGIN.get(str(row["id"]), str(row.get("login") or "none"))
        # 卡片顺序跟内置声明走，自定义音源排在后面
        order = {item["id"]: index for index, item in enumerate(BUILTIN_SOURCES)}
        clean.sort(key=lambda row: order.get(str(row["id"]), len(order)))
        if changed or not SOURCES_FILE.exists():
            _write_json(SOURCES_FILE, clean)
        return clean


def _save_sources(rows: List[Dict[str, Any]]) -> None:
    _write_json(SOURCES_FILE, rows)


def _load_songs() -> List[Dict[str, Any]]:
    with _LOCK:
        rows = _read_json(SONGS_FILE, None)
        if not isinstance(rows, list):
            return []
        return [dict(row) for row in rows if isinstance(row, dict) and row.get("id")]


def _save_songs(rows: List[Dict[str, Any]]) -> None:
    _write_json(SONGS_FILE, rows)


def _bot_library_file() -> Optional[Path]:
    """机器人（basic_tools 插件）的曲库文件；中枢这边只读取展示，路径可用环境变量覆盖。"""
    env = os.environ.get("SONGS_BOT_LIBRARY", "").strip()
    if env:
        candidate = Path(env).expanduser()
        return candidate if candidate.is_file() else None
    try:
        bot = astrbot_dir()
    except Exception:  # noqa: BLE001 - 找目录失败不影响自己的 songs.json
        return None
    if not bot:
        return None
    candidate = (
        bot / "data" / "plugin_data" / "astrbot_plugin_basic_tools" / "music_library.json"
    )
    return candidate if candidate.is_file() else None


def _bot_songs() -> List[Dict[str, Any]]:
    """把插件曲库映射成中枢的歌曲结构（字段名不同：name/file → title/path）。"""
    path = _bot_library_file()
    if path is None:
        return []
    rows = _read_json(path, None)
    if not isinstance(rows, list):
        return []
    music_dir = path.parent / "music"
    songs: List[Dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        file_name = str(item.get("file") or "").strip()
        title = str(item.get("name") or "").strip() or Path(file_name).stem
        if not title:
            continue
        added = item.get("added")
        try:
            added_at = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(added)))
                if added
                else ""
            )
        except (TypeError, ValueError, OSError):
            added_at = ""
        songs.append(
            {
                "id": "bot:" + (file_name or title),
                "title": title,
                "artist": str(item.get("artist") or "").strip(),
                "album": "",
                "source": str(item.get("source") or "local").strip() or "local",
                "source_id": str(item.get("contentId") or ""),
                "path": str(music_dir / file_name) if file_name else "",
                "url": "",
                "duration": _parse_duration(item.get("duration")),
                "added_at": added_at,
                "origin": "bot",
            }
        )
    return songs


def _all_songs() -> List[Dict[str, Any]]:
    """中枢自己的曲库 + 机器人曲库（同名同歌手去重），只用于展示/试听。"""
    rows = _load_songs()
    seen = {
        (str(row.get("title") or "").lower(), str(row.get("artist") or "").lower())
        for row in rows
    }
    try:
        extra = _bot_songs()
    except Exception:  # noqa: BLE001 - 插件曲库坏了也不拖垮本服务
        extra = []
    for row in extra:
        key = (row["title"].lower(), row["artist"].lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def status() -> Dict[str, Any]:
    """给 /health 用：歌曲数 + 启用音源数。读盘失败也要返回结构。"""
    try:
        songs = _all_songs()
        sources = _load_sources()
    except Exception:  # noqa: BLE001 - health 不允许把服务带崩
        return {"songs": 0, "sources": 0, "sources_enabled": 0}
    enabled = [item for item in sources if _as_bool(item.get("enabled"), True)]
    return {
        "songs": len(songs),
        "sources": len(sources),
        "sources_enabled": len(enabled),
    }
