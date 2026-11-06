"""平台适配：把 wechatauto 注册进 AstrBot 平台管理器（主动/分条发送依赖它）。"""

from __future__ import annotations

import asyncio

from astrbot.api.platform import Platform, PlatformMetadata

from .chain import send_chain
from .io_client import IoWechatClient

PLATFORM = PlatformMetadata(
    name="wechatauto",
    description="PC 微信小号（wechatauto RPA）",
    id="wechatauto",
    support_streaming_message=False,
)


class WechatAutoPlatform(Platform):
    """注册给 AstrBot 平台管理器，让 context.send_message（主动/分条发送）也能落到微信。

    之前没注册这个实例，humanizer 分条发送的第二段会报
    「cannot find platform for session wechatauto:...」然后整段丢掉。
    """

    def __init__(self, client: IoWechatClient, event_queue) -> None:
        super().__init__({}, event_queue)
        self._client = client

    def meta(self) -> PlatformMetadata:
        return PLATFORM

    async def run(self) -> None:
        # 消息由 WechatPoller 轮询投递，这里只是占位，保持平台任务存活
        while True:
            await asyncio.sleep(3600)

    async def send_by_session(self, session, message_chain) -> None:
        await super().send_by_session(session, message_chain)
        session_id = str(session.session_id)
        await send_chain(self._client, session_id, message_chain)
