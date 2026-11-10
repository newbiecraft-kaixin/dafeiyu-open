"""视频平台限速：每个平台同时只跑一个任务，两次请求至少间隔 60 秒。

从 main.py 拆出来；锁与上次请求时间这两份全局状态只归本模块所有。
"""

from __future__ import annotations

import asyncio

# 每个视频平台同时只跑一个任务，且两次请求至少间隔 60 秒，避免触发平台风控
_PLATFORM_LOCKS: dict[str, asyncio.Lock] = {}
_PLATFORM_LAST: dict[str, float] = {}
_PLATFORM_MIN_GAP = 60.0


def _platform_lock(key: str) -> asyncio.Lock:
    lock = _PLATFORM_LOCKS.get(key)
    if lock is None:
        lock = _PLATFORM_LOCKS[key] = asyncio.Lock()
    return lock
