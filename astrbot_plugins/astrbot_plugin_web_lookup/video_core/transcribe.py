"""音频下载、ffmpeg 抽轨与 faster-whisper 本地转写。"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave

import aiohttp

from .models import DEFAULT_CFG, _clean

_WHISPER = None


_WHISPER_LOCK = threading.Lock()


def _ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


def _whisper_model():
    global _WHISPER
    with _WHISPER_LOCK:
        if _WHISPER is None:
            from faster_whisper import WhisperModel

            _WHISPER = WhisperModel("small", device="cpu", compute_type="int8")
    return _WHISPER


def _transcribe_sync(path: str, max_s: int) -> str:
    """ffmpeg 抽 16k 单声道 -> numpy -> faster-whisper（small/CPU/int8）。"""
    import numpy as np

    ff = _ffmpeg_exe()
    if not ff:
        raise RuntimeError("没找到 ffmpeg")
    wav = path + ".16k.wav"
    try:
        subprocess.run(
            [ff, "-y", "-i", path, "-t", str(int(max_s)), "-ar", "16000", "-ac", "1", wav],
            capture_output=True, timeout=180,
        )
        with wave.open(wav, "rb") as w:
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(
                np.float32
            ) / 32768.0
    finally:
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except OSError:
            pass
    if audio.size == 0:
        return ""
    kw = dict(
        language="zh", beam_size=1,
        initial_prompt="以下是普通话视频内容，使用简体中文。",
    )
    try:
        segments, _info = _whisper_model().transcribe(
            audio, vad_filter=True, **kw)
    except AttributeError:
        # onnxruntime 版本比 faster-whisper 旧：Silero VAD 的
        # is_webgpu_graph_capture_enabled 不存在。关掉 VAD 照样能转写。
        segments, _info = _whisper_model().transcribe(
            audio, vad_filter=False, **kw)
    return _clean("".join(s.text for s in segments))


async def transcribe_urls(
    urls: list[str], headers: dict, cfg: dict | None = None, duration: float = 0.0
) -> tuple[str, bool, str]:
    """下载音频直链 -> ffmpeg 截前 N 秒 -> whisper 转写。返回 (文本, 是否截断, 失败原因)。"""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    if not urls:
        return "", False, "没有可下载的音频直链"
    max_s = max(30, int(cfg.get("video_audio_max_s", 300)))
    cap = max(5, int(cfg.get("video_audio_max_mb", 60))) * 1024 * 1024
    path = os.path.join(tempfile.gettempdir(), f"fy_video_{int(time.time() * 1000)}.bin")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=180), trust_env=False
        ) as sess:
            got = False
            for u in urls:
                try:
                    async with sess.get(u, headers=headers, allow_redirects=True) as r:
                        if r.status not in (200, 206):
                            continue
                        size = 0
                        with open(path, "wb") as f:
                            async for chunk in r.content.iter_chunked(65536):
                                size += len(chunk)
                                if size > cap:
                                    break
                                f.write(chunk)
                        if size > 0:
                            got = True
                            break
                except Exception:  # noqa: BLE001
                    continue
            if not got:
                return "", False, "音频下载失败"
        text = await asyncio.to_thread(_transcribe_sync, path, max_s)
        if not text:
            return "", False, "whisper 没识别出文字"
        # 纯 BGM 的视频（没配音）常被 whisper 写成英文歌词，这种不能当讲解用
        hz = len(re.findall(r"[\u4e00-\u9fa5]", text))
        if len(text) >= 60 and hz / len(text) < 0.35:
            return "", False, "转写出来不像中文讲解（视频可能只有BGM）"
        truncated = bool(duration and float(duration) > float(max_s))
        return text[: max(500, int(cfg.get("video_transcript_chars", 4000)))], truncated, ""
    except Exception as e:  # noqa: BLE001
        return "", False, f"{type(e).__name__}"
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
