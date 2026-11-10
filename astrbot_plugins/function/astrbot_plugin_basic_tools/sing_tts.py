"""sing_render 拆分：整句语音合成（声线来源）。

从 sing_render.py 按职责拆出；sing_render.py 保留为门面与脚本入口。
edge-tts（带 WordBoundary）、本地 CosyVoice2 克隆声线（线程级长连接）、
Windows SAPI 离线兜底都在这里。CosyVoice 连接状态（_COSY_LOCAL）是
全局可变状态的唯一归属。
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from sing_util import to_wav


async def tts_line(text: str, mp3_path: str, attempts: int = 5,
                   voice: str = "zh-CN-XiaoyiNeural") -> list[tuple[float, float, str]]:
    """整句合成一次，返回 WordBoundary [(开始秒, 时长秒, 词)]。"""
    import edge_tts
    last: Exception | None = None
    for i in range(attempts):
        try:
            com = edge_tts.Communicate(
                text, voice, rate="+0%", pitch="+0Hz",
                boundary="WordBoundary",
            )
            audio: list[bytes] = []
            bounds: list[tuple[float, float, str]] = []
            async for item in com.stream():
                if item["type"] == "audio":
                    audio.append(item["data"])
                elif item["type"] == "WordBoundary":
                    # edge 的 offset/duration 单位是 100ns
                    bounds.append((item["offset"] / 1e7, item["duration"] / 1e7,
                                   str(item.get("text") or "")))
            if not audio:
                raise RuntimeError("edge-tts 没返回音频")
            with open(mp3_path, "wb") as f:
                f.write(b"".join(audio))
            return bounds
        except Exception as e:  # noqa: BLE001
            last = e
            await asyncio.sleep(0.8 + i * 1.2)
    raise RuntimeError(f"TTS {attempts} 次都失败: {type(last).__name__}: {last}")


# 本地 CosyVoice2 克隆声线（聊天语音系统同款，主人点的「晓波」）。没有词边界，
# 切字走 char_spans 的能量均分兜底；情绪语速统一 1.0（唱歌自己会拉时长）。
COSY_URL = os.environ.get("COSYVOICE_URL", "http://127.0.0.1:8714")
_COSY_PROMPT = ("今天天气真好呀，我们一起去公园散步吧。"
                "你看那边的花都开了，心情一下子就亮起来了呢。")
_COSY_AF = ("highpass=f=90,equalizer=f=2800:t=q:w=1.0:g=3,"
            "loudnorm=I=-17:TP=-1.5:LRA=11")


_COSY_URL_PARTS = urlsplit(COSY_URL)
_COSY_HOST = _COSY_URL_PARTS.hostname or "127.0.0.1"
_COSY_PORT = _COSY_URL_PARTS.port or 8714
_COSY_LOCAL = threading.local()


def _cosy_connection(timeout: float = 300.0) -> http.client.HTTPConnection:
    """每线程复用一条长连接，整首歌几十句不用反复重连。"""
    conn = getattr(_COSY_LOCAL, "conn", None)
    if conn is None:
        conn = http.client.HTTPConnection(_COSY_HOST, _COSY_PORT, timeout=timeout)
        _COSY_LOCAL.conn = conn
    return conn


def _cosy_drop() -> None:
    conn = getattr(_COSY_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass
    _COSY_LOCAL.conn = None


def tts_line_cosy_sync(text: str, out_path: str, speed: float = 1.0,
                       prompt_wav: str | None = None,
                       prompt_text: str | None = None) -> None:
    body = {"text": text, "speed": speed, "fmt": "mp3",
            "prompt_text": prompt_text or _COSY_PROMPT, "af": _COSY_AF}
    if prompt_wav:
        body["prompt_wav"] = prompt_wav  # 服务端按请求换参考声线（A/B 音色用）
    payload = json.dumps(body).encode("utf-8")
    try:
        conn = _cosy_connection()
        conn.request(
            "POST",
            "/tts",
            body=payload,
            headers={"Content-Type": "application/json", "Connection": "keep-alive"},
        )
        response = conn.getresponse()
        audio = response.read()
        if response.will_close:
            _cosy_drop()
        if response.status >= 400:
            raise RuntimeError(f"CosyVoice HTTP {response.status}: {audio[:200]!r}")
    except (http.client.HTTPException, OSError):
        _cosy_drop()
        raise
    if not audio or len(audio) < 1000:
        raise RuntimeError("CosyVoice 返回音频过短/为空")
    with open(out_path, "wb") as f:
        f.write(audio)


def sapi_line(text: str, wav_path: str) -> bool:
    """离线兜底：用 Windows SAPI 合成整句（没有词边界，按字数均摊即可）。"""
    if sys.platform != "win32":
        return False
    txt = wav_path + ".txt"
    raw = wav_path + ".sapi.wav"
    with open(txt, "w", encoding="utf-8") as f:
        f.write(text)
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$t=Get-Content -LiteralPath '" + txt + "' -Encoding UTF8 -Raw;"
        "$sp=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$sp.SetOutputToWaveFile('" + raw + "');"
        "$sp.Speak($t);$sp.Dispose()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=60, check=True)
        if not (os.path.exists(raw) and os.path.getsize(raw) > 1024):
            return False
        to_wav(raw, wav_path)
        return os.path.exists(wav_path) and os.path.getsize(wav_path) > 1024
    except Exception:  # noqa: BLE001
        return False
