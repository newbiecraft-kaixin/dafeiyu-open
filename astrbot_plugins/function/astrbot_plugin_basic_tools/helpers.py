"""basic_tools 的常量与无状态工具函数（从 main.py 拆出）。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Face, Image, Plain, Record
from astrbot.api.star import Context, Star, register


PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))  # .../data
STICKER_MAP = os.path.join(DATA_ROOT, "stickers", "map.txt")
STICKER_ROOT = os.path.join(DATA_ROOT, "stickers")
REMINDERS_PATH = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "reminders.json"
)
# 定时任务：某个时间段内做 N 次「抓取路径 → AI 回答 → 发回会话」
SCHED_TASKS_PATH = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "scheduled_tasks.json"
)
LIMITS_PATH = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "daily_limits.json"
)
VOICE_DIR = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "voices"
)
# 唱歌音色：主人点「晓波」=聊天语音系统同款本地 CosyVoice2 克隆声线（voice="cosy"）
SING_VOICE = "cosy"
MUSIC_DIR = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "music"
)
# 乐曲资源库索引（music 目录里的高潮片段 + 来源信息）
MUSIC_LIB = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "music_library.json"
)
MUSIC_API = "https://music.163.com/api/search/get/web"
MUSIC_OUTER = "https://music.163.com/song/media/outer/url?id={sid}.mp3"
MUSIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Accept": "*/*",
}


# ---------- 桌宠字幕直推（绕过消息链的旁路链路） ----------
# 语音/唱歌回复是直发 OneBot 的，管道里的文字链会被 _mute_sing_echo 清空，
# 桌宠插件的 on_decorating_result 拿不到真正念出的文本 → 「有语音、没字幕」。
# 这里在音频发出的同时把同一段文本直接推给桌宠中转（23334），保证声画一致。
PET_RELAY_URL = os.environ.get("PET_RELAY_URL", "http://127.0.0.1:23334/state")


def _pet_source_of(event) -> str:
    platform = str(event.get_platform_name() or "").lower()
    if any(k in platform for k in ("weixin", "wechat", "wcf", "gewe")):
        return "wechat"
    if any(k in platform for k in ("aiocqhttp", "onebot", "qq")):
        return "qq"
    return ""


def push_pet_subtitle(event, text: str, what: str = "语音回复已发出") -> None:
    """把已经念出口/唱出口的文本推成桌宠字幕；即发即忘，失败不影响消息链路。"""
    src = _pet_source_of(event)
    text = (text or "").strip()
    if not src or not text:
        return
    gid = str(event.get_group_id() or "").strip()
    payload = {
        "source": src,
        "state": "working",
        "event": what,
        "detail": text[:400],
        "sender_name": "肥鱼",
        "sender_id": str(event.get_self_id() or ""),
        "is_self": True,
        "group_id": gid,
        "chat_type": "group" if gid else "private",
    }

    async def _post():
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(PET_RELAY_URL, json=payload) as resp:
                    await resp.text()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[basic_tools] 桌宠字幕推送失败: {type(e).__name__}: {e}")

    try:
        asyncio.get_running_loop().create_task(_post())
    except RuntimeError:
        pass


def _safe_music_name(text: str, limit: int = 40) -> str:
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text or "")).strip(" ._")
    return text[:limit] or "song"


# 非原唱/改版识别：DJ、翻唱、现场、伴奏这类音源本身就被重编曲或叠加过，
# 换音色时人声被伴奏/和声干扰，唱出来最容易少词，所以默认不用。
_DERIVATIVE_RE = re.compile(
    r"dj|翻唱|改编|版|现场|live|演唱会|伴奏|纯音乐|remix|混音|升调|降调|"
    r"女声|男声|片段|铃声|串烧|抖音|合唱|single version|demo|cover|"
    r"钢琴|吉他|清唱|和声|翻自|片段",
    re.I,
)


def _is_derivative(text: str) -> bool:
    """标题/歌手名看着像改版、翻唱、现场、伴奏就返回 True。"""
    return bool(_DERIVATIVE_RE.search(str(text or "")))


def _audio_seconds(path: str) -> float:
    """拿音频时长（秒）；没有 ffprobe 就返回 0，交给调用方按「未知」处理。"""
    import shutil
    import subprocess

    exe = shutil.which("ffprobe")
    if not exe:
        ve = _load_voice_engine()
        ff = ""
        try:
            ff = ve._ffmpeg() if ve is not None else ""
        except Exception:  # noqa: BLE001
            ff = ""
        cand = os.path.join(os.path.dirname(ff), "ffprobe.exe") if ff else ""
        if cand and os.path.exists(cand):
            exe = cand
    if not exe:
        return 0.0
    try:
        out = subprocess.run(
            [
                exe, "-v", "quiet", "-show_entries", "format=duration",
                "-of", "csv=p=0", path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return float((out.stdout or "").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


_ASR_PROBE = None


def _song_lyrics_probe(path: str) -> str:
    """扒歌验货：抽中段两窗各 15 秒转写，返回拼接文本。

    网易云外链对没版权的歌会给假音源（伴奏、带「字幕byxxx」水印的视频音轨），
    整首转写不出歌词。本地 faster-whisper small，失败返回空串（按可疑处理）。
    """
    global _ASR_PROBE
    import subprocess
    import wave

    import numpy as np

    def _one(start: float, dur: float) -> str:
        tmp = path + ".probe16k.wav"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", path,
                 "-ar", "16000", "-ac", "1", tmp],
                check=True, capture_output=True, timeout=30,
            )
            with wave.open(tmp, "rb") as w:
                sr = w.getframerate()
                w.setpos(min(int(start * sr), max(w.getnframes() - 1, 0)))
                raw = w.readframes(int(dur * sr))
            y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            segs, _ = _ASR_PROBE.transcribe(
                y, language="zh", beam_size=1,
                condition_on_previous_text=False)
            return "".join(s.text for s in segs)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[basic_tools] 扒歌验货转写失败: {type(e).__name__}: {e}")
            return ""
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    total = _audio_seconds(path)
    if total < 30:
        return ""
    if _ASR_PROBE is None:
        from faster_whisper import WhisperModel
        _ASR_PROBE = WhisperModel(
            "small", device="cpu", compute_type="int8",
            local_files_only=True)
    return (_one(total * 0.25, 15) + " " + _one(total * 0.55, 15)).strip()


async def _netease_fetch_song(song: str) -> str:
    """曲库没有时去网易云扒一首：搜索 → 外链 mp3 → 存进 music 目录。

    返回本地文件路径；没版权/网络不通/都下不来就返回空串。
    """
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, sock_read=25),
            headers=MUSIC_HEADERS,
            trust_env=False,
        ) as sess:
            async with sess.post(
                MUSIC_API,
                data={"s": song, "type": "1", "offset": "0", "limit": "6"},
            ) as r:
                if r.status != 200:
                    return ""
                payload = await r.json(content_type=None)
            songs = ((payload or {}).get("result") or {}).get("songs") or []
            if not isinstance(songs, list) or not songs:
                return ""
            picks = []
            for s in songs:
                if not isinstance(s, dict) or not s.get("id"):
                    continue
                name = str(s.get("name") or "").strip()
                if not name or "伴奏" in name:
                    continue
                ar = "、".join(
                    str(a.get("name") or "").strip()
                    for a in (s.get("artists") or [])
                    if isinstance(a, dict)
                )
                picks.append((name, ar, s["id"]))
            # 同名原曲优先：网易云热门原曲常因没版权下不了，排序靠前的多是翻唱/改版
            # 改版（DJ/翻唱/现场/伴奏）默认不碰，实在只剩改版才退回去用
            exact = song.strip().lower()
            clean = [t for t in picks if not _is_derivative(t[0] + " " + t[1])]
            pool = clean or picks
            pool.sort(key=lambda t: 0 if t[0].lower() == exact else 1)
            os.makedirs(MUSIC_DIR, exist_ok=True)
            for name, ar, sid in pool[:3]:
                fname = _safe_music_name(f"{name}-{ar}" if ar else name) + ".mp3"
                path = os.path.join(MUSIC_DIR, fname)
                if os.path.exists(path) and os.path.getsize(path) > 100_000:
                    return path
                try:
                    async with sess.get(
                        MUSIC_OUTER.format(sid=sid),
                        allow_redirects=True,
                        max_redirects=6,
                    ) as r:
                        if r.status != 200:
                            continue
                        ctype = str(r.headers.get("Content-Type") or "").lower()
                        if not any(k in ctype for k in ("audio", "mpeg", "octet")):
                            continue
                        buf = bytearray()
                        async for chunk in r.content.iter_chunked(64 * 1024):
                            buf.extend(chunk)
                            if len(buf) > 30 * 1024 * 1024:
                                break
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[basic_tools] 扒歌失败 {name}: {type(e).__name__}")
                    continue
                if len(buf) < 100_000:
                    continue
                tmp = path + ".part"
                with open(tmp, "wb") as f:
                    f.write(buf)
                os.replace(tmp, path)
                # 假音源验货：网易云外链对没版权的歌会给伴奏/水印音轨，
                # 整首转写不出歌词。抽两窗验货，凑不出几个字就拒收换下一首。
                probe = await asyncio.to_thread(_song_lyrics_probe, path)
                words = re.sub(r"[^一-鿿a-zA-Z0-9]", "", probe)
                if len(words) < 8 or "字幕by" in probe:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    logger.warning(
                        f"[basic_tools] 扒到的 {name} 是假音源"
                        f"（转写「{probe[:24]}」），已拒收"
                    )
                    continue
                logger.info(
                    f"[basic_tools] 扒到新歌：{fname}（{len(buf) // 1024}KB，网易云 {sid}）"
                )
                return path
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[basic_tools] 网易云扒歌失败: {type(e).__name__}: {e}")
    return ""


def _find_music_file(name: str) -> str:
    """在曲库里按名字找一首（名字越贴近越优先），找不到返回空串。

    同名翻唱/改编版很多，先比「歌名-歌手」的歌名部分完全一致，再看包含关系。
    """
    os.makedirs(MUSIC_DIR, exist_ok=True)
    exts = (".mp3", ".m4a", ".flac", ".wav", ".ogg")
    local = [f for f in os.listdir(MUSIC_DIR) if f.lower().endswith(exts)]
    want = str(name or "").strip().lower()
    if not want:
        return ""

    def rank(f: str) -> tuple[int, int, int, str]:
        stem = os.path.splitext(f)[0].lower()
        base = stem.split("-")[0].strip()
        if stem == want or base == want:
            r = 0
        elif stem.startswith(want + "-") or base.startswith(want):
            r = 1
        elif want in stem:
            r = 9
        else:
            r = 99
        # 同样贴题时，原唱优先；DJ/翻唱/现场这类改版排到最后
        return (1 if _is_derivative(stem) else 0, r, len(f), f)

    cands = sorted(rank(f) for f in local if want in f.lower())
    return os.path.join(MUSIC_DIR, cands[0][3]) if cands else ""


async def _run_child(args: list[str], timeout: float) -> tuple[bytes, bytes] | None:
    """跑子进程收 stdout/stderr；超时直接杀掉。

    不杀的话子进程会继续跑（还被管道堵住），占着显存变成孤儿，
    之后每次重试都叠一个上去，最后 OOM。
    """
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        name = os.path.basename(args[1] if len(args) > 1 else args[0])
        logger.warning(f"[basic_tools] 子进程超时 {timeout}s，已杀掉：{name}")
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        return None
    except asyncio.CancelledError:
        # /停 取消任务时也要杀掉子进程，否则渲染会变孤儿继续吃显存
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise


async def _run_cover_job(job: dict, timeout: int = 900) -> dict:
    """跑 cover_render.py（分轨/换音色/混音），返回它 stdout 里的结果 JSON。"""
    job_path = job["out"] + ".json"
    stdout = b""
    stderr = b""
    try:
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)
        res = await _run_child(
            [sys.executable, os.path.join(PLUGIN_DIR, "cover_render.py"), job_path],
            timeout,
        )
        if res is None:
            return {"ok": False, "error": "转换超时"}
        stdout, stderr = res
    finally:
        try:
            os.remove(job_path)
        except OSError:
            pass
    info: dict = {}
    for ln in reversed((stdout or b"").decode("utf-8", "ignore").splitlines()):
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                info = json.loads(ln)
            except ValueError:
                info = {}
            break
    if not info.get("ok"):
        logger.warning(
            f"[basic_tools] 翻唱失败: {info} {(stderr or b'')[-300:]!r}"
        )
    return info


SCORE_CACHE_DIR = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "scores"
)

# 后台翻唱是否在跑（整首转换几分钟，同一时间只跑一首）
_COVER_STATE = {"busy": False}
# 后台任务的强引用：asyncio 只存弱引用，不存着会被 GC 掉、任务中途消失
_COVER_TASKS: set[asyncio.Task] = set()
# 翻唱锁文件：热重载会清掉内存标记，靠锁文件继续挡住新任务
COVER_LOCK = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "cover.lock"
)
COVER_LOCK_TTL = 900  # 秒；进程被杀留下残锁，最多挡 15 分钟后自动失效


def _train_pause_flag() -> str:
    """Seed-VC 续训暂停标记（cover_render 渲染前立、渲染完撤）。"""
    cur = PLUGIN_DIR
    for _ in range(8):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        runs = os.path.join(parent, "audio", "seed-vc", "seed-vc-main", "runs")
        if os.path.isdir(runs):
            return os.path.join(runs, ".pause_training")
        cur = parent
    return ""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _drop_stale_cover() -> None:
    """翻唱锁确认是死的：删锁 + 撤掉残留的续训暂停标记。"""
    for path in (COVER_LOCK, _train_pause_flag()):
        if not path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def _cover_busy() -> bool:
    """现在有翻唱在跑吗？内存标记 + 锁文件。

    锁里记着写锁进程的 pid：AstrBot 重启后旧 pid 已死，残锁立刻失效，
    不用等 15 分钟 TTL，也不会让 Seed-VC 续训一直卡在暂停。
    """
    if _COVER_STATE.get("busy"):
        return True
    try:
        age = time.time() - os.path.getmtime(COVER_LOCK)
        raw = open(COVER_LOCK, encoding="utf-8").read().strip()
    except OSError:
        # 没有锁却还留着暂停标记 → 上次任务被重启打断，顺手清掉
        pause = _train_pause_flag()
        if pause:
            try:
                os.remove(pause)
            except OSError:
                pass
        return False
    try:
        meta = json.loads(raw)
        ts = float(meta.get("ts") or 0)
        pid = int(meta.get("pid") or 0)
    except (ValueError, TypeError, AttributeError):
        ts, pid = (float(raw) if raw.replace(".", "", 1).isdigit() else 0.0), 0
    now = time.time()
    if age > COVER_LOCK_TTL or ts > now + 60 or (ts and now - ts > COVER_LOCK_TTL):
        _drop_stale_cover()
        return False
    if pid and pid != os.getpid() and not _pid_alive(pid):
        _drop_stale_cover()
        return False
    return True


def _cover_lock() -> bool:
    """抢翻唱锁，已经有任务占着就返回 False（禁止堆叠）。"""
    if _cover_busy():
        return False
    try:
        os.makedirs(os.path.dirname(COVER_LOCK), exist_ok=True)
        with open(COVER_LOCK, "w", encoding="utf-8") as f:
            json.dump({"ts": int(time.time()), "pid": os.getpid()}, f)
    except OSError:
        return False
    _COVER_STATE["busy"] = True
    return True


def _cover_unlock() -> None:
    _COVER_STATE["busy"] = False
    try:
        os.remove(COVER_LOCK)
    except OSError:
        pass


def _score_cache_file(name: str) -> str:
    """缓存路径：歌名做文件名（Windows 非法字符换掉）。"""
    safe = re.sub(r'[\\/:*?"<>|]', "_", str(name or "").strip())
    return os.path.join(SCORE_CACHE_DIR, (safe or "song") + ".json")


def _score_cache_find(name: str) -> dict | None:
    """按名字找缓存谱：先精确文件名，再按 json 里的歌名模糊包含。"""
    want = str(name or "").strip()
    if not want or not os.path.isdir(SCORE_CACHE_DIR):
        return None
    exact = _score_cache_file(want)
    if os.path.exists(exact):
        try:
            with open(exact, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("lines"):
                return data
        except (OSError, ValueError):
            pass
    for fn in os.listdir(SCORE_CACHE_DIR):
        if not fn.endswith(".json"):
            continue
        stem = fn[:-5]
        if want not in stem and stem not in want:
            continue
        try:
            with open(os.path.join(SCORE_CACHE_DIR, fn), encoding="utf-8") as f:
                data = json.load(f)
            if data.get("lines"):
                return data
        except (OSError, ValueError):
            continue
    return None


def _score_cache_save(name: str, info: dict, source: str) -> None:
    try:
        os.makedirs(SCORE_CACHE_DIR, exist_ok=True)
        data = {
            "name": name,
            "source": source,
            "lines": info["lines"],
            "syllables": info.get("syllables") or [],
            "bpm": info.get("bpm") or 96,
            "total": info.get("total") or 0,
            "range": info.get("range") or [0, 0],
            "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(_score_cache_file(name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info(f"[basic_tools] 扒谱缓存已写入：{name}（{len(data['lines'])} 句）")
    except OSError as e:
        logger.warning(f"[basic_tools] 扒谱缓存写入失败: {e}")


# 唱歌成品缓存：同一「歌名 + 歌词 + 音色 + 谱面」3 天内重复点，直接发上次那版。
# 单独放一个目录，别跟 VOICE_DIR 的「只留最近 100 首」清理打架。
SING_CACHE_DIR = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "sing_cache"
)
SING_CACHE_TTL = 3 * 24 * 3600  # 秒，3 天


def _sing_cache_key(payload: dict | str) -> str:
    raw = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _sing_cache_sweep() -> None:
    """清掉超过 3 天的成品缓存。"""
    if not os.path.isdir(SING_CACHE_DIR):
        return
    now = time.time()
    for fn in os.listdir(SING_CACHE_DIR):
        path = os.path.join(SING_CACHE_DIR, fn)
        try:
            if now - os.path.getmtime(path) > SING_CACHE_TTL:
                os.remove(path)
        except OSError:
            pass


def _sing_cache_find(key: str) -> str | None:
    """命中返回缓存音频路径；过期就当没有（顺手删掉）。"""
    path = os.path.join(SING_CACHE_DIR, key + ".mp3")
    try:
        if os.path.getsize(path) < 2000:
            return None
        if time.time() - os.path.getmtime(path) > SING_CACHE_TTL:
            os.remove(path)
            return None
        return path
    except OSError:
        return None


def _sing_cache_save(key: str, src: str) -> str | None:
    """把成品复制进缓存目录，返回缓存路径。"""
    try:
        if not os.path.exists(src) or os.path.getsize(src) < 2000:
            return None
        os.makedirs(SING_CACHE_DIR, exist_ok=True)
        dst = os.path.join(SING_CACHE_DIR, key + ".mp3")
        shutil.copyfile(src, dst)
        os.utime(dst, None)
        _sing_cache_sweep()
        return dst
    except OSError as e:
        logger.warning(f"[basic_tools] 唱歌缓存写入失败: {e}")
        return None


# 存储维护：一天只跑一次，阈值都放宽，别把还能用的东西清掉。
LOG_RETENTION_DAYS = 30  # AstrBot 轮转日志保留天数
VOICE_KEEP_FILES = 100   # 语音成品最多留多少个
_STORAGE_SWEEP_INTERVAL = 24 * 3600
_STORAGE_SWEEP_TS = 0.0


def _storage_sweep(force: bool = False) -> None:
    """每天清一次过期唱歌缓存、超量的语音成品、AstrBot 旧轮转日志。"""
    global _STORAGE_SWEEP_TS
    now = time.time()
    if not force and now - _STORAGE_SWEEP_TS < _STORAGE_SWEEP_INTERVAL:
        return
    _STORAGE_SWEEP_TS = now

    _sing_cache_sweep()

    try:
        if os.path.isdir(VOICE_DIR):
            files = [f for f in os.listdir(VOICE_DIR) if f.endswith(".mp3")]
            files.sort(key=lambda f: os.path.getmtime(os.path.join(VOICE_DIR, f)))
            for old in files[:-VOICE_KEEP_FILES]:
                os.remove(os.path.join(VOICE_DIR, old))
            # 顺手清掉已删音频留下的 .mp3.json 作业文件，别只删母文件
            for fn in os.listdir(VOICE_DIR):
                if not fn.endswith(".mp3.json"):
                    continue
                if not os.path.exists(os.path.join(VOICE_DIR, fn[:-5])):
                    os.remove(os.path.join(VOICE_DIR, fn))
    except OSError:
        pass

    try:
        log_dir = os.path.join(DATA_ROOT, "logs")
        cutoff = now - LOG_RETENTION_DAYS * 86400
        for fn in os.listdir(log_dir):
            if not (fn.startswith("astrbot.") and fn.endswith(".log")):
                continue
            path = os.path.join(log_dir, fn)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    logger.info(f"[basic_tools] 清理旧日志：{fn}")
            except OSError:
                pass
    except OSError:
        pass


def _ckpt_step(name: str) -> int:
    m = re.search(r"ft_model_(\d+)", name)
    return int(m.group(1)) if m else -1


def _profile_ckpt(root: str, name: str) -> str:
    """档案目录里 Seed-VC 实际会用的权重（判定必须和 cover_render._active_ckpt 一致）。

    ckpt.txt 里存的是相对安装根的路径，进程 cwd 不同 os.path.isfile 就失败；
    以前这里失败后退到「按 mtime 挑最新权重」，cover_render 退到「按步数挑最大」，
    两边挑到不同文件，缓存键和真正渲染用的权重各说各话。现在统一：相对路径只按
    文件名回档案目录里找，找不到再按步数最大。
    """
    d = os.path.join(root, name)
    try:
        with open(os.path.join(d, "ckpt.txt"), encoding="utf-8-sig") as f:
            stored = f.read().strip()
    except OSError:
        stored = ""
    if stored:
        for cand in (os.path.join(d, os.path.basename(stored)), stored):
            if os.path.isfile(cand):
                return cand
    names = [n for n in os.listdir(d)
             if n.startswith("ft_model") and n.endswith(".pth")]
    if not names:
        return ""
    return os.path.join(d, max(names, key=_ckpt_step))


def _voice_tag() -> str:
    """当前声线标识（声线名 + Seed-VC 权重身份）。

    成品缓存必须按声线分开：切到「姬子」再点同一首，不能拿「小忆」那版缓存顶上。
    权重也一起带上——同名声线重新绑了 ckpt（比如续训到 1 万步）后成品也该重跑。
    """
    try:
        ve = _load_voice_engine()
        if ve is None:
            return ""
        name = str(ve.active_profile() or "")
        ck = ""
        root = str(getattr(ve, "PROFILE_ROOT", "") or "")
        # Edge 热门音色没有档案目录：只把声线名带进缓存键，别去 listdir 不存在的目录
        if name and root and os.path.isdir(os.path.join(root, name)):
            ckpt = _profile_ckpt(root, name)
            if ckpt:
                ck = f"{os.path.basename(ckpt)}:{os.path.getsize(ckpt)}"
        return f"{name}|{ck}" if ck else name
    except Exception:  # noqa: BLE001
        return ""


def _cover_cache_key(name: str, src: str) -> str:
    """翻唱缓存键：歌名 + 原曲文件（大小和 mtime，重下后会变）+ 当前声线。

    mix 是混音/咬字增强的版本号：改了 cover_render 的 EQ 或混音参数就 +1，
    免得用户再点时命中上一个版本的成品。
    """
    sig = [0, 0]
    try:
        sig = [os.path.getsize(src), int(os.path.getmtime(src))]
    except OSError:
        pass
    return _sing_cache_key(
        {"kind": "cover", "song": name, "src": sig, "voice": _voice_tag(),
         # mix 3：没有自己 ft_model 的声线改成走基座（不再套小忆的微调），老成品全部作废重跑
         "mix": 3}
    )



SUPER_FACES = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "super_faces.json"
)
# human_sim 把「群里真实出现过的表情名 → face id」记在它自己的 state.json 里；
# 官方 face_config 拿不到的新表情包（如 朵莉亚）只能靠这份数据补
HUMAN_SIM_STATE = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_human_sim", "state.json"
)
# 表情清单属于动态数据：每周直接从 QQ 官方的 face_config.json 重读一次，
# 不再把结果静态入库；抓取结果写桌面 txt 给主人核对。
FACE_TXT = os.path.join(os.path.expanduser("~"), "Desktop", "超级表情清单.txt")
FACE_MARK = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "face_refresh.txt"
)
FACE_REFRESH_SECONDS = 7 * 24 * 3600
# QQ 客户端会更新表情资源（face_config.json），超级表情的 QSid 会跟着变；
# 每周从这里重读一次，结果只进内存 + 写桌面 txt，不再写回 super_faces.json。
FACE_CONFIG_CANDIDATES = [
    os.environ.get("NAPCAT_FACE_CONFIG", ""),
    os.path.join(
        os.path.expanduser("~"),
        "Documents", "Tencent Files", "nt_qq", "global", "nt_data",
        "Emoji", "emoji-resource", "face_config.json",
    ),
    os.path.join(
        os.path.expanduser("~"), "dafeiyu", "stack", "napcat", "qq", "nt_qq",
        "global", "nt_data", "Emoji", "emoji-resource", "face_config.json",
    ),
]
_voice_engine = None


def _split_voice(text: str, limit: int = 42) -> list[str]:
    """把长句切成 1~2 段语音。

    真人发语音不会一口气念 20 秒：说两句、松手、再补一条。
    按标点就近切一半，两段长度尽量均衡，避免出现「一句 3 秒 + 一条 18 秒」。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    mid = len(text) // 2
    cuts = [m.start() + 1 for m in re.finditer(r"[。！？!?…，,；;、]", text)]
    if not cuts:
        return [text]
    cut = min(cuts, key=lambda i: abs(i - mid))
    a, b = text[:cut].strip(), text[cut:].strip()
    return [p for p in (a, b) if p]


