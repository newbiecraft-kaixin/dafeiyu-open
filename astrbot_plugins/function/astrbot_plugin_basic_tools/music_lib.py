"""乐曲资源库：本地索引 + 酷狗/咪咕取直链 + 高潮片段裁剪。

默认从酷狗扒（有会员 Cookie 就能拿 VIP 曲），酷狗搜不到时退回咪咕。
只把「高潮片段」存进资源库，翻唱用它又快又省显存。
"""
import json
import hashlib
import logging
import os
import re
import time

import numpy as np
import requests
from pydub import AudioSegment

logger = logging.getLogger("music_lib")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
        "Origin": "https://h5.nf.migu.cn", "Referer": "https://h5.nf.migu.cn/",
        "ua": "Android_migu", "version": "6.8.8", "channel": "014021I",
        "subchannel": "014021I"}
SEARCH_URL = "https://c.musicapp.migu.cn/v1.0/content/search_all.do"
LISTEN_URL = "https://c.musicapp.migu.cn/strategy/listen-url/h5/v2.4"
KUGOU_SEARCH = "https://songsearch.kugou.com/song_search_v2"
KUGOU_COMPLEX_SEARCH = "https://complexsearch.kugou.com/v2/search/song"
KUGOU_SONGINFO = "https://wwwapi.kugou.com/play/songinfo"
KUGOU_MOBILE = "https://m.kugou.com/app/i/getSongInfo.php"
KUGOU_PLAY = "https://wwwapi.kugou.com/yy/index.php"
NETEASE_SEARCH = "https://music.163.com/api/search/get"
NETEASE_PLAY = "https://music.163.com/api/song/enhance/player/url"
NETEASE_HEADERS = {"User-Agent": UA, "Referer": "https://music.163.com/"}
# 酷狗 Web 端接口签名用的固定盐（所有准官方接口共用）
KUGOU_SECRET = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
MAGIC = b"\xab\xcd\x01"
KEY = b"Jk8qzuePiJ1qE3mDYhLQ3T73DtDoAhLP"
EXTS = (".mp3", ".m4a", ".flac", ".wav", ".ogg")


def _safe(name: str, limit: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name or "")).strip(" ._")
    return name[:limit] or "song"


def _decode(resp: requests.Response) -> dict:
    raw = resp.content
    if resp.headers.get("signature") == "1" or raw.startswith(MAGIC):
        seed = raw[3]
        plain = bytes((b + seed - KEY[i % len(KEY)]) & 0xFF
                      for i, b in enumerate(raw[4:]))
        return json.loads(plain.decode("utf-8"))
    return json.loads(raw.decode("utf-8"))


def _kugou_headers(cookie: str = "") -> dict:
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
         "Referer": "https://www.kugou.com/"}
    if cookie:
        h["Cookie"] = cookie
    return h


