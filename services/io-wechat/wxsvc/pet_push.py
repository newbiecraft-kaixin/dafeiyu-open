"""把带图/表情/GIF 的微信消息直接推给桌宠中转（pet-relay）。

正文消息走 AstrBot 桥接 -> dafeiyu_pet 插件推送；微信侧只保留文字收发，
图片/表情在桥接层不进 AstrBot 消息链，所以在这里补推 relay。
payload 结构与 dafeiyu_pet 插件一致：relay 取 images[0] 转成桌宠气泡图片，
GIF（微信以 emoji 消息发出，cdnurl 即动图直链）同样支持。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from urllib.parse import quote

from .config import PORT
from .util import log

RELAY_URL = "http://127.0.0.1:23334/state"
MIN_INTERVAL = 2.0  # 同一会话节流，别连刷桌宠
FAIL_COOLDOWN = 60.0  # relay 不在线就冷却，避免每条消息都白等一次超时

_last: dict[str, float] = {}
_cooldown_until = 0.0
_lock = threading.Lock()


def push_image_message(item: dict) -> None:
    """收到带图片/表情的消息时异步推给 relay；失败静默，不影响消息主链路。"""
    global _cooldown_until
    urls = [
        str(m.get("url") or "").strip()
        for m in (item.get("media") or [])
        if isinstance(m, dict)
        and str(m.get("kind") or "") == "image"
        and str(m.get("url") or "").strip().lower().startswith(("http://", "https://"))
    ]
    if not urls:
        return
    if time.monotonic() < _cooldown_until:
        return
    chat_key = str(
        item.get("chat_uid") or item.get("chat") or item.get("sender_uid") or ""
    )
    now = time.monotonic()
    with _lock:
        if now - _last.get(chat_key, 0.0) < MIN_INTERVAL:
            return
        _last[chat_key] = now
    chat_type = "group" if item.get("chat_type") == "group" else "private"
    chat_uid = str(item.get("chat_uid") or "")
    chat_name = str(item.get("chat") or "")
    text = str(item.get("content") or "").strip()
    sender_uid = str(item.get("sender_uid") or "")
    # 头像走本服务 /avatar（head_image.db 解密）；查不到时渲染端自动退回纯文字气泡
    sender_avatar = (
        f"http://127.0.0.1:{PORT}/avatar?id={quote(sender_uid, safe='')}"
        if sender_uid
        else ""
    )
    payload = {
        "source": "wechat",
        "state": "thinking",
        "event": "收到新消息，开始思考",
        # 不带 message 字段：让 relay 走 detail 分支，气泡才会拼「昵称：正文」前缀
        "detail": text or "发来一张图片",
        "chat_type": chat_type,
        "chat_name": chat_name,
        "group_id": chat_uid if chat_type == "group" else "",
        "group_name": chat_name if chat_type == "group" else "",
        "sender_name": str(item.get("sender") or ""),
        "sender_id": sender_uid,
        "sender_avatar": sender_avatar,
        "images": urls[:3],
    }
    threading.Thread(target=_post, args=(payload,), daemon=True).start()


def _post(payload: dict) -> None:
    global _cooldown_until
    try:
        req = urllib.request.Request(
            RELAY_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6):
            pass
    except Exception as exc:  # noqa: BLE001
        _cooldown_until = time.monotonic() + FAIL_COOLDOWN
        log("pet push failed:", f"{type(exc).__name__}: {exc}")
