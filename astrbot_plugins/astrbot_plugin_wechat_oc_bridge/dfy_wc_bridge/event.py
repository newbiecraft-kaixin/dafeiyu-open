"""事件层：io-wechat 消息 -> AstrBot 消息对象/事件，回复再落回 io-wechat。"""

from __future__ import annotations

import time

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
)

from .chain import send_chain
from .io_client import IoWechatClient
from .adapter import PLATFORM


class WechatAutoEvent(AstrMessageEvent):
    """把流水线的 event.send() 落成 io-wechat 的 /send。"""

    def __init__(self, message_str, message_obj, session_id, client: IoWechatClient):
        super().__init__(message_str, message_obj, PLATFORM, session_id)
        self._client = client

    async def send(self, message: MessageChain) -> None:
        await super().send(message)
        await send_chain(self._client, self.session_id, message)


def build_event(item: dict, client: IoWechatClient, session_id: str) -> WechatAutoEvent:
    chat = session_id
    chat_name = str(item.get("chat") or chat)
    sender_name = str(item.get("sender") or "").strip()
    sender_uid = str(item.get("sender_uid") or "").strip() or sender_name
    text = str(item.get("content") or "").strip()
    is_group = str(item.get("chat_type") or "") == "group"

    # 微信正文里的 “@机器人” 只是普通文本，AstrBot 的唤醒检查只认 At 消息段。
    # 这里按 io-wechat 给的 mentions 还原成 At，并把 @昵称 从正文里去掉，
    # 否则群里 @ 她不会唤醒，@ 完再接 “/听歌” 也会因为字符串开头不是指令而不匹配。
    bot_names = [
        str(m.get("name") or "")
        for m in (item.get("mentions") or [])
        if isinstance(m, dict) and m.get("is_bot") and str(m.get("name") or "")
    ]
    for name in bot_names:
        text = text.replace(f"@{name}", "", 1)
    text = text.strip()

    obj = AstrBotMessage()
    obj.type = MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE
    obj.self_id = "wechatauto"
    obj.session_id = chat
    obj.message_id = str(item.get("id") or item.get("seq") or int(time.time()))
    obj.group = Group(group_id=chat, group_name=chat_name) if is_group else None
    obj.sender = MessageMember(user_id=sender_uid, nickname=sender_name or sender_uid)
    obj.message = [At(qq=obj.self_id, name=name) for name in bot_names]
    obj.message.append(Plain(text))
    obj.message_str = text
    obj.raw_message = item
    return WechatAutoEvent(text, obj, chat, client)
