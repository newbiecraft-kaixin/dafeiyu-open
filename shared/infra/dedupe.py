"""去重：QQ 有精确 message_id，微信用「会话 + 发送者 + 内容 + 时间窗」启发式。

用法：
    d = Dedupe()
    if d.seen(env):      # True = 重复，直接丢弃
        return
    ...

TTL 到期自动清理，避免内存无界增长。
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional

from .config import DEDUPE_TTL_S, DEDUPE_WINDOW_S

# raw 里可能出现的消息 id 字段名
_ID_KEYS = ("message_id", "msg_id", "messageId", "msgId", "id")


def extract_message_id(env: Dict[str, Any]) -> str:
    """只从 raw 里取消息 id；chat.id 是会话 id，不能混用。"""
    raw = env.get("raw") or {}
    for key in _ID_KEYS:
        value = raw.get(key)
        if value:
            return str(value)
    return ""


def make_key(env: Dict[str, Any], *, window: int = DEDUPE_WINDOW_S) -> str:
    """优先 message_id；没有就退化成时间窗内的内容指纹。"""
    platform = str(env.get("platform") or "?")
    chat_id = str((env.get("chat") or {}).get("id") or "")
    mid = extract_message_id(env)
    if mid:
        return f"{platform}:{chat_id}:mid:{mid}"

    sender = env.get("sender") or {}
    who = str(sender.get("id") or "") or str(sender.get("name") or "")
    text = str(env.get("text") or "")
    ts = int(float(env.get("ts") or time.time()))
    bucket = ts // max(1, int(window))
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{platform}:{chat_id}:{who}:{digest}:{bucket}"


class Dedupe:
    """线程安全的 TTL 去重表。"""

    def __init__(
        self,
        *,
        ttl: float = DEDUPE_TTL_S,
        max_entries: int = 20000,
        window: int = DEDUPE_WINDOW_S,
        key_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = float(ttl)
        self.max_entries = int(max_entries)
        self.window = int(window)
        self.key_fn = key_fn or (lambda env: make_key(env, window=self.window))
        self.clock = clock
        self._items: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def seen(self, env: Dict[str, Any]) -> bool:
        """记录并判断是否重复；True 表示这条该丢。"""
        return self.seen_key(self.key_fn(env))

    def seen_key(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            expire = self._items.get(key)
            if expire is not None and expire > now:
                self._items.move_to_end(key)
                self._hits += 1
                return True

            self._items[key] = now + self.ttl
            self._items.move_to_end(key)
            self._misses += 1
            if len(self._items) > self.max_entries:
                self._prune_locked(now, force=True)
            return False

    def _prune_locked(self, now: float, *, force: bool = False) -> int:
        removed = 0
        while self._items:
            key, expire = next(iter(self._items.items()))
            if expire <= now:
                self._items.popitem(last=False)
                removed += 1
            else:
                break
        if force and len(self._items) > self.max_entries:
            overflow = len(self._items) - self.max_entries
            for _ in range(overflow):
                self._items.popitem(last=False)
                removed += 1
        return removed

    def prune(self) -> int:
        with self._lock:
            return self._prune_locked(self.clock())

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._items),
                "ttl_s": int(self.ttl),
                "window_s": self.window,
                "dup_hits": self._hits,
                "first_seen": self._misses,
            }