def _cookie_value(cookie: str, key: str) -> str:
    for part in str(cookie or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == key:
            return v.strip()
    return ""


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


def _kugou_sign(params: dict) -> str:
    """酷狗签名：盐 + 按参数名排序的 k=v 串 + 盐，取 md5。"""
    raw = KUGOU_SECRET + "".join(
        f"{k}={params[k]}" for k in sorted(params)) + KUGOU_SECRET
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _kugou_ctx(cookie: str) -> dict:
    """从登录 Cookie 里取签名要用的 mid / dfid / userid / token。"""
    mid = _cookie_value(cookie, "kg_mid") or _cookie_value(cookie, "mid")
    if not mid:
        mid = hashlib.md5(str(cookie or "dfy-kugou").encode("utf-8")).hexdigest()
    return {
        "mid": mid,
        "dfid": _cookie_value(cookie, "kg_dfid") or _cookie_value(cookie, "dfid") or "-",
        "userid": _cookie_value(cookie, "KugooID") or "0",
        "token": _cookie_value(cookie, "t") or "",
    }


def search_kugou(keyword: str, limit: int = 8, cookie: str = "") -> list[dict]:
    """酷狗搜索：歌名 → 候选（含 EMixSongID / hash / 时长）。"""
    ctx = _kugou_ctx(cookie)
    params = {
        "appid": "1014", "bitrate": "0", "callback": "callback123",
        "clienttime": str(int(time.time() * 1000)), "clientver": "1000",
        "dfid": ctx["dfid"], "filter": "10", "inputtype": "0",
        "iscorrection": "1", "isfuzzy": "0", "keyword": keyword,
        "mid": ctx["mid"], "page": "1", "pagesize": str(max(3, limit)),
        "platform": "WebFilter", "privilege_filter": "0", "srcappid": "2919",
        "token": ctx["token"], "userid": ctx["userid"], "uuid": ctx["mid"],
    }
    params["signature"] = _kugou_sign(params)
    r = requests.get(KUGOU_COMPLEX_SEARCH, headers=_kugou_headers(cookie),
                     params=params, timeout=20)
    r.raise_for_status()
    body = r.text.strip()
    if body.startswith("callback123("):
        body = body[len("callback123("):].rstrip(")")
    lists = ((json.loads(body).get("data") or {}).get("lists") or [])[:limit]
    out = []
    for s in lists:
        if not isinstance(s, dict) or not s.get("EMixSongID"):
            continue
        out.append({
            "name": _plain(s.get("SongName")),
            "artist": _plain(s.get("SingerName")),
            "hash": s.get("FileHash") or "",
            "eid": s["EMixSongID"],
            "album_id": s.get("AlbumID") or "",
            "duration": float(s.get("Duration") or 0),
            "source": "kugou",
        })
    return out


def _search_migu(keyword: str, limit: int = 8) -> list[dict]:
    """咪咕搜索接口：歌名 → 候选（含 contentId / copyrightId / 时长）。"""
    r = requests.get(
        SEARCH_URL, headers=HDRS, timeout=20,
        params={"text": keyword, "pageNo": 1, "pageSize": max(3, limit),
                "isCopyright": 1, "sort": 1,
                "searchSwitch": json.dumps({"song": 1, "album": 0, "singer": 0,
                                            "tagSong": 1, "mvSong": 0,
                                            "bestShow": 1})})
    r.raise_for_status()
    data = _decode(r)
    songs = (data.get("songResultData") or {}).get("result") or []
    out = []
    for s in songs[:limit]:
        if not isinstance(s, dict) or not s.get("contentId"):
            continue
        out.append({
            "name": str(s.get("name") or "").strip(),
            "artist": "、".join(a.get("name") for a in (s.get("singers") or [])
                               if isinstance(a, dict) and a.get("name")),
            "contentId": s["contentId"],
            "copyrightId": s.get("copyrightId") or "",
            "duration": float(s.get("duration") or 0) / 1000.0
            if s.get("duration") else 0.0,
            "source": "migu",
            "formats": [f.get("formatType") for f in (s.get("rateFormats") or [])
                        if isinstance(f, dict)],
        })
    return out


def search_online(keyword: str, limit: int = 8, kugou_cookie: str = "") -> list[dict]:
    """先搜酷狗（有 Cookie 时含 VIP 曲），搜不到再退回咪咕。"""
    try:
        songs = search_kugou(keyword, limit, kugou_cookie)
    except Exception:  # noqa: BLE001
        songs = []
    return songs or _search_migu(keyword, limit)


def _kugou_stream_url(song: dict, cookie: str = "") -> str:
    """酷狗直链：新版 play/songinfo（签名 + 登录态），失败再退回旧接口。"""
    h = _kugou_headers(cookie)
    eid = song.get("eid") or ""
    if eid:
        ctx = _kugou_ctx(cookie)
        params = {
            "srcappid": "2919", "clientver": "20000",
            "clienttime": str(int(time.time() * 1000)),
            "mid": ctx["mid"], "uuid": ctx["mid"], "dfid": ctx["dfid"],
            "appid": "1014", "platid": "4", "encode_album_audio_id": eid,
            "token": ctx["token"], "userid": ctx["userid"],
        }
        params["signature"] = _kugou_sign(params)
        try:
            d = requests.get(KUGOU_SONGINFO, headers=h, params=params,
                             timeout=20).json()
            data = d.get("data") or {}
            for u in (data.get("play_url"), data.get("play_backup_url"),
                      data.get("play_full_url")):
                if isinstance(u, str) and u.startswith("http"):
                    return u
        except Exception:  # noqa: BLE001
            pass
    if not song.get("hash"):
        return ""
    try:
        d = requests.get(
            KUGOU_MOBILE, headers=h, timeout=20,
            params={"cmd": "playInfo", "hash": song["hash"]},
        ).json()
        for u in [d.get("url")] + list(d.get("backup_url") or []):
            if isinstance(u, str) and u.startswith("http"):
                return u
    except Exception:  # noqa: BLE001
        pass
    ctx = _kugou_ctx(cookie)
    try:
        d = requests.get(
            KUGOU_PLAY, headers=h, timeout=20,
            params={"r": "play/getdata", "hash": song["hash"],
                    "album_id": song.get("album_id") or "", "mid": ctx["mid"],
                    "dfid": ctx["dfid"], "platid": 4, "appid": 1014},
        ).json()
    except Exception:  # noqa: BLE001
        return ""
    data = d.get("data") or {}
    for u in (data.get("play_url"), data.get("url"), data.get("play_backup_url")):
        if isinstance(u, str) and u.startswith("http"):
            return u
    return ""


def _netease_search(keyword: str, limit: int = 8) -> list[dict]:
    """网易云搜索：未登录也能拿到部分免费歌的 320k 直链（VIP 歌不给 url）。"""
    try:
        r = requests.get(
            NETEASE_SEARCH, headers=NETEASE_HEADERS, timeout=20,
            params={"s": keyword, "type": 1, "offset": 0,
                    "total": "true", "limit": max(3, limit)})
        r.raise_for_status()
        songs = (r.json().get("result") or {}).get("songs") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for s in songs[:limit]:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        out.append({
            "name": str(s.get("name") or "").strip(),
            "artist": "、".join(a.get("name") for a in (s.get("artists") or [])
                               if isinstance(a, dict) and a.get("name")),
            "song_id": s["id"],
            "duration": float(s.get("duration") or 0) / 1000.0
            if s.get("duration") else 0.0,
            "source": "netease",
        })
    return out


def _netease_stream_url(song: dict) -> str:
    """网易云直链：br=320000 按最高要，版权/VIP 受限返回空。"""
    sid = song.get("song_id") or ""
    if not sid:
        return ""
    try:
        r = requests.get(NETEASE_PLAY, headers=NETEASE_HEADERS, timeout=20,
                         params={"ids": f"[{sid}]", "br": 320000})
        for item in (r.json().get("data") or []):
            u = (item or {}).get("url") or ""
            if isinstance(u, str) and u.startswith("http"):
                return u
    except Exception:  # noqa: BLE001
        pass
    return ""


def _stream_url(song: dict, kugou_cookie: str = "") -> str:
    """按来源取可下载直链；版权受限会返回空。"""
    if song.get("source") == "kugou":
        return _kugou_stream_url(song, kugou_cookie)
    if song.get("source") == "netease":
        return _netease_stream_url(song)
    h = dict(HDRS)
    h.update({"Content-Type": "application/json;charset=UTF-8",
              "birth": "h5page", "signature": "1"})
    # HQ（320k）优先：PQ 只有 128k，翻唱出来又闷又糊
    for rt, tone in (("2", "HQ"), ("2", "PQ"), ("3", "LQ")):
        params = [("contentId", song["contentId"]),
                  ("copyrightId", song["copyrightId"]),
                  ("resourceType", rt), ("netType", "01"),
                  ("toneFlag", tone), ("scene", ""),
                  ("lowerQualityContentId", song["contentId"])]
        try:
            r = requests.get(LISTEN_URL, params=params, headers=h, timeout=8)
            d = _decode(r)
        except Exception:  # noqa: BLE001
            continue
        url = (d.get("data") or {}).get("url") or ""
        if url.startswith("http"):
            return url
    return ""


def _hook_range(seg: AudioSegment, hook_secs: int) -> tuple[float, float]:
    """找最响的一段连续区间（高潮），避开开头结尾各 4 秒。"""
    total = len(seg) / 1000.0
    if total <= hook_secs + 9:
        return 0.0, total
    mono = seg.set_channels(1).set_frame_rate(8000)
    a = np.array(mono.get_array_of_samples(), dtype=np.float64)
    if a.size == 0:
        return 0.0, min(total, hook_secs)
    a /= float(1 << (8 * mono.sample_width - 1))
    step = 8000  # 1 秒
    frames = a[: len(a) // step * step].reshape(-1, step)
    e = np.sqrt(np.mean(frames ** 2, axis=1))
    win = max(1, int(hook_secs))
    if len(e) <= win:
        return 0.0, total
    csum = np.concatenate([[0.0], np.cumsum(e)])
    lo, hi = 4, len(e) - win - 4
    if hi <= lo:
        lo, hi = 0, len(e) - win
    scores = csum[lo + win:hi + win] - csum[lo:hi]
    i = lo + int(np.argmax(scores))
    return float(i), float(min(hook_secs, total - i))


def _library_load(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _library_save(path: str, items: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def duration_of(path: str) -> float:
    """音频时长（秒），解码失败返回 0。"""
    try:
        return len(AudioSegment.from_file(path)) / 1000.0
    except Exception:  # noqa: BLE001
        return 0.0


def library_list(lib_path: str) -> list[dict]:
    """资源库全部条目（/听歌 列表用）。"""
    return _library_load(lib_path)


def library_hits(lib_path: str, keyword: str, limit: int = 5) -> list[dict]:
    kw = str(keyword or "").strip().lower()
    if not kw:
        return []
    items = _library_load(lib_path)
    hits = [it for it in items
            if kw in str(it.get("name") or "").lower()
            or kw in str(it.get("artist") or "").lower()
            or kw in str(it.get("file") or "").lower()]
    return hits[:limit]


def library_remove(lib_path: str, keyword: str) -> list[dict]:
    """按歌名/歌手/文件名从资源库删条目（文件由调用方删），返回被删掉的条目。"""
    kw = _norm_name(keyword)
    if not kw:
        return []
    keep, removed = [], []
    for it in _library_load(lib_path):
        hay = _norm_name(" ".join(
            str(it.get(k) or "") for k in ("name", "artist", "file")))
        (removed if kw in hay else keep).append(it)
    if removed:
        _library_save(lib_path, keep)
    return removed


def _norm_name(text: str) -> str:
    return re.sub(r"[\s\-_·、,，.。'\"“”《》()（）\[\]]+", "", _plain(text)).lower()


def _base_name(text: str) -> str:
    """去掉括号里的副标题：咪咕爱写「孤勇者(《双城之战》主题曲)」，酷狗只写「孤勇者」。"""
    return _norm_name(re.split(r"[（(\[【]", _plain(text))[0])


def _is_preview(actual: float, expected: float) -> bool:
    """实际时长明显短于搜索结果里的时长 = 只下到了试听片段。

    酷狗未登录 / 无版权时常返回 60 秒试听流，接口看着是成功、文件也能解码，
    只能靠时长比对识别：有参考时长就按 70% 卡，没参考时长则「正好 60 秒」视为试听。
    """
    if actual <= 0:
        return False
    if expected > 0:
        return expected >= 90 and actual < expected * 0.7
    return 55.0 <= actual <= 65.0


# 音源高频带宽闸门：低于这个滚降频率基本是 128k 级源，翻唱出来会闷糊。
# 与 cover_render.SOURCE_ROLLOFF_MIN 同值，那里是最后一道防线，这里是提前换源。
ROLLOFF_MIN = 13500.0


def _rolloff_hz(seg: AudioSegment) -> float:
    """取中段 30 秒算累计能量 99.9% 处的频率，量音源码率等级。

    实测：128k 源滚降 ~13.4kHz，320k 源 15kHz+；解不开/太短返回 0（跳过判断）。
    """
    try:
        total = len(seg) / 1000.0
        span = min(30.0, total)
        if span < 10:
            return 0.0
        start = max(0.0, (total - span) / 2.0)
        clip = seg[int(start * 1000):int((start + span) * 1000)].set_channels(1)
        sr = clip.frame_rate
        a = np.array(clip.get_array_of_samples(), dtype=np.float64)
        if a.size < sr:
            return 0.0
        a /= float(1 << (8 * clip.sample_width - 1))
        spec = np.abs(np.fft.rfft(a)) ** 2
        freqs = np.fft.rfftfreq(a.size, 1.0 / sr)
        csum = np.cumsum(spec)
        idx = int(np.searchsorted(csum, csum[-1] * 0.999))
        return float(freqs[min(idx, freqs.size - 1)])
    except Exception:  # noqa: BLE001
        return 0.0


def _match_alt(song: dict, cands: list[dict]) -> dict | None:
    """在候选里找同一首：歌名一致、时长相差 10 秒内、歌手互含。"""
    want = _base_name(song.get("name") or "")
    if not want:
        return None
    want_artist = _base_name(song.get("artist") or "")
    dur = float(song.get("duration") or 0)
    for c in cands:
        got = _base_name(c.get("name") or "")
        if not got or not (got == want or got.startswith(want)):
            continue
        cd = float(c.get("duration") or 0)
        if dur and cd and abs(cd - dur) > 10:
            continue
        artist = _base_name(c.get("artist") or "")
        if want_artist and artist and want_artist not in artist and artist not in want_artist:
            continue
        return c
    return None


def _migu_match(song: dict) -> dict | None:
    """在咪咕找同一首，用于酷狗质量差/只给试听时换源。"""
    keyword = f"{song.get('name') or ''} {song.get('artist') or ''}".strip()
    if not keyword:
        return None
    try:
        return _match_alt(song, _search_migu(keyword, 8))
    except Exception:  # noqa: BLE001
        return None


def _netease_match(song: dict) -> dict | None:
    """在网易云找同一首（免费歌未登录也有 320k 直链）。"""
    keyword = f"{song.get('name') or ''} {song.get('artist') or ''}".strip()
    if not keyword:
        return None
    return _match_alt(song, _netease_search(keyword, 8))


def is_preview_entry(lib_path: str, filename: str) -> bool:
    """库里这条是不是当年只下到试听片段的坏条目。

    新下载的条目带 checked 标记（已核对过整首），直接放行；
    旧条目没存参考时长，只能按特征猜：酷狗来源 + 时长短 + 高潮不从 0 秒开始。
    """
    for it in _library_load(lib_path):
        if str(it.get("file")) != filename:
            continue
        if it.get("checked"):
            return False
        dur = float(it.get("duration") or 0)
        start = float(it.get("hook_start") or 0)
        return (it.get("source") == "kugou" and 0 < dur <= 130 and start > 0)
    return False


def _download_audio(song: dict, music_dir: str,
                    kugou_cookie: str = "") -> tuple[AudioSegment | None, str]:
    """取直链并下载解码；失败返回 (None, 原因)。"""
    url = _stream_url(song, kugou_cookie)
    if not url:
        src = "酷狗" if song.get("source") == "kugou" else "咪咕"
        return None, f"该歌曲版权受限，{src}不给直链"
    os.makedirs(music_dir, exist_ok=True)
    tmp = os.path.join(music_dir, f".dl_{int(time.time() * 1000)}.bin")
    try:
        with requests.get(url, headers={"User-Agent": UA}, timeout=30,
                          stream=True) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    f.write(chunk)
        return AudioSegment.from_file(tmp), ""
    except Exception as e:  # noqa: BLE001
        return None, f"下载/解码失败：{type(e).__name__}"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def fetch_to_library(music_dir: str, lib_path: str, song: dict,
                     hook_secs: int = 45, kugou_cookie: str = "") -> dict:
    """下载整首歌进资源库，并记下高潮位置（翻唱整首再截高潮，音质不打折）。

    会核对实际时长：只拿到试听片段时，酷狗音源自动换咪咕同一首重下；
    换源也拿不到整首就如实报错，不把残片写进曲库。
    """
    chosen = song
    expected = float(chosen.get("duration") or 0)
    full, err = _download_audio(chosen, music_dir, kugou_cookie)
    actual = len(full) / 1000.0 if full is not None else 0.0
    # 酷狗两种失败都换源：没直链（版权受限）或只给了试听片段
    if chosen.get("source") == "kugou" and (full is None or _is_preview(actual, expected)):
        alt = _migu_match(chosen)
        if alt is not None:
            got, _ = _download_audio(alt, music_dir)
            alt_expected = float(alt.get("duration") or expected)
            if got is not None and not _is_preview(len(got) / 1000.0, alt_expected):
                alt = dict(alt)
                alt["name"] = chosen.get("name") or alt.get("name") or ""
                full, chosen, err = got, alt, ""
                expected = alt_expected
                actual = len(got) / 1000.0
    # 酷狗默认给的就是 128k 级源（高频滚降 ~13.4k），整首拿到了翻唱出来照样闷糊：
    # 量一下带宽，不达标就在咪咕/网易云里找同一首重下，哪个源高频更足用哪个。
    if full is not None and chosen.get("source") == "kugou":
        rolloff = _rolloff_hz(full)
        if 0.0 < rolloff < ROLLOFF_MIN:
            best, best_seg, best_rolloff = None, None, rolloff
            for alt in filter(None, (_migu_match(chosen), _netease_match(chosen))):
                got, _ = _download_audio(alt, music_dir)
                alt_expected = float(alt.get("duration") or expected)
                if got is None or _is_preview(len(got) / 1000.0, alt_expected):
                    continue
                r = _rolloff_hz(got)
                if r > best_rolloff:
                    best, best_seg, best_rolloff = alt, got, r
            if best is not None:
                best = dict(best)
                best["name"] = chosen.get("name") or best.get("name") or ""
                full, chosen = best_seg, best
                expected = float(chosen.get("duration") or expected)
                actual = len(full) / 1000.0
                rolloff = best_rolloff
                logger.warning(
                    f"[music_lib] {chosen.get('name')} 酷狗源高频只到 "
                    f"{rolloff / 1000:.1f}kHz（128k 级），已换 "
                    f"{chosen.get('source')} 源")
            else:
                logger.warning(
                    f"[music_lib] {chosen.get('name')} 酷狗源高频只到 "
                    f"{rolloff / 1000:.1f}kHz（128k 级），"
                    "咪咕/网易云也没有更好的，保留酷狗源")
    if full is None:
        return {"ok": False, "error": err}
    if _is_preview(actual, expected):
        return {"ok": False,
                "error": f"只拿到 {actual:.0f} 秒试听片段（"
                         f"{chosen.get('source') or '音源'}没给整首），换首歌试试"}
    start, dur = _hook_range(full, hook_secs)
    fname = _safe(f"{chosen['name']}-{chosen.get('artist') or ''}") + ".mp3"
    out = os.path.join(music_dir, fname)
    full.export(out, format="mp3", bitrate="160k")
    # 把音源带宽记进条目：翻唱时据此判断是不是已是能扒到的最好源
    src_rolloff = round(_rolloff_hz(full))
    items = [it for it in _library_load(lib_path)
             if str(it.get("file")) != fname]
    items.append({
        "file": fname, "name": chosen["name"], "artist": chosen.get("artist") or "",
        "source": chosen.get("source") or "migu",
        "hash": chosen.get("hash"), "contentId": chosen.get("contentId"),
        "duration": round(actual, 1),
        "hook_start": round(start, 1), "hook_len": round(dur, 1),
        "rolloff": src_rolloff,
        "checked": True,
        "added": int(time.time()),
    })
    _library_save(lib_path, items)
    return {"ok": True, "file": out, "name": chosen["name"],
            "artist": chosen.get("artist") or "", "duration": actual,
            "hook_start": start, "hook_len": dur,
            "switched_source": chosen is not song}


def slice_hook(src: str, out: str, start: float | None = None,
               secs: int = 45) -> dict:
    """从整首歌里截高潮段（start 为空就现算），导出成独立 mp3。"""
    seg = AudioSegment.from_file(src)
    total = len(seg) / 1000.0
    if start is None:
        start, _ = _hook_range(seg, secs)
    start = max(0.0, min(float(start), total - 1.0))
    dur = min(float(secs), total - start)
    clip = seg[int(start * 1000):int((start + dur) * 1000)]
    clip.export(out, format="mp3", bitrate="192k")
    return {"ok": True, "start": start, "duration": len(clip) / 1000.0}
