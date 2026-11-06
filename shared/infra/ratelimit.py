"""按会话的令牌桶限流：防止微信群刷屏、防止对同一好友连发。

    rl = RateLimiter(rate=1.0, burst=3)   # 每会话每秒 1 条，允许瞬发 3 条
    if not rl.allow(chat_id):
        wait = rl.retry_after(chat_id)
        time.sleep(wait)
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable, Dict


class _Bucket:
    __slots__ = ("tokens", "last")

    def __init__(self, tokens: float, last: float) -> None:
        self.tokens = tokens
        self.last = last


class RateLimiter:
    """每个 key（会话 id）一个令牌桶，线程安全。"""

    def __init__(
        self,
        *,
        rate: float = 1.0,
        burst: int | None = None,
        max_keys: int = 10000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate 必须大于 0")
        self.rate = float(rate)
        self.burst = float(burst if burst is not None else max(1, int(rate)))
        self.max_keys = int(max_keys)
        self.clock = clock
        self._buckets: "OrderedDict[str, _Bucket]" = OrderedDict()
        self._lock = threading.Lock()
        self._allowed = 0
        self._blocked = 0

    def _bucket(self, key: str, now: float) -> _Bucket:
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(self.burst, now)
            self._buckets[key] = b
            if len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
        else:
            elapsed = max(0.0, now - b.last)
            b.tokens = min(self.burst, b.tokens + elapsed * self.rate)
            b.last = now
            self._buckets.move_to_end(key)
        return b

    def allow(self, key: str, cost: float = 1.0, *, now: float | None = None) -> bool:
        if cost <= 0:
            return True
        t = self.clock() if now is None else now
        with self._lock:
            b = self._bucket(key, t)
            if b.tokens >= cost:
                b.tokens -= cost
                self._allowed += 1
                return True
            self._blocked += 1
            return False

    def retry_after(self, key: str, cost: float = 1.0, *, now: float | None = None) -> float:
        """还差多少秒才能放行；已可放行返回 0。"""
        t = self.clock() if now is None else now
        with self._lock:
            b = self._bucket(key, t)
            missing = cost - b.tokens
            if missing <= 0:
                return 0.0
            return missing / self.rate

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def stats(self) -> Dict[str, float]:
        with self._lock:
            return {
                "keys": len(self._buckets),
                "rate": self.rate,
                "burst": self.burst,
                "allowed": self._allowed,
                "blocked": self._blocked,
            }
