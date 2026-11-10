"""video.parse_api 的音频转写兜底：下载音频、ffmpeg 抽轨、whisper / 本机字幕服务转写。

从 parse_api.py 按职责拆出（纯移动，逻辑与签名不变）；
whisper 模型单例（_WHISPER）与加载锁等全局可变状态归属本模块。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import wave
from typing import Any, Dict, List, Optional, Tuple

from _parse_common import _OPENER, _clean, _http, _remain

_WHISPER = None
_WHISPER_LOCK = threading.Lock()


def _ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


def _download_audio(urls: List[str], headers: Dict[str, str], deadline: float,
                    cap_mb: int = 60) -> str:
    cap = max(5, int(cap_mb)) * 1024 * 1024
    for url in urls[:3]:
        path = os.path.join(tempfile.gettempdir(), f"dfy_video_{uuid.uuid4().hex}.bin")
        size = 0
        try:
            request = urllib.request.Request(url, headers=headers)
            with _OPENER.open(request, timeout=_remain(deadline, 60)) as response:
                with open(path, "wb") as handle:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        handle.write(chunk)
                        size += len(chunk)
                        if size >= cap or time.time() > deadline:
                            break
            if size > 0:
                return path
        except Exception:  # noqa: BLE001 - 换下一个直链
            pass
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    return ""


def _ffmpeg_trim(path: str, max_s: int, deadline: float) -> str:
    """把任意音频/视频抽成 16k 单声道 wav，并截到前 max_s 秒。"""
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return ""
    wav = path + ".16k.wav"
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", path, "-t", str(int(max_s)), "-ar", "16000", "-ac", "1", wav],
            capture_output=True,
            timeout=max(20, min(180, int(deadline - time.time()))),
        )
        if os.path.exists(wav) and os.path.getsize(wav) > 44:
            return wav
    except Exception:  # noqa: BLE001
        pass
    return ""


def _whisper_model():
    global _WHISPER
    with _WHISPER_LOCK:
        if _WHISPER is None:
            from faster_whisper import WhisperModel

            _WHISPER = WhisperModel("small", device="cpu", compute_type="int8")
        return _WHISPER


def _transcribe_sync(wav: str) -> str:
    import numpy as np

    with wave.open(wav, "rb") as handle:
        audio = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    audio = audio.astype(np.float32) / 32768.0
    if audio.size == 0:
        return ""
    options = dict(
        language="zh",
        beam_size=1,
        initial_prompt="以下是普通话视频内容，使用简体中文。",
    )
    try:
        segments, _info = _whisper_model().transcribe(audio, vad_filter=True, **options)
    except AttributeError:
        # onnxruntime 版本比 faster-whisper 旧时没有 Silero VAD，关掉照样能转写
        segments, _info = _whisper_model().transcribe(audio, vad_filter=False, **options)
    return _clean("".join(segment.text for segment in segments))


def _run_timed(func, timeout: float, *args) -> Tuple[str, str]:
    """在守护线程里跑（whisper 无法中断），超时就放弃并返回原因。"""
    box: Dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = func(*args)
        except Exception as exc:  # noqa: BLE001
            box["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(max(5.0, float(timeout)))
    if thread.is_alive():
        return "", "转写超时"
    if "error" in box:
        reason = str(box["error"])
        if reason.startswith("ModuleNotFoundError"):
            return "", "本地没装 faster-whisper"
        return "", reason
    return str(box.get("value") or ""), ""


def _subtitle_service_asr(wav: str, deadline: float) -> str:
    """本机 subtitle 服务（23405）在线时优先用它，省得再加载一份模型。"""
    try:
        with _http("http://127.0.0.1:23405/health", timeout=0.8) as response:
            if response.status != 200:
                return ""
    except Exception:  # noqa: BLE001
        return ""
    try:
        body = json.dumps({"path": wav}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        with _http(
            "http://127.0.0.1:23405/asr", headers, data=body,
            timeout=_remain(deadline, 180),
        ) as response:
            payload = json.loads(response.read().decode("utf-8", "ignore"))
        if isinstance(payload, dict) and payload.get("ok"):
            return _clean((payload.get("data") or {}).get("text"))
    except Exception:  # noqa: BLE001
        pass
    return ""


def _transcribe_audio(urls: List[str], headers: Dict[str, str], cfg: Dict[str, Any],
                      duration: float, deadline: float) -> Tuple[str, bool, str]:
    """返回 (文本, 是否截断, 失败原因)；任何异常都不往外抛。"""
    if not urls:
        return "", False, "没有可下载的音频直链"
    max_s = max(30, int(cfg["audio_max_s"]))
    source = ""
    wav = ""
    try:
        source = _download_audio(urls, headers, deadline)
        if not source:
            return "", False, "音频下载失败"
        wav = _ffmpeg_trim(source, max_s, deadline)
        if not wav:
            return "", False, "音频抽轨失败（缺 ffmpeg）"

        text = _subtitle_service_asr(wav, deadline)
        why = ""
        if not text:
            text, why = _run_timed(_transcribe_sync, max(30.0, deadline - time.time()), wav)
        if not text:
            return "", False, why or "whisper 没识别出文字"

        chinese = len(re.findall(r"[\u4e00-\u9fa5]", text))
        if len(text) >= 60 and chinese / len(text) < 0.35:
            return "", False, "转写出来不像中文讲解（视频可能只有 BGM）"
        truncated = bool(duration and float(duration) > float(max_s))
        return text[: max(500, int(cfg["transcript_chars"]))], truncated, ""
    except Exception as exc:  # noqa: BLE001 - 转写是兜底能力，绝不影响主流程
        return "", False, f"{type(exc).__name__}"
    finally:
        for path in (wav, source):
            if path:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
