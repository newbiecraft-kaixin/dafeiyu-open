"""songs（歌曲库）拆分：HTTP handlers 与路由。

从 songs_api.py 按职责拆出；songs_api.py 保留为门面转发。
音源管理 / 搜索 / 曲库增删查 / 试听（含 Range 分段）的路由处理都在这里。
"""
from __future__ import annotations

import mimetypes
import sys
import time
import uuid
from html import escape as _escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import Blob, Html, fail, ok  # noqa: E402

import source_login  # noqa: E402

from songs_adapters import _ADAPTERS, _local_search  # noqa: E402
from songs_common import (  # noqa: E402
    BUILTIN_IDS,
    DEFAULT_LOCAL_DIR,
    _MIME,
    _as_bool,
    _header,
    _parse_duration,
    _payload,
    _read_json,
    _write_json,
)
from songs_storage import (  # noqa: E402
    _LOCK,
    _all_songs,
    _bot_library_file,
    _load_songs,
    _load_sources,
    _save_songs,
    _save_sources,
)


# ---------------- handlers：音源 ----------------

def _login_required(row: Dict[str, Any]) -> bool:
    return str(row.get("login") or "none") == "required"


def handle_sources_get(_req) -> Any:  # noqa: ANN001
    rows = _load_sources()
    payload = []
    for row in rows:
        item = dict(row)
        item["builtin"] = item["id"] in BUILTIN_IDS
        item["login_required"] = _login_required(item)
        item["logged_in"] = (not item["login_required"]) or source_login.has_login(str(item["id"]))
        payload.append(item)
    return ok(payload)


