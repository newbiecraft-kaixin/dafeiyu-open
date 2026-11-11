"""翻唱管理：扫描 AstrBot 插件产出的 sing_/cover_ 成品并支持试听、清理。

成品目录（由 astrbot_plugin_basic_tools 写入）
  <dafeiyu>/astrbot-win/AstrBot-4.29.3/data/plugin_data/astrbot_plugin_basic_tools/voices
可用环境变量 DFY_COVER_DIR 覆盖。

路由
  GET    /covers          列表（name/kind/size/mtime，按时间倒序）
  GET    /covers/audio    试听（支持 Range）
  DELETE /covers          删除单个成品
  POST   /covers/open     在资源管理器中打开成品目录
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import Blob, astrbot_dir, fail, find_versioned_dir, ok  # noqa: E402

_ASTRBOT = (
    astrbot_dir(_HERE.parents[2])
    or find_versioned_dir(_HERE.parents[2] / "astrbot-win")
    or (_HERE.parents[2] / "astrbot-win" / "AstrBot")
)
PLUGIN_DATA = (_ASTRBOT / "data" / "plugin_data" / "astrbot_plugin_basic_tools")
COVER_DIR = Path(os.environ.get("DFY_COVER_DIR") or (PLUGIN_DATA / "voices"))
MUSIC_DIR = PLUGIN_DATA / "music"

# 只管理唱歌/翻唱成品，别误删同目录下的 voice_*.mp3（聊天语音）
_NAME_RE = re.compile(r"^(cover|sing)_(\d+)\.mp3$", re.IGNORECASE)
_MIME = "audio/mpeg"


def _items() -> List[Dict[str, Any]]:
    if not COVER_DIR.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for path in COVER_DIR.iterdir():
        match = _NAME_RE.match(path.name)
        if not match or not path.is_file():
            continue
        stat = path.stat()
        rows.append({
            "name": path.name,
            "kind": "cover" if match.group(1).lower() == "cover" else "sing",
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
            "stamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        })
    rows.sort(key=lambda row: row["mtime"], reverse=True)
    return rows


def _safe_path(name: str) -> Optional[Path]:
    if not _NAME_RE.match(name or ""):
        return None
    path = COVER_DIR / name
    if not path.is_file():
        return None
    return path


def _header(req, name: str) -> str:  # noqa: ANN001
    for key, value in (getattr(req, "headers", None) or {}).items():
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def _parse_range(size: int, header: str) -> Optional[Tuple[int, int]]:
    if not header or not header.strip().lower().startswith("bytes="):
        return None
    spec = header.strip()[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    left, _, right = spec.partition("-")
    try:
        if left:
            start = int(left)
            end = int(right) if right else size - 1
        else:
            suffix = int(right)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
    except ValueError:
        return None
    if start < 0 or start >= size:
        return None
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


def handle_covers(req) -> Any:  # noqa: ANN001
    rows = _items()
    return ok({
        "dir": str(COVER_DIR),
        "music_dir": str(MUSIC_DIR),
        "exists": COVER_DIR.is_dir(),
        "count": len(rows),
        "cover": sum(1 for row in rows if row["kind"] == "cover"),
        "sing": sum(1 for row in rows if row["kind"] == "sing"),
        "total_size": sum(row["size"] for row in rows),
        "items": rows,
    })


def handle_cover_audio(req) -> Any:  # noqa: ANN001
    path = _safe_path(str(req.q("name") or ""))
    if path is None:
        return fail("成品不存在", 404)
    size = path.stat().st_size
    rng = _parse_range(size, _header(req, "Range"))
    if rng and not (rng[0] == 0 and rng[1] == size - 1):
        with path.open("rb") as handle:
            handle.seek(rng[0])
            chunk = handle.read(rng[1] - rng[0] + 1)
        return 206, Blob(chunk, _MIME)
    return 200, Blob(path.read_bytes(), _MIME)


def handle_cover_delete(req) -> Any:  # noqa: ANN001
    name = str(req.q("name") or "").strip()
    path = _safe_path(name)
    if path is None:
        return fail("成品不存在", 404)
    try:
        path.unlink()
    except OSError as exc:
        return fail(f"删除失败：{exc}", 500)
    return ok({"deleted": name})


def handle_cover_open(_req) -> Any:  # noqa: ANN001
    if not COVER_DIR.is_dir():
        return fail(f"目录不存在：{COVER_DIR}", 404)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(COVER_DIR))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(COVER_DIR)])  # noqa: S603
    except Exception as exc:  # noqa: BLE001
        return fail(f"打不开目录：{exc}", 500)
    return ok({"dir": str(COVER_DIR)})


def status() -> Dict[str, Any]:
    rows = _items()
    return {
        "dir": str(COVER_DIR),
        "exists": COVER_DIR.is_dir(),
        "count": len(rows),
    }


routes: Dict[str, Any] = {
    "GET /covers": handle_covers,
    "GET /covers/audio": handle_cover_audio,
    "DELETE /covers": handle_cover_delete,
    "POST /covers/open": handle_cover_open,
}
