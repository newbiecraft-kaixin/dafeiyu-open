"""音频 / 视觉推理：下载、解码、转写与画面描述。"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .config import ASR_MODEL, MAX_BYTES, OLLAMA, VISION_MODEL

_LOCK = threading.Lock()
_whisper = None

# 语言 → (whisper language, 初始提示词)。auto 交给 whisper 自己判定。
_LANG_PROFILES = {
    "zh": ("zh", "以下是普通话对话，使用简体中文。"),
    "en": ("en", "This is an English conversation. Use standard English punctuation."),
    "auto": (None, ""),
}

_VISION_PROMPT = {
    "zh": (
        "用一句中文说清这张图里是什么（谁/什么东西/什么场景/图上的字），"
        "不超过60字。只写画面里能确认的事实，不猜、不客套。"
    ),
    "en": (
        "Describe in one English sentence what is in this image "
        "(who / what / scene / any text in the image) in at most 25 words. "
        "Only state facts visible in the image; do not guess or add pleasantries."
    ),
}


def lang_profile(lang: str) -> tuple:
    """把请求里的 lang 归一成受支持的档位，未知值按 zh 处理。"""
    key = (lang or "zh").lower()
    if key.startswith("en"):
        key = "en"
    elif key.startswith(("auto", "detect")):
        key = "auto"
    else:
        key = "zh"
    return key, _LANG_PROFILES[key]


def fetch(src: str) -> tuple[str, bool]:
    """把 path / url 统一成本地文件。返回 (路径, 是否临时文件)。"""
    if src.startswith(("http://", "https://")):
        suffix = os.path.splitext(urlparse(src).path)[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            with urllib.request.urlopen(src, timeout=60) as resp:
                data = resp.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("文件过大")
            tmp.write(data)
            return tmp.name, True
    path = Path(src)
    if not path.is_file():
        raise FileNotFoundError(src)
    return str(path), False


def load_whisper():
    global _whisper
    with _LOCK:
        if _whisper is None:
            from faster_whisper import WhisperModel

            _whisper = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8")
        return _whisper


def whisper_loaded() -> bool:
    return _whisper is not None


def asr_sync(path: str, lang: str = "zh") -> str:
    """音频/视频 → 文字。音频解码走 ffmpeg + numpy（绕开 av 与 faster-whisper 的兼容问题）。"""
    import wave

    import numpy as np

    wav = path + ".16k.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-ar", "16000", "-ac", "1", wav],
            capture_output=True,
            timeout=180,
        )
        with wave.open(wav, "rb") as wf:
            audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        audio = audio.astype(np.float32) / 32768.0
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    if audio.size == 0:
        return ""
    model = load_whisper()
    _key, (whisper_lang, prompt) = lang_profile(lang)
    segments, _info = model.transcribe(
        audio,
        language=whisper_lang,
        beam_size=1,
        vad_filter=True,
        initial_prompt=prompt or None,
    )
    return "".join(seg.text for seg in segments).strip()


def vision_sync(path: str, lang: str = "zh") -> str:
    """图片/视频帧 → 一句话画面描述（本地 Ollama vision）。"""
    key, _profile = lang_profile(lang)
    with open(path, "rb") as handle:
        image = base64.b64encode(handle.read()).decode("ascii")
    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": _VISION_PROMPT[key],
                "images": [image],
            }
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 120},
    }
    req = urllib.request.Request(
        OLLAMA.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = ((data.get("message") or {}).get("content") or "").strip()
    text = " ".join(text.split())
    if key == "zh":
        return (text.split("。")[0].strip()[:80] if text else "")
    return (text.split(". ")[0].strip()[:200] if text else "")
