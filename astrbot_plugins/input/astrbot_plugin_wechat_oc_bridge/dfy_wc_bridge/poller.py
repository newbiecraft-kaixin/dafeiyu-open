"""轮询层：拉取 io-wechat 消息、做过滤，把事件投进 AstrBot 队列。"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from astrbot.api import logger
from astrbot.api.star import Context

from .adapter import PLATFORM, WechatAutoPlatform
from .event import build_event
from .io_client import IoWechatClient


class WechatPoller:
    def __init__(self, context: Context, settings: dict) -> None:
        self.context = context
        self.settings = settings
        self.client = IoWechatClient(str(settings["io_base"]))
        self._seq = 0
        self._seen: deque[str] = deque(maxlen=300)
        self._task: asyncio.Task | None = None

    def _register_platform(self) -> None:
        """把 wechatauto 平台实例挂进 AstrBot 平台管理器。

        没有它，插件的 context.send_message(umo, ...) 会因为找不到平台而丢消息
        （humanizer 分条发送、主动消息都走这条路）。
        """
        pm = getattr(self.context, "platform_manager", None)
        if pm is None:
            return
        try:
            pm.platform_insts = [
                p for p in pm.platform_insts
                if getattr(getattr(p, "meta", lambda: None)(), "id", "") != PLATFORM.id
            ]
            pm.platform_insts.append(WechatAutoPlatform(self.client, pm.event_queue))
            logger.info("[wechat_bridge] 已注册平台实例 wechatauto")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 注册平台实例失败: {exc}")

    async def start(self) -> None:
        self._register_platform()
        await self._align_cursor()
        self._task = asyncio.create_task(self._pump())
        logger.info(f"[wechat_bridge] 已启动，io-wechat={self.client.base}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        await self.client.close()

    async def _align_cursor(self) -> None:
        """启动时对齐 seq，避免把服务里的历史消息重放一遍。"""
        try:
            self._seq = int((await self.client.messages(0)).get("seq") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 初始化游标失败: {exc}")

    async def _pump(self) -> None:
        warned = False
        while True:
            try:
                data = await self.client.messages(self._seq, wait_ms=2000)
                seq = int(data.get("seq") or 0)
                if seq < self._seq:
                    # io-wechat 重启后 seq 归零。这里必须重新对齐到「当前」seq，
                    # 不能把游标改成 0——那会把服务里保留的历史消息整批重放进
                    # AstrBot 队列，看起来就是消息大量堆积、机器人回复隔夜消息。
                    await self._align_cursor()
                    logger.warning("[wechat_bridge] 检测到 io-wechat 重启，已对齐游标，跳过历史消息")
                    continue
                for item in data.get("messages") or []:
                    self._seq = max(self._seq, int(item.get("seq") or 0))
                    await self._handle(item)
                self._seq = max(self._seq, seq)
                warned = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not warned:
                    logger.warning(f"[wechat_bridge] 轮询失败，稍后重试: {exc}")
                    warned = True
                await asyncio.sleep(3)

    def _accept(self, item: dict) -> tuple[bool, str, str]:
        """返回 (是否处理, chat_uid, text)。过滤规则集中在这里。"""
        if item.get("is_self"):
            return False, "", ""
        # 丢弃积压太久的消息：模型排队或服务重启时，队列里可能压着几分钟前的语气，
        # 再回复只会接错话。默认 120s，可在插件配置里调 max_message_age_seconds。
        max_age = int(self.settings.get("max_message_age_seconds") or 120)
        ts = int(item.get("ts") or 0)
        if ts and max_age > 0 and time.time() - ts > max_age:
            logger.warning(
                f"[wechat_bridge] 丢弃过期消息 {time.time() - ts:.0f}s "
                f"{item.get('sender') or ''}@{item.get('chat') or ''}"
            )
            return False, "", ""
        # io-wechat 的 chat 是显示名（会重名/会改），chat_uid 才是 wxid/群 id。
        # 回复必须按 uid 发，显示名只用来比较 allow_chats 和打日志。
        chat_uid = str(item.get("chat_uid") or "").strip()
        chat_name = str(item.get("chat") or "").strip()
        sender = str(item.get("sender") or "").strip()
        sender_uid = str(item.get("sender_uid") or "").strip()
        text = str(item.get("content") or "").strip()
        is_group = str(item.get("chat_type") or "") == "group"
        # 群消息的回复目标只能是群 id。缺群 id 时如果退回 sender_uid（群成员的
        # wxid），回复就会被当成私聊发给那个人，看起来就是「群里问、私聊答」。
        if is_group and not chat_uid.endswith("@chatroom"):
            return False, "", ""
        chat_uid = chat_uid or chat_name or sender_uid or sender
        if not chat_uid or not (sender or sender_uid) or not text:
            return False, "", ""
        # 表情/图片/链接等 appmsg 是 XML，直接跳过，别和别的机器人抢答
        if text.startswith("<msg") or "<appmsg" in text:
            return False, "", ""
        if is_group and not self.settings.get("enable_group", False):
            return False, "", ""
        allowed = [str(x).strip() for x in (self.settings.get("allow_chats") or [])
                   if str(x).strip()]
        if allowed and chat_uid not in allowed and chat_name not in allowed:
            return False, "", ""

        key = f"{chat_uid}\x1f{item.get('id') or item.get('seq')}\x1f{text[:24]}"
        if key in self._seen:
            return False, "", ""
        self._seen.append(key)

        max_chars = int(self.settings.get("max_chars") or 400)
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        return True, chat_uid, text

    async def _handle(self, item: dict) -> None:
        ok, chat, text = self._accept(item)
        if not ok:
            return
        self.context.get_event_queue().put_nowait(
            build_event(item, self.client, chat)
        )
        logger.info(
            f"[wechat_bridge] 收到 {item.get('sender_name') or item.get('sender')}"
            f"@{item.get('chat') or chat}({chat}): {text[:40]}"
        )
