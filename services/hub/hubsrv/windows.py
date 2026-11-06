"""主动私聊后的「监听窗口」。

场景：在 A 群让 AI 主动私聊某人后，A 群的 AI 需要在接下来一段时间内
同时盯着「这个人的私聊」和「A 群」，直到窗口结束。

窗口只保存关联关系，不复制消息内容；到点自动失效。
默认时长 300 秒，可用环境变量 LISTEN_WINDOW_SECONDS 覆盖。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from infra import fail, ok

from .config import DATA_DIR

STORE = DATA_DIR / "listen_windows.json"
DEFAULT_SECONDS = max(1, int(os.environ.get("LISTEN_WINDOW_SECONDS") or 300))
_LOCK = threading.Lock()


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        raw = STORE.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(items: Dict[str, Dict[str, Any]]) -> None:
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _prune(items: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    now = time.time()
    return {k: v for k, v in items.items() if float(v.get("until") or 0) > now}


def start(
    peer_id: str,
    group_id: str = "",
    group_umo: str = "",
    seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """开启/续期一个监听窗口，key 是私聊对象 ID。"""
    peer = str(peer_id or "").strip()
    if not peer:
        raise ValueError("peer_id 不能为空")
    now = time.time()
    span = max(1, int(seconds or DEFAULT_SECONDS))
    with _LOCK:
        items = _prune(_load())
        window = {
            "peer_id": peer,
            "group_id": str(group_id or ""),
            "group_umo": str(group_umo or ""),
            "started_at": now,
            "until": now + span,
            "seconds": span,
        }
        items[peer] = window
        _save(items)
    return window


def stop(peer_id: str) -> bool:
    peer = str(peer_id or "").strip()
    with _LOCK:
        items = _prune(_load())
        existed = items.pop(peer, None) is not None
        _save(items)
    return existed


def get(peer_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        items = _prune(_load())
        _save(items)
        return items.get(str(peer_id or "").strip())


def snapshot() -> List[Dict[str, Any]]:
    now = time.time()
    with _LOCK:
        items = _prune(_load())
        _save(items)
        rows = []
        for window in items.values():
            row = dict(window)
            row["left_s"] = max(0, round(float(window.get("until") or 0) - now))
            rows.append(row)
    rows.sort(key=lambda r: r.get("until", 0))
    return rows


# ---------- HTTP handlers ----------

def handle_list(_req: Any) -> Any:
    return ok({"default_seconds": DEFAULT_SECONDS, "items": snapshot()})


def handle_start(req: Any) -> Any:
    body = req.payload if isinstance(req.payload, dict) else {}
    try:
        window = start(
            str(body.get("peer_id") or body.get("peer") or ""),
            str(body.get("group_id") or ""),
            str(body.get("group_umo") or ""),
            body.get("seconds"),
        )
    except (TypeError, ValueError) as error:
        return fail(str(error), 400)
    return ok(window)


def handle_stop(req: Any) -> Any:
    body = req.payload if isinstance(req.payload, dict) else {}
    peer = str(body.get("peer_id") or body.get("peer") or "")
    return ok({"stopped": stop(peer), "peer_id": peer})
