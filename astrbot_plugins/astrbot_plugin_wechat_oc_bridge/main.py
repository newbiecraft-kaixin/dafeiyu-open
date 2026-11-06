"""插件入口：只做装配与生命周期，具体实现见 dfy_wc_bridge 包。

分层：
    settings  配置
    io_client io-wechat 的 /messages、/send
    chain     消息链拆解与发送落地
    adapter   平台注册（主动/分条发送）
    event     消息 -> AstrBot 事件
    poller    轮询、过滤、投递事件队列
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

from dfy_wc_bridge.poller import WechatPoller
from dfy_wc_bridge.settings import load_settings


@register(
    "astrbot_plugin_wechat_oc_bridge",
    "dafeiyu-local",
    "微信小号桥接：io-wechat 收发接入 AstrBot",
    "v1.0.0",
)
class WechatBridge(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        settings = load_settings(_HERE)
        self._poller = WechatPoller(context, settings) if settings.get("enabled", True) else None

    async def initialize(self) -> None:
        if self._poller is None:
            logger.info("[wechat_bridge] 已在 config.json 中禁用")
            return
        await self._poller.start()

    async def terminate(self) -> None:
        if self._poller is not None:
            await self._poller.stop()
