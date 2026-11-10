"""失败重试 + 死信队列。

    policy = RetryPolicy()
    try:
        result = call_with_retry(lambda: send(...), policy=policy,
                                 on_error=lambda i, e: log(...))
    except RetryExhausted as e:
        dead.add(envelope, reason=str(e.last), attempts=e.attempts)
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    base: float = 0.5
    factor: float = 2.0
    max_delay: float = 8.0
    jitter: float = 0.2

    def delay(self, attempt_index: int, *, rnd: Callable[[], float] = random.random) -> float:
        """第 attempt_index 次失败后的等待秒数（从 0 开始计）。"""
        raw = min(self.max_delay, self.base * (self.factor ** attempt_index))
        if self.jitter > 0:
            raw *= 1.0 - self.jitter * rnd()
        return max(0.0, raw)

    def delays(self) -> List[float]:
        return [self.delay(i, rnd=lambda: 0.0) for i in range(max(0, self.attempts - 1))]


class RetryExhausted(RuntimeError):
    def __init__(self, attempts: int, last: BaseException) -> None:
        super().__init__(f"{attempts} 次尝试均失败: {last!r}")
        self.attempts = attempts
        self.last = last


def call_with_retry(
    fn: Callable[[], Any],
    *,
    policy: Optional[RetryPolicy] = None,
    on_error: Optional[Callable[[int, BaseException], None]] = None,
    should_retry: Optional[Callable[[BaseException], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """同步重试。第 n 次失败用于决定是否继续，用尽后抛 RetryExhausted。"""
    p = policy or RetryPolicy()
    attempts = max(1, int(p.attempts))
    last: Optional[BaseException] = None

    for i in range(attempts):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 - 需要保留原始异常
            last = exc
            if on_error:
                on_error(i + 1, exc)
            if should_retry and not should_retry(exc):
                raise
            if i == attempts - 1:
                break
            sleep(p.delay(i))

    assert last is not None
    raise RetryExhausted(attempts, last) from last


class DeadLetter:
    """有界死信队列，容纳永远发不出去的请求，供监控页拉取。"""

    def __init__(self, maxlen: int = 200) -> None:
        self.maxlen = int(maxlen)
        self._items: Deque[Dict[str, Any]] = deque(maxlen=self.maxlen)
        self._lock = threading.Lock()
        self._total = 0

    def add(
        self,
        envelope: Any = None,
        *,
        reason: str = "",
        attempts: int = 0,
        target: str = "",
    ) -> Dict[str, Any]:
        item = {
            "ts": time.time(),
            "target": target,
            "reason": str(reason)[:500],
            "attempts": int(attempts),
            "envelope": envelope,
        }
        with self._lock:
            self._items.append(item)
            self._total += 1
        return item

    def items(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._items)

    def drain(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = list(self._items)
            self._items.clear()
            return out

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"size": len(self._items), "maxlen": self.maxlen, "total": self._total}
