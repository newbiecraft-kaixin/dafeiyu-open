"""后台附着微信窗口：断线不影响 HTTP 服务，附着成功后自动收消息。"""

from __future__ import annotations

import time

import wechatauto

from . import state, window
from .inbox import on_message
from .util import log


def attach_loop() -> None:
    shown = False
    while state.wx is None:
        try:
            client = wechatauto.WeChat()
            # 先记下附着时刻：AddListenAll 会把停机期间积压的消息补发一遍，
            # 这些消息的 create_time 早于此刻，由 inbox 按时间丢弃。
            state.attach_time = time.time()
            client.AddListenAll(on_message)
            state.wx = client
            window.tame_gui()
            state.bot_name = str(getattr(client, "nickname", "") or state.bot_name)
            state.bot_wxid = str(getattr(client, "_wxid", "") or "")
            log("attached to WeChat", f"bot={state.bot_name or '?'}")
            return
        except Exception as exc:  # noqa: BLE001
            if not shown:
                log("attach failed, retrying:", exc)
                shown = True
            time.sleep(5)
