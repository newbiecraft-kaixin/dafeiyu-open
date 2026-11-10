"""视频解析对外入口、平台分发与耗时预估。"""
from __future__ import annotations

import asyncio
import math
import time

from .bili import _parse_bili
from .douyin import _parse_douyin
from .ixigua import _parse_ixigua
from .kuaishou import _parse_kuaishou
from .models import (
    DEFAULT_CFG, _RESULT_CACHE, _RESULT_CACHE_MAX, _RESULT_CACHE_TTL_S,
    _cache_key, _empty, _lock, _num, detect_platform,
)

async def parse_video(url: str, cfg: dict | None = None, on_meta=None) -> dict:
    """解析一个视频链接；同平台用同一把锁串行。返回统一 dict（失败也返回 dict，含 notes）。"""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    platform = detect_platform(url)
    if not platform:
        return _empty(None, url, "不是支持的四家视频链接")
    key = _cache_key(url)
    hit = _RESULT_CACHE.get(key)
    if hit and time.time() - hit[0] < _RESULT_CACHE_TTL_S:
        return hit[1]
    async with _lock(platform):
        try:
            data = await asyncio.wait_for(
                _parse_impl(platform, url, cfg, on_meta),
                timeout=float(cfg["video_parse_timeout_s"]),
            )
        except asyncio.TimeoutError:
            data = _empty(platform, url, f"解析超时（>{int(cfg['video_parse_timeout_s'])} 秒）")
        except Exception as e:  # noqa: BLE001
            data = _empty(platform, url, f"解析失败（{type(e).__name__}）")
    if data.get("title") or data.get("subtitle") or data.get("comments") or data.get("image_urls"):
        _RESULT_CACHE[key] = (time.time(), data)
        if len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
            oldest = min(_RESULT_CACHE, key=lambda k: _RESULT_CACHE[k][0])
            _RESULT_CACHE.pop(oldest, None)
    return data


async def _parse_impl(platform: str, url: str, cfg: dict, on_meta=None) -> dict:
    if platform == "bilibili":
        return await _parse_bili(url, cfg, on_meta)
    if platform == "douyin":
        return await _parse_douyin(url, cfg, on_meta)
    if platform == "kuaishou":
        return await _parse_kuaishou(url, cfg, on_meta)
    return await _parse_ixigua(url, cfg, on_meta)


def estimate_minutes(data: dict, cfg: dict | None = None) -> int:
    """等待时长预估：有字幕≈1 分钟；要转写≈ceil(时长/300)+1 分钟。"""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    if data.get("subtitle"):
        return 1
    imgs = data.get("image_urls") or []
    if imgs:
        # 图文要逐张下载 + OCR：按 3 张/分钟估，最多 5 分钟
        return max(1, min(5, math.ceil(len(imgs) / 3)))
    if not data.get("media_urls"):
        return 1
    dur = _num(data.get("duration"))
    if not dur:
        return 3
    return max(2, math.ceil(dur / float(max(30, int(cfg.get("video_audio_max_s", 300)))) + 1))
