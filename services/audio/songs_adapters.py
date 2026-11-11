"""songs（歌曲库）拆分：音源适配器。

从 songs_api.py 按职责拆出；songs_api.py 保留为门面转发。
酷狗 / QQ音乐 / 网易云 / 咪咕的搜索与播放地址解析、本地目录扫描都在这里。
所有第三方音源都 fail-soft：超时/断网/解析失败由 handler 层转成 fail(...)。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from html import unescape as _unescape
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from songs_common import (  # noqa: E402
    AUDIO_EXTS,
    DEFAULT_LOCAL_DIR,
    MAX_LOCAL_FILES,
    SEARCH_TIMEOUT,
    _UA,
    _parse_duration,
)


def _clean_text(value: Any) -> str:
    """搜索结果常带 <em> 高亮标签，去掉标签并还原实体。"""
    text = str(value or "")
    text = re.sub(r"<[^>]+>", "", text)
    return _unescape(text).strip()


def _http_json(url: str, headers: Dict[str, str], timeout: float = SEARCH_TIMEOUT) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8", "ignore"))


def _kugou_play_url(file_hash: str, deadline: float, cookie: str = "") -> str:
    """酷狗播放地址要二次接口；拿不到就返回空串，不算失败。"""
    if not file_hash or time.time() >= deadline:
        return ""
    url = "https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash=" + urllib.parse.quote(file_hash)
    remain = max(1.0, min(3.0, deadline - time.time()))
    headers = {"User-Agent": _UA, "Referer": "https://m.kugou.com/", "Accept": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        data = _http_json(url, headers, timeout=remain)
        if isinstance(data, dict):
            play = str(data.get("url") or "")
            return play if play.startswith("http") else ""
    except Exception:  # noqa: BLE001 - 二次接口失败不影响搜索本身
        return ""
    return ""


def _kugou_search(keyword: str, cookie: str = "") -> List[Dict[str, Any]]:
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
    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.kugou.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if cookie:
        headers["Cookie"] = cookie
    data = _http_json(url, headers)
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
        item["url"] = _kugou_play_url(str(item["source_id"]), deadline, cookie)
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


def _migu_search(keyword: str, cookie: str = "") -> List[Dict[str, Any]]:
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


def _qqmusic_play_url(song_mid: str, cookie: str, deadline: float) -> str:
    """QQ音乐取 vkey 换直链；登录态失效就返回空串（不阻断搜索）。"""
    if not song_mid or time.time() >= deadline:
        return ""
    uin = "0"
    match = re.search(r"(?:^|;\s*)uin=o?0*(\d+)", cookie or "")
    if match:
        uin = match.group(1)
    payload = {
        "req_0": {
            "module": "vkey.GetVkeyServer",
            "method": "CgiGetVkey",
            "param": {
                "guid": "3982823384",
                "songmid": [song_mid],
                "songtype": [0],
                "uin": uin,
                "loginflag": 1,
                "platform": "20",
            },
        },
        "comm": {"uin": uin, "format": "json", "ct": 24, "cv": 0},
    }
    request = urllib.request.Request(
        "https://u.y.qq.com/cgi-bin/musicu.fcg",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": _UA,
            "Referer": "https://y.qq.com/",
            "Content-Type": "application/json",
            "Cookie": cookie,
        },
    )
    remain = max(1.0, min(3.0, deadline - time.time()))
    try:
        with urllib.request.urlopen(request, timeout=remain) as response:
            data = json.loads(response.read().decode("utf-8", "ignore"))
        info = ((((data or {}).get("req_0") or {}).get("data") or {}).get("midurlinfo") or [])
        for row in info:
            purl = str((row or {}).get("purl") or "")
            if purl.startswith("/"):
                purl = "https://ws.stream.qqmusic.qq.com" + purl
            if purl.startswith("http"):
                return purl
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _qqmusic_search(keyword: str, cookie: str = "") -> List[Dict[str, Any]]:
    # 新版 client_search_cp 对未登录请求常返回空列表，这里用仍可用的老接口
    query = urllib.parse.urlencode(
        {
            "w": keyword,
            "format": "json",
            "p": 1,
            "n": 20,
            "cr": 1,
            "g_tk": 5381,
            "t": 0,
            "flag_qc": 0,
            "remoteplace": "txt.yqq.song",
        }
    )
    url = "https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp?" + query
    headers = {
        "User-Agent": _UA,
        "Referer": "https://y.qq.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if cookie:
        headers["Cookie"] = cookie
    data = _http_json(url, headers)
    rows = ((((data or {}).get("data") or {}).get("song") or {}).get("list") or [])
    if not isinstance(rows, list):
        raise ValueError("返回结构异常（未找到 data.song.list）")

    items: List[Dict[str, Any]] = []
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        title = _clean_text(row.get("songname") or row.get("title"))
        if not title:
            continue
        singers = row.get("singer")
        if isinstance(singers, list):
            artist = "、".join(
                _clean_text(song.get("name")) for song in singers
                if isinstance(song, dict) and song.get("name")
            )
        else:
            artist = _clean_text(row.get("singername"))
        items.append(
            {
                "title": title,
                "artist": artist,
                "album": _clean_text(row.get("albumname")),
                "url": "",
                "duration": _parse_duration(row.get("interval")),
                "source_id": str(row.get("songmid") or row.get("mid") or ""),
            }
        )
    deadline = time.time() + SEARCH_TIMEOUT
    for item in items[:5]:
        item["url"] = _qqmusic_play_url(str(item["source_id"]), cookie, deadline)
    return items


def _netease_play_url(song_id: str, cookie: str, deadline: float) -> str:
    if not song_id or time.time() >= deadline:
        return ""
    url = "https://music.163.com/api/song/enhance/player/url?" + urllib.parse.urlencode(
        {"ids": f"[{song_id}]", "br": 320000}
    )
    headers = {"User-Agent": _UA, "Referer": "https://music.163.com/"}
    if cookie:
        headers["Cookie"] = cookie
    remain = max(1.0, min(3.0, deadline - time.time()))
    try:
        data = _http_json(url, headers, timeout=remain)
        rows = (data or {}).get("data") if isinstance(data, dict) else None
        if isinstance(rows, list) and rows:
            play = str((rows[0] or {}).get("url") or "")
            return play if play.startswith("http") else ""
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _netease_search(keyword: str, cookie: str = "") -> List[Dict[str, Any]]:
    query = urllib.parse.urlencode(
        {"s": keyword, "type": 1, "offset": 0, "total": "true", "limit": 20}
    )
    url = "https://music.163.com/api/search/get/web?" + query
    headers = {
        "User-Agent": _UA,
        "Referer": "https://music.163.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if cookie:
        headers["Cookie"] = cookie
    data = _http_json(url, headers)
    rows = (((data or {}).get("result") or {}).get("songs") or [])
    if not isinstance(rows, list):
        raise ValueError("返回结构异常（未找到 result.songs）")

    items: List[Dict[str, Any]] = []
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        title = _clean_text(row.get("name"))
        if not title:
            continue
        artists = row.get("artists") or row.get("ar") or []
        if isinstance(artists, list):
            artist = "、".join(
                _clean_text(song.get("name")) for song in artists
                if isinstance(song, dict) and song.get("name")
            )
        else:
            artist = _clean_text(artists)
        album = row.get("album") or row.get("al") or ""
        if isinstance(album, dict):
            album = _clean_text(album.get("name"))
        else:
            album = _clean_text(album)
        duration = _parse_duration(row.get("duration") or row.get("dt"))
        if duration > 10000:  # 网易云给的是毫秒
            duration = duration // 1000
        items.append(
            {
                "title": title,
                "artist": artist,
                "album": album,
                "url": "",
                "duration": duration,
                "source_id": str(row.get("id") or ""),
            }
        )
    deadline = time.time() + SEARCH_TIMEOUT
    for item in items[:5]:
        item["url"] = _netease_play_url(str(item["source_id"]), cookie, deadline)
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
    "qqmusic": _qqmusic_search,
    "netease": _netease_search,
    "migu": _migu_search,
}
