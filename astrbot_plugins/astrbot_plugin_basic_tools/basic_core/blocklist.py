"""拉黑/厌烦共享存储：basic_tools 写（AI tool + /拉黑 指令），human_sim 读。

黑名单是全局的（不分群）：被拉黑的人在窗口期内发什么消息都不回应。
落盘 data/blocklist.json，重启不清零；到期自动失效（human_sim 按 mtime
缓存增量重读，basic_tools 进程内缓存）。
"""

from __future__ import annotations

import json
import os
import threading
import time

from .compat import enter_plugin_main, exit_plugin_main, install_helpers

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())

BLOCKLIST_PATH = os.path.join(DATA_ROOT, "blocklist.json")
_LOCK = threading.Lock()
_CACHE = None


def _load() -> dict:
    try:
        with open(BLOCKLIST_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    global _CACHE
    _CACHE = d
    try:
        with open(BLOCKLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _items() -> dict:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load()
    return _CACHE


def set_block(uid, minutes, reason: str = "", by: str = "ai") -> int:
    """拉黑某人 minutes 分钟，返回实际生效的分钟数（1-720）。"""
    uid = str(uid or "").strip()
    if not uid:
        return 0
    try:
        mins = max(1, min(720, int(str(minutes).strip() or 30)))
    except (TypeError, ValueError):
        mins = 30
    with _LOCK:
        d = dict(_items())
        d[uid] = {
            "until": time.time() + mins * 60,
            "minutes": mins,
            "reason": str(reason or "").strip(),
            "by": by,
        }
        _save(d)
    return mins


def clear(uid) -> bool:
    with _LOCK:
        d = dict(_items())
        removed = d.pop(str(uid or "").strip(), None) is not None
        _save(d)
    return removed


def active(uid) -> dict | None:
    """还在拉黑窗口内则返回条目，否则 None（过期的顺便清掉）。"""
    with _LOCK:
        d = _items()
        ent = d.get(str(uid or "").strip())
        if not ent:
            return None
        if float(ent.get("until") or 0) <= time.time():
            d = dict(d)
            d.pop(str(uid or "").strip(), None)
            _save(d)
            return None
        return ent


def listing() -> list:
    now = time.time()
    with _LOCK:
        d = _items()
        return [
            (uid, ent) for uid, ent in d.items() if float(ent.get("until") or 0) > now
        ]


exit_plugin_main(globals(), _REAL_MODULE_NAME)
