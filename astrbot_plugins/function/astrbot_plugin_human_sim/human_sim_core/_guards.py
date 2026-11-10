"""出站守卫：去重、过时回复丢弃、禁言/额度、认人纠正、指令排队与回复入史（从 pipeline.py 按职责拆出）。"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain

from .text import (
    _DUP_SEND_LAST,
    _DUP_SEND_WINDOW,
    _send_dedupe_key,
    _send_dedupe_text,
    _strip_tokens,
)

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class PipelineGuardsMixin:

    # ---------- 同一内容别发两遍（流水线偶尔把 result_decorate/respond 跑重复） ----------
    @filter.on_decorating_result()
    async def _dedupe_duplicate_send(self, event: AstrMessageEvent):
        try:
            text = _send_dedupe_text(event)
            if len(text) < 8:
                return
            event.set_extra("hs_last_text", text)
            key = _send_dedupe_key(event)
            now = time.time()
            prev = _DUP_SEND_LAST.get(key)
            # 只掐「确认已经发出去过」的那一条，避免把被丢弃的唯一结果也吞掉
            if prev and now - prev[0] <= _DUP_SEND_WINDOW and prev[1] == text:
                logger.info("[human_sim] 同一条回复刚发过，掐掉重复的第二次")
                event.clear_result()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 重复发送去重异常: {type(e).__name__}: {e}")

    @filter.after_message_sent()
    async def _remember_sent_text(self, event: AstrMessageEvent):
        try:
            # 主流水线的回复也算「开口」，供 _send 的连发守卫判断
            umo = getattr(event, "unified_msg_origin", "")
            if umo:
                now_ts = time.time()
                self.state[f"last_bot_say:{umo}"] = now_ts
                # 同步重置自主回话的冷却钟：正常回复已经说过了，
                # 水群模块别再按自己的 180 秒节点接着抢话（否则就是「刚说完又冒一句」）
                self.state[f"last_interject:{umo}"] = now_ts
            text = event.get_extra("hs_last_text") or _send_dedupe_text(event)
            if not text or len(text) < 8:
                return
            key = _send_dedupe_key(event)
            now = time.time()
            _DUP_SEND_LAST[key] = (now, text)
            for k, (ts, _) in list(_DUP_SEND_LAST.items()):
                if now - ts > 60:
                    _DUP_SEND_LAST.pop(k, None)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 主流水线回复也记进历史（被@后的正式回复），决策才能接自己的话头 ----------
    @filter.on_decorating_result()
    async def _drop_stale_reply(self, event: AstrMessageEvent):
        """消息排队太久才轮到处理：这条回复已经过时，直接吞掉。

        队列堆积时（本地模型超时重试、背景任务抢算力），旧消息会被拖到几分钟后
        才生成回复，发出去就是答非所问。这里按消息落地时间判断，超过阈值就不发。
        """
        try:
            ts = float(getattr(event.message_obj, "timestamp", 0) or 0)
            if ts <= 0:
                return
            limit = float(self.cfg.get("stale_reply_seconds", 90))
            age = time.time() - ts
            if limit > 0 and age > limit:
                logger.info(
                    f"[human_sim] 回复迟到 {age:.0f}s（阈值 {limit:.0f}s），丢弃这条过时回复"
                )
                event.stop_event()
        except Exception:  # noqa: BLE001
            pass

    @filter.on_decorating_result()
    async def _mute_and_quota_guard(self, event: AstrMessageEvent):
        """正式流水线的回复也过禁言 / 共享分钟额度：额度用完这条就不发了。

        多机器人在同群互相对话时，各自进程的 _rate 看不到对方的发送量，
        只有这份跨进程计数能把两边合起来算，避免两只机器人越聊越起劲。
        """
        try:
            umo = event.unified_msg_origin
            if self._is_private(umo) or self._is_slash_message(event):
                return
            chain = getattr(event.get_result(), "chain", None) or []
            if not chain:
                return
            if self._is_muted(umo):
                logger.info(
                    f"[human_sim] 禁言中，丢弃回复（{umo.rsplit(':', 1)[-1]}）"
                )
                event.stop_event()
                return
            if not self._shared_claim(umo):
                logger.info(
                    f"[human_sim] 全局本分钟发言已达上限"
                    f"（{self.cfg.get('shared_minute_cap', 20)} 条），丢弃回复"
                )
                event.stop_event()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 禁言/限流装饰异常: {type(e).__name__}: {e}")

    @filter.on_decorating_result()
    async def _guard_owner_address(self, event: AstrMessageEvent):
        """官方流水线的回复也过一遍认人：说话的不是主人，就不许喊主人。

        提示词能压住大部分情况，但群里真有人昵称就叫「主人」时，
        小模型还是会顺着名字喊；这里在发出前兜底改成对方自己的昵称。
        """
        try:
            uid = str(event.get_sender_id() or "")
            if not uid or uid in self._owner_uids():
                return
            chain = getattr(event.get_result(), "chain", None) or []
            name = (event.get_sender_name() or "").split("#")[0][:12]
            for c in chain:
                if isinstance(c, Plain) and c.text and "主人" in c.text:
                    fixed = self._fix_owner_address(c.text, uid, name)
                    if fixed != c.text:
                        c.text = fixed
                        logger.info("[human_sim] 官方回复里错喊的「主人」已改成对方昵称")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 认人纠正异常: {type(e).__name__}: {e}")

    @filter.on_decorating_result()
    async def _queue_slash_reply(self, event: AstrMessageEvent):
        """斜杠指令的回复也进出站队列：跟普通发言一样逐条排队，不再秒回、不再连发。

        以前只有主动消息走 SendGate，/状态、/看视频 这类指令是核心直接发出去的，
        同群两条指令就是同一秒连甩，和真人节奏对不上，也更容易触发风控。
        """
        try:
            if not self._is_slash_reply(event):
                return
            result = event.get_result()
            chain = getattr(result, "chain", None) or []
            if not chain or self._gate is None:
                return
            await self._gate.wait(event.unified_msg_origin)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 指令回复排队异常: {type(e).__name__}: {e}")

    @filter.on_decorating_result()
    async def _record_own_pipeline_reply(self, event: AstrMessageEvent):
        try:
            # /指令的回复（/状态、/余额 这类）不进滑动窗口：那是人对机器人下的命令，
            # 回复也是给命令发起者看的系统输出，混进历史里模型会把「大肥鱼：已重置」
            # 当成自己说过的话，跟着复读指令腔
            if self._is_slash_reply(event):
                return
            umo = event.unified_msg_origin
            if umo not in self.cfg["watch_groups"]:
                return
            result = event.get_result()
            chain = getattr(result, "chain", None) or []
            text = "".join(
                (getattr(c, "text", "") or "") for c in chain
            ).strip()
            if not text and not self._describe_chain_media(chain):
                return
            # 记历史前先抹掉 :名称:，否则模型会照抄自己的旧消息，一直重复占位符；
            # 但表情本身要留一条带意思的动作记录，否则下一轮它不知道自己发过什么
            clean = _strip_tokens(text).strip()
            if clean:
                self._history.setdefault(umo, deque(maxlen=30)).append(
                    f"大肥鱼：{clean[:80]}"
                )
            media = self._describe_chain_media(chain) + self._describe_tokens(text)
            media = list(dict.fromkeys(media))
            if media:
                self._note_act(umo, f"发了表情包：{'、'.join(media[:2])}")
            # 正式回复后也概率给提问者的消息贴表情回应（旧随机路径，默认关闭）
            mid = getattr(event.message_obj, "message_id", None)
            if mid and self.cfg.get("react_random_decide", False) and random.random() < float(
                self.cfg.get("react_after_reply_probability", 0.25)
            ):
                asyncio.create_task(
                    self._delayed_react(umo, mid, random.uniform(0.8, 3.0))
                )
            # 回完人顺手拍他一下（概率很低，纯本地，不额外调模型）
            sender = str(event.get_sender_id() or "")
            if sender.isdigit():
                # 正式流水线回完这个人：也开一段专项关注，之后 TA 接着说别被概率漏掉
                self._followup_start(umo, sender)
                asyncio.create_task(
                    self._maybe_active_poke(
                        umo, sender, "reply", getattr(event, "bot", None)
                    )
                )
        except Exception:  # noqa: BLE001
            pass
