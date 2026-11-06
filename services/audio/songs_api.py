"""songs（歌曲库）：音源配置 / 搜索 / 入库 / 试听。

属于「音频能力域」的可选扩展：装了就能搜歌、入库、试听；
不装也不影响桌宠对话。由 service.py 装配到 23366 端口。

接口
  GET    /songs/sources        音源列表
  POST   /songs/sources        新增或更新音源（按 id upsert，可开关 enabled）
  DELETE /songs/sources?id=    删除自定义音源（内置三条不可删）
  GET    /songs/search         按音源搜索；source=local 时扫描本地目录
  GET    /songs                已入库歌曲（支持 ?q= 过滤）
  POST   /songs                入库（给 path 校验文件存在，给 url 记为远程）
  DELETE /songs?id=            删除记录（不动本地文件）
  GET    /songs/audio?id=      试听：本地文件按扩展名给 Content-Type，支持 Range；远程 url 跳转

所有第三方音源都 fail-soft：超时/断网/解析失败只返回 fail("音源不可用：...")，
绝不抛出异常导致服务 500。
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from html import escape as _escape
from html import unescape as _unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import Blob, Html, fail, ok  # noqa: E402

DATA_DIR = _HERE.parent / "data"
SOURCES_FILE = DATA_DIR / "sources.json"
SONGS_FILE = DATA_DIR / "songs.json"

# 本地音源默认扫描目录（玩家把音频丢这里即可搜到）
DEFAULT_LOCAL_DIR = r"C:\path\to\workspace\sing_ab\songs"

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
BUILTIN_SOURCES: List[Dict[str, Any]] = [
    {"id": "kugou", "name": "酷狗音乐", "type": "remote", "enabled": True},
    {"id": "migu", "name": "咪咕音乐", "type": "remote", "enabled": True},
    {"id": "local", "name": "本地音频", "type": "local", "enabled": True},
]
BUILTIN_IDS = {item["id"] for item in BUILTIN_SOURCES}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SEARCH_TIMEOUT = 8.0  # 秒
MAX_LOCAL_FILES = 500

# 可重入锁：handler 里会嵌套调用 _load_*（同样要拿锁）
_LOCK = threading.RLock()


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


def _clean_text(value: Any) -> str:
    """搜索结果常带 <em> 高亮标签，去掉标签并还原实体。"""
    text = str(value or "")
    text = re.sub(r"<[^>]+>", "", text)
    return _unescape(text).strip()


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


def status() -> Dict[str, Any]:
    """给 /health 用：歌曲数 + 启用音源数。读盘失败也要返回结构。"""
    try:
        songs = _load_songs()
        sources = _load_sources()
    except Exception:  # noqa: BLE001 - health 不允许把服务带崩
        return {"songs": 0, "sources": 0, "sources_enabled": 0}
    enabled = [item for item in sources if _as_bool(item.get("enabled"), True)]
    return {
        "songs": len(songs),
        "sources": len(sources),
        "sources_enabled": len(enabled),
    }


# ---------------- 音源适配器 ----------------

def _http_json(url: str, headers: Dict[str, str], timeout: float = SEARCH_TIMEOUT) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8", "ignore"))


def _kugou_play_url(file_hash: str, deadline: float) -> str:
    """酷狗播放地址要二次接口；拿不到就返回空串，不算失败。"""
    if not file_hash or time.time() >= deadline:
        return ""
    url = "https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash=" + urllib.parse.quote(file_hash)
    remain = max(1.0, min(3.0, deadline - time.time()))
    try:
        data = _http_json(
            url,
            {"User-Agent": _UA, "Referer": "https://m.kugou.com/", "Accept": "application/json"},
            timeout=remain,
        )
        if isinstance(data, dict):
            play = str(data.get("url") or "")
            return play if play.startswith("http") else ""
    except Exception:  # noqa: BLE001 - 二次接口失败不影响搜索本身
        return ""
    return ""


def _kugou_search(keyword: str) -> List[Dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "keyword": keyword,
            "platform": "WebFilter",
            "userid": "-1",
            "clientver": "2000",
            "iscorrection": "1",
            "privilege_filter": "0",
        }
    )
    url = "https://songsearch.kugou.com/song_search_v2?" + query
    data = _http_json(
        url,
        {
            "User-Agent": _UA,
            "Referer": "https://www.kugou.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    lists = ((data or {}).get("data") or {}).get("lists") if isinstance(data, dict) else None
    if not isinstance(lists, list):
        raise ValueError("返回结构异常（未找到 data.lists）")

    deadline = time.time() + SEARCH_TIMEOUT
    items: List[Dict[str, Any]] = []
    for row in lists[:20]:
        if not isinstance(row, dict):
            continue
        file_hash = str(row.get("FileHash") or row.get("EMixSongId") or "")
        title = _clean_text(row.get("SongName")) or _clean_text(row.get("FileName"))
        if not title:
            continue
        items.append(
            {
                "title": title,
                "artist": _clean_text(row.get("SingerName")),
                "album": _clean_text(row.get("AlbumName")),
                "url": "",
                "duration": _parse_duration(row.get("Duration")),
                "source_id": file_hash,
            }
        )
    # 只给前几条补播放地址，避免把 8 秒预算耗光
    for item in items[:5]:
        item["url"] = _kugou_play_url(str(item["source_id"]), deadline)
    return items


def _migu_rows(data: Any) -> List[Dict[str, Any]]:
    """咪咕返回层级偶有变化，做几种常见路径兼容。"""
    if not isinstance(data, dict):
        return []
    for key in ("songResultData", "data", "song"):
        node = data.get(key)
        if isinstance(node, dict):
            for sub in ("result", "songResult", "list"):
                rows = node.get(sub)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
    for sub in ("result", "songResult", "list"):
        rows = data.get(sub)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _migu_search(keyword: str) -> List[Dict[str, Any]]:
    switch = '{"song":1,"album":0,"singer":0,"tag":0}'
    query = urllib.parse.urlencode(
        {"text": keyword, "pageNo": 1, "pageSize": 20, "searchSwitch": switch}
    )
    url = "https://pd.musicapp.migu.cn/MIGUM2.0/v1.0/content/search_all.do?" + query
    data = _http_json(
        url,
        {
            "User-Agent": _UA,
            "Referer": "https://music.migu.cn/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    rows = _migu_rows(data)
    if not rows:
        raise ValueError("返回结构异常（未找到歌曲列表）")

    items: List[Dict[str, Any]] = []
    for row in rows[:20]:
        title = _clean_text(row.get("name") or row.get("songName") or row.get("title"))
        if not title:
            continue
        singers = row.get("singers") or row.get("singerList") or []
        if isinstance(singers, list):
            artist = "、".join(
                _clean_text(song.get("name"))
                for song in singers
                if isinstance(song, dict) and song.get("name")
            )
        else:
            artist = _clean_text(singers)
        albums = row.get("albums") or row.get("albumList") or []
        album = ""
        if isinstance(albums, list) and albums and isinstance(albums[0], dict):
            album = _clean_text(albums[0].get("name"))
        elif isinstance(albums, str):
            album = _clean_text(albums)
        play = ""
        for key in ("audioUrl", "listenUrl", "playUrl", "mp3Url", "url"):
            value = row.get(key)
            if isinstance(value, str) and value.startswith("http"):
                play = value
                break
        items.append(
            {
                "title": title,
                "artist": artist,
                "album": album,
                "url": play,
                "duration": _parse_duration(
                    row.get("duration") or row.get("length") or row.get("songDuration")
                ),
                "source_id": str(row.get("songId") or row.get("contentId") or row.get("id") or ""),
            }
        )
    return items


def _local_search(directory: str) -> List[Dict[str, Any]]:
    folder = Path(directory or DEFAULT_LOCAL_DIR).expanduser()
    if not folder.is_dir():
        raise ValueError(f"目录不存在：{folder}")
    items: List[Dict[str, Any]] = []
    for path in sorted(folder.rglob("*")):
        if len(items) >= MAX_LOCAL_FILES:
            break
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTS:
            continue
        items.append(
            {
                "title": path.stem,
                "artist": "",
                "album": path.parent.name if path.parent != folder else "",
                "url": "",
                "duration": 0,
                "source_id": str(path),
                "path": str(path),
            }
        )
    return items


# 已适配的远程音源；没适配的 id 会返回「音源不可用」而不是 500
_ADAPTERS = {
    "kugou": _kugou_search,
    "migu": _migu_search,
}


# ---------------- handlers：音源 ----------------

def handle_sources_get(_req) -> Any:  # noqa: ANN001
    rows = _load_sources()
    payload = []
    for row in rows:
        item = dict(row)
        item["builtin"] = item["id"] in BUILTIN_IDS
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
    if source != "local":
        config = lookup.get(source)
        if config is None:
            return fail(f"未知音源：{source}", 400)
        if not _as_bool(config.get("enabled"), True):
            return fail(f"音源已停用：{config.get('name') or source}", 400)

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
        items = adapter(keyword)
    except Exception as exc:  # noqa: BLE001 - 断网/超时/结构变化一律 fail-soft
        return fail(f"音源不可用：{exc}", 502)
    return ok({"source": source, "items": items})


def handle_songs_get(req) -> Any:  # noqa: ANN001
    keyword = str(req.q("q") or "").strip().lower()
    rows = _load_songs()
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
            return fail(f"歌曲不存在：{song_id}", 404)
        _save_songs(kept)
    # 只删记录，不删本地文件
    return ok({"deleted": song_id})


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
    song = next((row for row in _load_songs() if str(row.get("id")) == song_id), None)
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
