"""消息链拆解：只取正文，落成 io-wechat 的 /send。

微信侧只保留文字收发；@、引用、图片/文件/语音都需要额外窗口点击或转文件发送，
已从 io-wechat 移除，这里也一并忽略。
"""

from __future__ import annotations

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from .io_client import IoWechatClient


def chain_text(chain: MessageChain) -> str:
    """只取正文。At 不算正文：微信没有 At 消息段，塞进正文只会变成一串 "@wxid"。"""
    parts: list[str] = []
    for seg in chain.chain:
        if isinstance(seg, Plain) and seg.text:
            parts.append(seg.text)
    return "".join(parts).strip()


async def send_chain(
    client: IoWechatClient,
    session_id: str,
    message: MessageChain,
) -> None:
    """把一条 AstrBot 消息链落成 io-wechat 的 /send（仅文字）。"""
    text = chain_text(message)
    if not text:
        # 只有 At/Reply/媒体段的回合，不往微信里灌空白消息
        return
    await client.send_text(session_id, text)