def handle_sources_post(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    source_id = str(body.get("id") or "").strip().lower()
    name = str(body.get("name") or "").strip()
    if not source_id:
        return fail("音源标识 id 不能为空", 400)
    if not name:
        return fail("音源名称不能为空", 400)
    source_type = str(body.get("type") or "remote").strip().lower()
    if source_type not in ("remote", "local"):
        return fail("音源类型只能是 remote（远程）或 local（本地）", 400)
    enabled = _as_bool(body.get("enabled"), True)

    with _LOCK:
        rows = _load_sources()
        existing: Optional[Dict[str, Any]] = None
        for row in rows:
            if str(row.get("id")) == source_id:
                existing = row
                break
        if existing is None:
            rows.append({"id": source_id, "name": name, "type": source_type, "enabled": enabled})
        else:
            existing["name"] = name
            existing["type"] = source_type
            existing["enabled"] = enabled
        _save_sources(rows)

    return ok({"id": source_id, "name": name, "type": source_type, "enabled": enabled})


def handle_sources_delete(req) -> Any:  # noqa: ANN001
    source_id = str(req.q("id") or "").strip().lower()
    if not source_id:
        return fail("缺少音源 id", 400)
    if source_id in BUILTIN_IDS:
        return fail("内置音源不允许删除（可以改为停用）", 400)
    with _LOCK:
        rows = _load_sources()
        kept = [row for row in rows if str(row.get("id")) != source_id]
        if len(kept) == len(rows):
            return fail(f"音源不存在：{source_id}", 404)
        _save_sources(kept)
    return ok({"deleted": source_id})


# ---------------- handlers：搜索 / 歌曲 ----------------

def handle_search(req) -> Any:  # noqa: ANN001
    source = str(req.q("source") or "kugou").strip().lower()
    keyword = str(req.q("q") or "").strip()

    sources = _load_sources()
    lookup = {str(row.get("id")): row for row in sources}
    config: Optional[Dict[str, Any]] = lookup.get(source)
    if source != "local":
        if config is None:
            return fail(f"未知音源：{source}", 400)
        if not _as_bool(config.get("enabled"), True):
            return fail(f"音源已停用：{config.get('name') or source}", 400)
        if _login_required(config) and not source_login.has_login(source):
            return fail(f"音源需要先扫码登录：{config.get('name') or source}", 403)

    if source == "local" or str((lookup.get(source) or {}).get("type") or "") == "local":
        try:
            items = _local_search(str(req.q("dir") or DEFAULT_LOCAL_DIR))
        except Exception as exc:  # noqa: BLE001 - 目录问题也要优雅提示
            return fail(f"本地扫描失败：{exc}", 400)
        return ok({"source": source, "items": items})

    if not keyword:
        return fail("缺少搜索关键词", 400)

    adapter = _ADAPTERS.get(source)
    if adapter is None:
        return fail(f"音源不可用：尚未适配 {source}，可直接用本地音频入库", 502)
    try:
        items = adapter(keyword, source_login.cookie_of(source))
    except Exception as exc:  # noqa: BLE001 - 断网/超时/结构变化一律 fail-soft
        return fail(f"音源不可用：{exc}", 502)
    return ok({"source": source, "items": items})


def handle_songs_get(req) -> Any:  # noqa: ANN001
    keyword = str(req.q("q") or "").strip().lower()
    rows = _all_songs()
    if keyword:
        rows = [
            row
            for row in rows
            if keyword
            in " ".join(
                str(row.get(field) or "") for field in ("title", "artist", "album")
            ).lower()
        ]
    rows = list(reversed(rows))  # 新入库的排前面
    return ok(rows)


def handle_songs_post(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    title = str(body.get("title") or "").strip()
    if not title:
        return fail("歌名不能为空", 400)
    artist = str(body.get("artist") or "").strip()
    album = str(body.get("album") or "").strip()
    source = str(body.get("source") or "").strip().lower()
    source_id = str(body.get("source_id") or "").strip()
    path_text = str(body.get("path") or "").strip()
    url_text = str(body.get("url") or "").strip()
    duration = _parse_duration(body.get("duration"))

    if path_text:
        audio_path = Path(path_text).expanduser()
        if not audio_path.is_file():
            return fail(f"音频文件不存在：{path_text}", 400)
        path_text = str(audio_path)
        source = source or "local"
    elif url_text:
        if not (url_text.startswith("http://") or url_text.startswith("https://")):
            return fail("url 必须是 http(s) 地址", 400)
        source = source or "remote"
    else:
        return fail("必须提供本地音频 path 或远程 url", 400)

    record = {
        "id": uuid.uuid4().hex[:16],
        "title": title,
        "artist": artist,
        "album": album,
        "source": source,
        "source_id": source_id,
        "path": path_text,
        "url": url_text if not path_text else "",
        "duration": duration,
        "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _LOCK:
        rows = _load_songs()
        rows.append(record)
        _save_songs(rows)
    return ok(record)


def handle_songs_delete(req) -> Any:  # noqa: ANN001
    song_id = str(req.q("id") or "").strip()
    if not song_id:
        return fail("缺少歌曲 id", 400)
    with _LOCK:
        rows = _load_songs()
        kept = [row for row in rows if str(row.get("id")) != song_id]
        if len(kept) == len(rows):
            if song_id.startswith("bot:"):
                return _delete_bot_song(song_id)
            return fail(f"歌曲不存在：{song_id}", 404)
        _save_songs(kept)
    # 只删记录，不删本地文件
    return ok({"deleted": song_id})


def _delete_bot_song(song_id: str) -> Any:
    """删机器人曲库里的歌（条目 + 音频文件），和 QQ 里 /听歌 移除 一致。"""
    path = _bot_library_file()
    if path is None:
        return fail("机器人曲库不可用", 404)
    keep, removed = [], []
    for item in _read_json(path, None) or []:
        if not isinstance(item, dict):
            continue
        file_name = str(item.get("file") or "")
        key = "bot:" + (file_name or str(item.get("name") or ""))
        (removed if key == song_id else keep).append(item)
    if not removed:
        return fail(f"歌曲不存在：{song_id}", 404)
    _write_json(path, keep)
    music_dir = path.parent / "music"
    for item in removed:
        file_name = str(item.get("file") or "")
        if not file_name:
            continue
        try:
            (music_dir / file_name).unlink()
        except OSError:
            pass
    return ok({"deleted": song_id, "removed": len(removed)})


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


def _serve_file(path: Path, range_header: str) -> Any:
    size = path.stat().st_size
    mime = _MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    rng = _parse_range(size, range_header)
    if rng and not (rng[0] == 0 and rng[1] == size - 1):
        with path.open("rb") as handle:
            handle.seek(rng[0])
            chunk = handle.read(rng[1] - rng[0] + 1)
        return 206, Blob(chunk, mime)
    return 200, Blob(path.read_bytes(), mime)


def handle_audio(req) -> Any:  # noqa: ANN001
    song_id = str(req.q("id") or "").strip()
    if not song_id:
        return fail("缺少歌曲 id", 400)
    song = next((row for row in _all_songs() if str(row.get("id")) == song_id), None)
    if song is None:
        return fail(f"歌曲不存在：{song_id}", 404)

    path_text = str(song.get("path") or "")
    if path_text:
        audio_path = Path(path_text)
        if not audio_path.is_file():
            return fail(f"音频文件不存在：{path_text}", 404)
        return _serve_file(audio_path, _header(req, "Range"))

    url_text = str(song.get("url") or "")
    if url_text.startswith("http://") or url_text.startswith("https://"):
        # infra 不开放自定义响应头，这里用跳转页实现「302 到远程地址」的效果
        safe = _escape(url_text, quote=True)
        return 302, Html(
            '<!doctype html><meta charset="utf-8">'
            f'<meta http-equiv="refresh" content="0;url={safe}">'
            '<p style="font:14px/1.7 system-ui;color:#bfd0ff;background:#070a14;padding:24px">'
            f'正在跳转到远程音频… <a style="color:#bfd0ff" href="{safe}">{safe}</a></p>'
        )
    return fail("该歌曲没有可试听的文件或链接", 404)


routes: Dict[str, Any] = {
    "GET /songs/sources": handle_sources_get,
    "POST /songs/sources": handle_sources_post,
    "DELETE /songs/sources": handle_sources_delete,
    "GET /songs/search": handle_search,
    "GET /songs": handle_songs_get,
    "POST /songs": handle_songs_post,
    "DELETE /songs": handle_songs_delete,
    "GET /songs/audio": handle_audio,
}
