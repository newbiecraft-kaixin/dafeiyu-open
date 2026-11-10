"""信封队列：平台 IO 入队，管道长轮询取走。

两个 IO 服务共用同一套语义：
    q = EventQueue()
    seq = q.push(env)                 # 入队并返回递增序号
    last, items = q.since(seq, wait_ms)  # 长轮询，没有新事件时最多等 wait_ms
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Tuple


class EventQueue:
    def __init__(self, maxlen: int = 2000) -> None:
        self._items: Deque[Dict[str, Any]] = deque(maxlen=int(maxlen))
        self._seq = 0
        self._cond = threading.Condition()
        self._pushed = 0
        self._dropped = 0
        # 消费游标：只有 since() 取走的事件才算「已消费」。
        # 以前 stats 直接用 len(_items)，deque 保留最近 2000 条，看上去永远「堆积 2000」，
        # 实际上早就处理完了；游标才能反映真实待处理量。
        self._read_seq = 0

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq

    def push(self, env: Dict[str, Any]) -> int:
        with self._cond:
            self._seq += 1
            self._items.append({"seq": self._seq, "envelope": env})
            self._pushed += 1
            overflowed = len(self._items) == self._items.maxlen and self._seq > self._items.maxlen
            if overflowed:
                self._dropped = self._seq - self._items.maxlen
            self._cond.notify_all()
            return self._seq

    def since(self, since: int = 0, wait_ms: int = 0) -> Tuple[int, List[Dict[str, Any]]]:
        """返回 (当前序号, 序号大于 since 的事件)。wait_ms 内没有新事件就返回空。"""
        deadline = time.time() + max(0, int(wait_ms)) / 1000.0
        with self._cond:
            while self._seq <= since and wait_ms > 0:
                remain = deadline - time.time()
                if remain <= 0:
                    break
                self._cond.wait(min(remain, 1.0))
            items = [item for item in self._items if item["seq"] > since]
            if items:
                self._read_seq = max(self._read_seq, items[-1]["seq"])
            return self._seq, items

    def clear(self) -> None:
        with self._cond:
            self._items.clear()

    def stats(self) -> Dict[str, int]:
        with self._cond:
            backlog = sum(1 for item in self._items if item["seq"] > self._read_seq)
            return {
                # queued = 真正还没被消费的事件；retained = 仍在环形缓冲里的历史条数
                "queued": backlog,
                "retained": len(self._items),
                "seq": self._seq,
                "pushed": self._pushed,
                "dropped": self._dropped,
            }
