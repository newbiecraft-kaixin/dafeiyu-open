"""songs（歌曲库）拆分：常量与通用小工具。

从 songs_api.py 按职责拆出；songs_api.py 保留为门面转发。
这里只放「音源定义 / 数据文件路径 / JSON 落盘 / 字段解析」这类被多处共用的东西。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import find_dir  # noqa: E402

DATA_DIR = _HERE.parent / "data"
SOURCES_FILE = DATA_DIR / "sources.json"
SONGS_FILE = DATA_DIR / "songs.json"

# 本地音源默认扫描目录（玩家把音频丢这里即可搜到；SONGS_LOCAL_DIR 可覆盖）
DEFAULT_LOCAL_DIR = os.environ.get("SONGS_LOCAL_DIR") or str(
    (find_dir(["sing_ab"]) or (Path.home() / "dafeiyu" / "sing_ab")) / "songs"
)

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".ape"}

_MIME = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wma": "audio/x-ms-wma",
    ".ape": "audio/x-ape",
}

# 内置音源：不允许通过接口删除
# login=required 的音源必须先扫码登录（咪咕、本地音频不用）
BUILTIN_SOURCES: List[Dict[str, Any]] = [
    {"id": "kugou", "name": "酷狗音乐", "type": "remote", "enabled": True, "login": "required"},
    {"id": "qqmusic", "name": "QQ音乐", "type": "remote", "enabled": True, "login": "required"},
    {"id": "netease", "name": "网易云音乐", "type": "remote", "enabled": True, "login": "required"},
    {"id": "migu", "name": "咪咕音乐", "type": "remote", "enabled": True, "login": "none"},
    {"id": "local", "name": "本地音频", "type": "local", "enabled": True, "login": "none"},
]
BUILTIN_LOGIN = {item["id"]: item.get("login", "none") for item in BUILTIN_SOURCES}
BUILTIN_IDS = {item["id"] for item in BUILTIN_SOURCES}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SEARCH_TIMEOUT = 8.0  # 秒
MAX_LOCAL_FILES = 500


# ---------------- 通用小工具 ----------------

def _payload(req) -> Dict[str, Any]:  # noqa: ANN001
    return req.payload if isinstance(req.payload, dict) else {}


def _header(req, name: str) -> str:  # noqa: ANN001
    """http.server 保留原始大小写，这里做一次忽略大小写的取值。"""
    for key, value in (getattr(req, "headers", None) or {}).items():
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("0", "false", "off", "no", "否", "")


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


def _parse_duration(value: Any) -> int:
    """把秒数 / "03:21" / "1:02:03" / "205.5" 统一成整数秒。"""
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