def _load_voice_engine():
    """按文件路径加载句调引擎（插件目录不在 sys.path 里，只能这么来）。

    ⚠ 必须先注册进 sys.modules 再 exec_module，否则模块内的 from __future__ /
    dataclass 解析注解会炸（human_sim/filters.py 踩过同一个坑）。
    """
    global _voice_engine
    if _voice_engine is not None:
        return _voice_engine
    import importlib.util
    import sys as _sys

    path = os.path.join(PLUGIN_DIR, "voice_engine.py")
    if not os.path.exists(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("dfy_voice_engine", path)
        mod = importlib.util.module_from_spec(spec)
        _sys.modules["dfy_voice_engine"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _voice_engine = mod
        return mod
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[basic_tools] 句调引擎加载失败（退回单段合成）: {e}")
        return None


_sing_engine = None
_music_lib = None


def _load_sing_engine():
    """唱歌引擎（简谱解析/填词校验），加载方式和 voice_engine 一样。"""
    global _sing_engine
    if _sing_engine is not None:
        return _sing_engine
    import importlib.util
    import sys as _sys

    path = os.path.join(PLUGIN_DIR, "sing_engine.py")
    if not os.path.exists(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("dfy_sing_engine", path)
        mod = importlib.util.module_from_spec(spec)
        _sys.modules["dfy_sing_engine"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _sing_engine = mod
        return mod
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[basic_tools] 唱歌引擎加载失败: {e}")
        return None


def _load_music_lib():
    """乐曲资源库（咪咕搜索/直链 + 高潮裁剪），加载方式同 sing_engine。"""
    global _music_lib
    if _music_lib is not None:
        return _music_lib
    import importlib.util
    import sys as _sys

    path = os.path.join(PLUGIN_DIR, "music_lib.py")
    if not os.path.exists(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("dfy_music_lib", path)
        mod = importlib.util.module_from_spec(spec)
        _sys.modules["dfy_music_lib"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _music_lib = mod
        return mod
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[basic_tools] 乐曲资源库加载失败: {e}")
        return None


def _daily_gate(kind: str, cap: int) -> str | None:
    """每日互动限额（防封号）；放行返回 None，超限返回拒绝文案"""
    today = time.strftime("%m-%d")
    data: dict = {}
    try:
        if os.path.exists(LIMITS_PATH):
            with open(LIMITS_PATH, encoding="utf-8") as f:
                data = json.load(f)
    except Exception:  # noqa: BLE001
        data = {}
    if data.get("day") != today:
        data = {"day": today}
    data[kind] = int(data.get(kind, 0)) + 1
    if data[kind] > cap:
        return f"今日「{kind}」已达防封上限（{cap} 次/天），明天再玩"
    try:
        os.makedirs(os.path.dirname(LIMITS_PATH), exist_ok=True)
        with open(LIMITS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    return None

WMO_CODES = {
    0: "晴", 1: "基本晴", 2: "多云", 3: "阴", 45: "有雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹",
}

# face_convert 会在消息前塞身份前缀（[主人(1000000000)] / [群友 昵称(QQ号)]），
# 命令要解析 message_str 时先剥掉，否则第一个参数永远是个中括号标签
_IDENTITY_TAG_RE = re.compile(r"^\[(?:主人|群友|非主人)[^\]]{0,40}\]\s*")


def _weather_default_city() -> str:
    """插件 config.json 里的默认城市（群友普遍问「好热啊」而不报城市时用）。"""
    try:
        with open(os.path.join(PLUGIN_DIR, "config.json"), encoding="utf-8-sig") as f:
            d = json.load(f)
        return str((d or {}).get("weather_default_city") or "").strip()
    except (OSError, ValueError):
        return ""


def _load_ollama_cfg() -> tuple[str, str]:
    """从 AstrBot 主配置读本地 Ollama 的地址和视觉模型（头像识别用，免云端）"""
    try:
        with open(
            os.path.join(DATA_ROOT, "cmd_config.json"), encoding="utf-8-sig"
        ) as f:
            cfg = json.load(f)
        for prov in cfg.get("provider", []):
            if str(prov.get("id", "")).startswith("ollama-chat"):
                base = str(prov.get("api_base") or "http://127.0.0.1:11434").rstrip("/")
                model = (prov.get("model_config") or {}).get("model") or "qwen3.5:9b"
                return base, model
    except Exception:  # noqa: BLE001
        pass
    return "http://127.0.0.1:11434", "qwen3.5:9b"


OLLAMA_BASE, OLLAMA_VISION_MODEL = _load_ollama_cfg()

# 主人 QQ：从主配置 admins_id 动态取，私聊里被告知「查主人/查我」时直接指他，不必让模型猜昵称
_OWNER_CACHE = {"mtime": None, "qq": ""}


def owner_qq() -> str:
    """主人 QQ：取 cmd_config.json 里 admins_id 的第一个纯数字账号。

    按文件 mtime 缓存，改配置后不用重启就生效；读不到返回空串，
    调用方自己回退到当前发言人（不再写死一个占位号码）。
    """
    path = os.path.join(DATA_ROOT, "cmd_config.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    if _OWNER_CACHE["mtime"] != mtime:
        qq = ""
        try:
            with open(path, encoding="utf-8-sig") as f:
                admins = json.load(f).get("admins_id") or []
            qq = next(
                (str(a).strip() for a in admins if str(a).strip().isdigit()), ""
            )
        except Exception:  # noqa: BLE001
            pass
        _OWNER_CACHE.update(mtime=mtime, qq=qq)
    return _OWNER_CACHE["qq"]


async def _hint_private(event: AstrMessageEvent, text: str, image_url: str = "") -> None:
    """群里的 /指令提示走私聊发给发言人，不在群里刷屏；私聊场景或发送
    失败时退回原会话正常回复。image_url 可选，用于私发二维码等图片。"""
    text = str(text or "").strip()
    if not text and not image_url:
        return
    try:
        if (event.get_group_id()
                and str(event.get_platform_name() or "").lower() == "aiocqhttp"
                and getattr(event, "bot", None) is not None):
            segs: list[dict] = []
            if image_url:
                segs.append({"type": "image", "data": {"url": image_url}})
            if text:
                segs.append({"type": "text", "data": {"text": text}})
            # 注意：这版 aiocqhttp 没有 call_api（会被 __getattr__ 误映射成
            # call_action("call_api", ...)），必须直接用 call_action
            await event.bot.call_action(
                "send_private_msg",
                user_id=int(event.get_sender_id() or 0),
                message=segs if image_url else text,
            )
            return
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[basic_tools] 提示走私聊失败（对方可能关了临时会话/不是好友），"
            f"改回群里 @ 发言人: {type(e).__name__}: {e}")
    try:
        if event.get_group_id():
            # 私发不出去就退回群里，@ 发言人保证能看到（比刷一整段列表克制）
            segs2: list = []
            if image_url:
                segs2.append(Image.fromURL(image_url))
            if text:
                segs2.append(At(qq=str(event.get_sender_id() or "")))
                segs2.append(Plain(f" {text}"))
            if segs2:
                await event.send(MessageChain(chain=segs2))
            return
        if image_url:
            await event.send(event.image_result(image_url))
        if text:
            await event.send(event.plain_result(text))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[basic_tools] 提示发送失败: {type(e).__name__}: {e}")
