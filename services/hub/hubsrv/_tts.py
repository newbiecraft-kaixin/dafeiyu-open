"""桌宠 TTS 转发：用指定克隆声线念一句话，带落盘缓存。"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from ._admins import ROOT
from ._voices import _voice_dir, _voice_prompt
from .http_client import http_call


def handle_tts(req: Any) -> Any:
    """给桌宠用的合成接口：用指定克隆声线（cosyvoice profiles）念一句话，直接回 wav。

    桌宠自带的 /voice/speak 只认 10 个热门 Edge 音色，切到克隆声线后会把名字当 Edge 音色传，
    于是 400 静音。这里由中枢代转 CosyVoice，桌宠按名字区分走哪条路。
    """
    from infra import Blob, fail

    payload = req.payload if isinstance(req.payload, dict) else {}
    text = str(payload.get("text") or "").strip()
    voice = str(payload.get("voice") or "").strip()
    if not text:
        return fail("text 不能为空", 400)
    try:
        folder = _voice_dir(voice)
    except ValueError as error:
        return fail(str(error), 400)
    if not (folder / "ref.wav").exists():
        return fail("该声线缺少 ref.wav", 400)
    # 克隆声线每次都要跑一遍 CosyVoice 推理（冷启动上百秒），固定台词/动作台词会被反复念。
    # 这里按「声线 + 参考音版本 + 文本」落盘缓存，同一句话第二次直接回文件，不再占推理队列。
    cache_dir = ROOT / "tts_cache"
    try:
        ref_stamp = str(int((folder / "ref.wav").stat().st_mtime))
    except OSError:
        ref_stamp = "0"
    key = hashlib.sha1(
        (voice + "\x00" + ref_stamp + "\x00" + text).encode("utf-8")
    ).hexdigest()
    cache_file = cache_dir / (key + ".wav")
    if cache_file.exists() and cache_file.stat().st_size > 0:
        return 200, Blob(cache_file.read_bytes(), "audio/wav")
    body = {
        "text": text,
        "prompt_wav": str(folder / "ref.wav"),
        "prompt_text": _voice_prompt(folder),
        "fmt": "wav",
        "speed": 1.0,
    }
    status, data = http_call("POST", "http://127.0.0.1:8714/tts", body, timeout=240.0)
    if status != 200 or not isinstance(data, (bytes, bytearray)):
        message = data.get("error") if isinstance(data, dict) else "CosyVoice 未响应"
        return fail("合成失败：" + str(message or "未知错误"), 502)
    audio = bytes(data)
    if audio:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            temp = cache_file.with_suffix(".tmp")
            temp.write_bytes(audio)
            temp.replace(cache_file)
            _prune_tts_cache(cache_dir)
        except OSError:
            pass
    return 200, Blob(audio, "audio/wav")


TTS_CACHE_MAX_FILES = 2000
TTS_CACHE_KEEP_DAYS = 7


def _prune_tts_cache(cache_dir: Path) -> None:
    """缓存最多留 2000 条，且只淘汰 7 天前的旧文件。

    之前上限 400，聊天台词一多就把动作台词/固定台词挤掉，桌宠下次念又要现合成。
    现在把上限放大，并给最近 7 天的文件免死：一周内反复念的句子一定还在。
    """
    cutoff = time.time() - TTS_CACHE_KEEP_DAYS * 86400
    try:
        files = sorted(
            (path for path in cache_dir.glob("*.wav")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[TTS_CACHE_MAX_FILES:]:
        try:
            if stale.stat().st_mtime > cutoff:
                continue
            stale.unlink()
        except OSError:
            pass
