"""消息流水线：记录、去重、守卫、决策与生成。"""

from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import (
    At,
    Face,
    File,
    Forward,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)

from .config import LOCAL_DECIDE_PROVIDER
from .prompts import SYSTEM_PROMPT
from .text import BALANCE_ERR_RE, HALLUCINATED_MEDIA_RE, _DUP_SEND_LAST, _DUP_SEND_WINDOW, _FY_CONTENT_RE, _IDENTITY_TAG_RE, _MIX_TOKEN_RE, _QUOTE_RE, _STICKER_REQUEST_RE, _TOKEN_RE, _clean_quote, _cut_at_sentence_boundary, _send_dedupe_key, _send_dedupe_text, _strip_tokens
from .runtime import filters

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class PipelineMixin:

    # ---------- 收集群消息 ----------
    def _is_slash_reply(self, event: AstrMessageEvent) -> bool:
        """这条结果是斜杠指令的回话吗（只看原文，不掐事件）。

        和 _is_slash_message 的区别：那个会 stop_event 掐掉没人接的指令，
        出站装饰阶段再调用会把正常的指令回复也一起丢掉，所以这里单独判一次。
        """
        try:
            raw = _IDENTITY_TAG_RE.sub(
                "",
                "".join(
                    (getattr(c, "text", "") or "")
                    for c in (event.message_obj.message or [])
                    if isinstance(c, Plain)
                ),
            ).strip()
        except Exception:  # noqa: BLE001
            return False
        return raw.startswith("/")

    def _is_slash_message(self, event: AstrMessageEvent) -> bool:
        """以 / 开头的是指令：返回 True 表示这条不归消息层管。

        唤醒阶段已经把 message_str 里的前缀去掉了，所以要看消息链里的原文；
        没有命令接得住的（打错/瞎敲）直接掐事件，别让 AI 去接「余额」「tool」这种词。
        """
        try:
            raw = _IDENTITY_TAG_RE.sub(
                "",
                "".join(
                    (getattr(c, "text", "") or "")
                    for c in (event.message_obj.message or [])
                    if isinstance(c, Plain)
                ),
            ).strip()
        except Exception:  # noqa: BLE001
            return False
        if not raw.startswith("/"):
            return False
        for h in event.get_extra("activated_handlers", []) or []:
            for f in getattr(h, "event_filters", None) or []:
                name = (getattr(f, "__class__", type(f)).__name__ or "").lower()
                if "command" in name or "regex" in name:
                    return True
        event.stop_event()
        return True

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def _guard_private_commands(self, event: AstrMessageEvent):
        """私聊里以 / # ！ 开头的指令同样只交给命令处理器，不喂模型"""
        self._is_slash_message(event)

    # 监听窗口：主动私聊后 300 秒内，把「这个人」和「原群」当成同一条线。
    _WIN_CACHE = {"ts": 0.0, "items": []}

    def _active_listen_window(self, peer_id: str) -> dict | None:
        """查 hub 的监听窗口；5 秒缓存，hub 不在线就当没有。"""
        now = time.time()
        cache = self._WIN_CACHE
        if now - float(cache.get("ts") or 0) > 5:
            try:
                import json as _json
                import urllib.request

                with urllib.request.urlopen(
                    "http://127.0.0.1:23408/windows", timeout=1.5
                ) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
                cache["items"] = ((data or {}).get("data") or {}).get("items") or []
            except Exception:  # noqa: BLE001
                cache["items"] = []
            cache["ts"] = now
        peer = str(peer_id or "")
        for item in cache.get("items") or []:
            if str(item.get("peer_id") or "") == peer:
                return item
        return None

    def _absorb_listen_window(self, window: dict, text: str, event) -> None:
        """窗口内：私聊内容并入原群上下文；需要时用原群身份接话。"""
        group_umo = str(window.get("group_umo") or "")
        if not group_umo:
            return
        sender = str(event.get_sender_name() or window.get("peer_id") or "")
        self._history.setdefault(group_umo, deque(maxlen=30)).append(
            f"{sender}（私聊）：{text[:120]}"
        )
        if bool(self.cfg.get("listen_window_reply_group", True)):
            asyncio.create_task(self._reply_in_listen_window(group_umo, text))

    async def _reply_in_listen_window(self, group_umo: str, text: str) -> None:
        try:
            prompt = (
                "对方在私聊里接着说：\n"
                f"{text[:300]}\n"
                "你正在原群里和他继续这个话题。用一句自然的话接住"
                "（不要提「私聊」两个字），"
                f"不超过{int(self.cfg.get('max_chars', 60))}字。"
            )
            reply = await self._generate(prompt, group_umo)
            reply = self._humanize(reply)
            if reply:
                await self._send(group_umo, [Plain(reply)])
                self._note_act(group_umo, reply, spoken=True)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 监听窗口接话失败: {type(e).__name__}: {e}")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def _record_private(self, event: AstrMessageEvent):
        """记下谁私聊过我、聊到哪儿了——主动问候 / 话题追问的数据来源。"""
        try:
            umo = event.unified_msg_origin
            text = (event.message_str or "").strip()
            qq = str(event.get_sender_id() or "")
            if not text or not qq.isdigit() or qq == str(event.get_self_id() or ""):
                return
            if text.startswith(("/", "#", "!")):
                return
            # 没加好友的陌生人/临时会话不记账：免得之后主动去私聊一个陌生人
            if self._private_is_stranger(event):
                return
            rec = self.state.setdefault("pm_last", {}).setdefault(umo, {})
            rec["qq"] = qq
            rec["ts"] = time.time()
            lines = rec.setdefault("lines", [])
            lines.append(text[:80])
            del lines[:-6]  # 只留最近 6 条，够接话头就行
            self._save_state()
            window = self._active_listen_window(qq)
            if window:
                self._absorb_listen_window(window, text, event)
        except Exception:  # noqa: BLE001
            pass

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def _record(self, event: AstrMessageEvent):
        try:
            umo = event.unified_msg_origin
            # 顺手学表情：群里出现的 face id → 名字，补 face_config 没跟上的新包
            self._learn_from_event(event)
            if self._is_slash_message(event):
                return
            # 禁言期间不再在这里掐事件：输入层（识图/语音/记忆/记录）照常处理，
            # 只在模型层（_mute_guard_before_llm）和发送层（_send / 装饰结果）截断，
            # 这样「闭嘴」只是不接话，消息本身不丢。
            # 点赞只走 /赞我 命令；输入层自然语言点赞链路已永久关闭
            # 点单：先让模型按当前聊天场景挑发哪个（梗图 or 超级表情）；
            # 模型挑不出来才落到下面的硬规则兜底
            if await self._handle_emoji_request(event):
                event.stop_event()
                return
            # 「发个超级表情 / 发个表情 / 来张图」这类点单：任何群都认，本地直接发。
            # 必须在 watch_groups 判断之前——以前放在后面，没在潜水池里的群（比如
            # 1000000001）喊「发个超级表情」，请求根本没到处理函数，模型只回了句空话
            if await self._handle_super_face_request(event):
                event.stop_event()
                return
            if await self._handle_sticker_request(event):
                event.stop_event()
                return
            if umo not in self.cfg["watch_groups"]:
                return
            gid = str(event.get_group_id() or "")
            if gid and gid == str(event.get_self_id()):
                return  # 自己发的
            # 拍一戳会以「消息」形态溜进来：message_str 是 [ComponentType.Poke] 占位串，
            # 非空 → 骗过下面的 if not text → 决策层照常打分（19 字 ≥15 →「说了不少」+1.2）
            # → AI 拿着乱码开始演「被召唤」。戳一戳由 _on_poke 专责处理；
            # 这里只把「谁拍了谁」记进上下文（此前整体丢弃，AI 对拍一拍零记忆、没目标主体）。
            poke_comp = None
            try:
                for c in (event.message_obj.message or []):
                    if type(c).__name__ == "Poke":
                        poke_comp = c
                        break
            except Exception:
                poke_comp = None
            if poke_comp is not None:
                poker = str(event.get_sender_id() or "")
                if poker and poker != str(event.get_self_id() or ""):
                    # 机器人自己拍的回显（poker=自己）已由 _do_poke 成功路径记录，这里跳过防重复
                    try:
                        bot_ref = getattr(event, "bot", None)
                        if bot_ref is not None:
                            self._bots[umo] = bot_ref
                        target = str(poke_comp.target_id() or "")
                        if target:
                            pn = await self._member_name(umo, poker)
                            tn = await self._member_name(umo, target)
                            # 历史统一「发言人(QQ)：内容」，拍的人才是发言人；
                            # 目标昵称里带「主人」只是名字，剔掉免得被当成主人
                            tn = re.sub(r"主人", "", str(tn or "")).strip() or "群友"
                            line = f"{self._who_label(pn, poker)}：[拍了拍 {tn}]"
                            hist = self._history.setdefault(umo, deque(maxlen=30))
                            if not hist or hist[-1] != line:  # 连拍去重
                                hist.append(line)
                                logger.info(f"[human_sim] {line}（{umo.rsplit(':', 1)[-1]}）")
                    except Exception as e:  # noqa: BLE001
                        logger.debug(f"[human_sim] 拍一拍记录失败: {type(e).__name__}: {e}")
                # 双保险：_on_poke 已按死唤醒标记；万一它没跑到（插件加载顺序变了），
                # 这里也按死一次，保证拍一戳不会被 LLM 当成一句话接走
                event.is_at_or_wake_command = False
                event.is_wake = False
                return
            # 自己消息的群聊回显：sender 就是自己。上面的 gid==self_id 只挡得住
            # 私聊形态的回显，群聊回显必须比对 sender——否则复读/决策都拿自己的
            # 旧发言当「别人说的话」，自问自答。
            if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
                return
            text = _IDENTITY_TAG_RE.sub("", (event.message_str or "").strip()).strip()
            # 输入层统一清掉系统说明（face_convert 写的「[对方发来…]」「（喊了你一声）」）：
            # 下游的接龙、复读、决策、AI 都不该把这些当成群友说的话
            if self._is_system_desc(text):
                text = ""
            # 「还剩多少额度 / 余额」：本地查官方余额接口，直接回一句，不进 AI
            if await self._handle_balance_request(event):
                event.stop_event()
                return
            # 引用分两种情况：
            # ① 消息链的 Reply 组件能拿到原作者和原话 → 引用单独起一行（带真实 QQ 归属）
            # ② 拿不到（只有「[引用消息]」占位）→ 退化成一句「（引用了上一条旧消息）」
            quote_bits = await self._quote_bits(event)
            if quote_bits:
                text = _QUOTE_RE.sub("", text).replace("[引用消息]", "").strip()
            else:
                text = _clean_quote(text, self._bot_names())
            # 图片消息也得进复读判断（群里刷同一张图是最典型的「一起玩」）
            img_url = ""
            try:
                for c in event.message_obj.message or []:
                    if isinstance(c, Image):
                        cand = str(getattr(c, "url", "") or getattr(c, "file", "") or "")
                        if cand.startswith(("http://", "https://")):
                            img_url = cand
                            break
            except Exception:  # noqa: BLE001
                pass
            # —— +1 接龙：输入层机械拦截，不走 AI 决策 ——
            # 接龙没被打断之前不插别的话（打断之后自然恢复正常说话）。
            now_ts = datetime.now().timestamp()
            plus = "none"
            named_now = any(n and n in text for n in (self.cfg.get("bot_names") or []))
            mkey = self._media_fingerprint(
                event.message_obj.message or [],
                getattr(event.message_obj, "raw_message", None),
            )
            # 接龙素材只认真人手打的 Plain 文本。message_str 会被 face_convert 这类
            # 插件改写成「[对方发来一个表情包（可能想逗你/跟你斗…）]」，照抄的话
            # 群里看到的就是一大堆括号说明 —— 纯媒体消息一律改用指纹（发原图/原表情）。
            typed = "".join(
                (getattr(c, "text", "") or "")
                for c in (event.message_obj.message or [])
                if isinstance(c, Plain)
            ).strip()
            if self._is_system_desc(typed):
                typed = ""
            # 文字优先：同一条「文字+图」被不同人转发时，QQ/群文件常常重新编码，
            # 图片 md5 每次都不一样，只按图去比永远对不上 —— 这也是「文字+图片
            # 接不了龙」的根因。有真人手打的字就按文字比，纯图/表情才退化成媒体指纹。
            if typed and len(typed) <= 80:
                pkey = filters.norm(typed) if filters is not None else typed
            elif mkey:
                pkey = mkey
            else:
                pkey = ""
            # 留着这条消息的完整组件链（文字+表情+图），接龙时整条原样重发。
            # 只留表情/图的话，「文字+表情」会被跟成只剩表情，两段就对不上了
            material_now = [
                c
                for c in (event.message_obj.message or [])
                if isinstance(c, (Image, Face))
                or (isinstance(c, Plain) and (c.text or "").strip())
            ][:8]
            adjacent = (
                self._at_self(event)
                or named_now
                or text.startswith(("/", "#", "！", "!"))
            )
            snd = str(event.get_sender_id() or "")
            prev = self._plus1_prev.get(umo)
            prev_key = str(prev[0]) if prev else ""
            if not adjacent:
                plus = self._plus1_touch(
                    umo, pkey, typed, now_ts, snd, prev, material_now
                )
                same = bool(pkey) and pkey == prev_key
                # 输入层总闸：上下两句一模一样（同一个人连发也算）就是接龙。
                # 先跟着发（用上一条的原文/原图），再把这条记成新的上一条；
                # 之后复读 / 决策 / AI 一律不许再跑，只有内容不同才继续往下走。
                if same or plus == "join":
                    # plus == "join" 也要认：有人直接打「+1 / 1 / +10086」时，
                    # 这条消息的 pkey 是暗号本身，和上一条的 key 天然对不上，
                    # 只靠 same 会永远不跟 —— 上一条是「文字+图片」时最明显。
                    if plus == "join":
                        await self._plus1_echo(event, umo)
                    if same and pkey:
                        self._plus1_prev[umo] = (pkey, snd, typed)
                        # 空手上阵的「+1」不该把接龙素材清掉，否则后面只能靠猜
                        if material_now:
                            self._plus1_material[umo] = (pkey, material_now)
                    event.stop_event()
                    return
                if pkey:
                    self._plus1_prev[umo] = (pkey, snd, typed)
                    if material_now:
                        self._plus1_material[umo] = (pkey, material_now)
            elif self._plus1_state(umo, now_ts):
                # 有人直接 @/叫名字/发指令找我，这条就是打断
                self.state.pop(f"plus1:{umo}", None)
                logger.info(f"[human_sim] +1 接龙被打断（群 {umo.rsplit(':', 1)[-1]}）")
            # 复读：先记账再判断，命中就发完走人（全本地，0 token）
            if await self._maybe_repeat(event, umo, text, img_url, typed):
                event.stop_event()
                return
            # 纯图 / 表情包 / 视频 / 文件：没人会对着别人甩的图正经写字回复，那是客服不是群友。
            # message_str 在这些消息上可能整条是空的（face_convert 不再塞「表情包」占位串），
            # 所以不能看 text 判空，只能看消息链里到底有没有人真打了字。
            chain = event.message_obj.message or []
            has_media = any(
                isinstance(c, (Image, Face, Record, Video, File, Forward, Nodes))
                for c in chain
            )
            said = "".join(
                (c.text or "") for c in chain if isinstance(c, Plain)
            ).strip()
            if not text and not has_media and not quote_bits:
                return
            if has_media and not said and not _FY_CONTENT_RE.search(text):
                # 表情包 / 黄脸表情单独走一条路：不写字，直接斗图回一个表情。
                # 普通照片（没字的图）仍然按纯媒体处理，免得每条图都硬回一张。
                is_sticker = bool(event.get_extra("_fy_sticker_hint")) or bool(
                    re.search(r"表情包|动画表情", text)
                ) or any(isinstance(c, Face) for c in chain)
                if is_sticker:
                    await self._maybe_sticker_back(event, umo)
                logger.info(
                    f"[human_sim] 纯媒体消息不接话（{umo.rsplit(':', 1)[-1]}）"
                    f"：{text[:30] or '[空]'}"
                )
                # 复读和表情回应上面已经处理过，这里只把「群里又热闹了一下」
                # 记进时间线，别让冷场判定误以为没人说话
                try:
                    now_ts = datetime.now().timestamp()
                    hist = self._history.setdefault(umo, deque(maxlen=30))
                    nm = (event.get_sender_name() or "").split("#")[0][:12]
                    line = f"{self._who_label(nm, str(event.get_sender_id() or ''))}：[图片]"
                    if not hist or hist[-1] != line:
                        hist.append(line)
                    self._msg_ts.setdefault(umo, deque(maxlen=600)).append(now_ts)
                    self.state[f"last_msg:{umo}"] = now_ts
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[human_sim] 纯媒体记录失败: {type(e).__name__}: {e}")
                return
            # 「发我头像」这种明确请求：本地甩图，连模型都不叫
            if await self._handle_avatar_request(event):
                event.stop_event()
                return
            # 「拍拍我 / 戳我」这类纯动作请求：本地拍回去就完事，不惊动 AI
            if await self._handle_poke_request(event):
                event.stop_event()
                return
            hist = self._history.setdefault(umo, deque(maxlen=30))
            name = (event.get_sender_name() or "").split("#")[0][:12]
            sender_label = self._who_label(name, str(event.get_sender_id() or ""))
            if quote_bits:
                # 引用内容不进历史，只在发言人这一行留个标记：
                # 旧话原文交给 _decide 的【引用】块。这样历史每行仍是「发言人：内容」，
                # 不会被多出来的 ↳ 行撑乱、也不会让模型把旧话当成新的聊天内容。
                said = (text[:60] + "（引用了旧消息）") if text else "（只发了引用，没打字）"
                hist.append(f"{sender_label}：{said}")
            else:
                # 空文本（只 @ / 只发图）别写成「昵称：」这种半截行，
                # 模型看到空行会自己脑补一句，越描越乱
                hist.append(f"{sender_label}：{text[:80] if text else '（没打字，只发了个动作）'}")
            self._msg_ts.setdefault(umo, deque(maxlen=600)).append(datetime.now().timestamp())
            # 记住 bot 客户端和最后发言者（@人 / 群成员列表用）
            bot_ref = getattr(event, "bot", None)
            if bot_ref is not None:
                self._bots[umo] = bot_ref
            self.state[f"self_id:{umo}"] = str(event.get_self_id())
            self.state[f"last_sender:{umo}"] = str(event.get_sender_id())
            if ":GroupMessage:" in umo:
                sid_now = str(event.get_sender_id() or "")
                if sid_now.isdigit():
                    self.state[f"last_group_of:{sid_now}"] = umo
            # 记录群里最后一次有人说话的时间（冷场判定用），30 秒节流落盘
            self.state[f"last_msg:{umo}"] = datetime.now().timestamp()
            now_ts = datetime.now().timestamp()
            if now_ts - float(self._last_save) > 30:
                self._last_save = now_ts
                self._save_state()
            # 看到你说话也不戳你：没在跟你聊天，戳人很冒犯（只在回完你的话后顺手拍）
            sender_id = str(event.get_sender_id() or "")

            # —— 回话决策：AI 自主决定，或退回旧的随机概率 ——
            # 同一发送者短时间反复 @/引用 → 限流，掐断两只 AI 互 @ 的循环
            if (
                event.is_at_or_wake_command or quote_bits
            ) and self._sender_rate_limited(umo, sender_id):
                event.stop_event()
                return
            if not self.cfg["enabled"]:
                return
            if event.is_at_or_wake_command or text.startswith(("/", "#", "！!")):
                # 被 @ 就是在找你：开一段专项关注窗口，之后 TA 接着说别被概率抽样漏掉
                if event.is_at_or_wake_command and sender_id:
                    self._followup_start(umo, sender_id)
                return  # 被 @ / 指令走正式流水线，不抢；is_wake 对所有群消息恒为 True（插件 handler 会置位），不能用作判据
            # 纯链接没 @ 也没叫名字：只留在历史里，不当成点单去解析（省开销）
            url_only = bool(re.search(r"https?://", text)) and not re.sub(
                r"https?://\S+", "", text
            ).strip(" 　")
            if url_only and not any(
                n and n in text for n in (self.cfg.get("bot_names") or ())
            ):
                logger.info(f"[human_sim] 纯链接没点名，先不解析（{umo.rsplit(':', 1)[-1]}）")
                return
            if bool(self.cfg.get("ai_decide")):
                # 点名检测：带机器人名字/外号，或引用的就是你自己 = 在找我。
                # 放在防刷屏之前：被叫到名字时群里再热闹也得先接住。
                directed = any(
                    n and n in text for n in (self.cfg.get("bot_names") or ())
                ) or bool(
                    quote_bits
                    and (
                        quote_bits[0].startswith("你自己")
                        or any(
                            n and n in quote_bits[0]
                            for n in (self.cfg.get("bot_names") or ())
                        )
                    )
                )
                # 刚被找过：接下来一段时间进入专项关注，只看 TA 的消息
                if directed:
                    self._followup_start(umo, sender_id)
                followup = False
                if not directed and self._followup_window(umo, sender_id) is not None:
                    if not self._followup_still_talking(umo, sender_label):
                        # 群里已经换人说话了：窗口里那句「专门盯你」过期，
                        # 退回普通概率，别硬插进 TA 跟别人的对话
                        self.state.pop(f"followup:{umo}", None)
                    elif self._followup_gap_ok(umo, sender_id):
                        followup = True
                    else:
                        # 窗口内但刚为 TA 决策过：这条先不重复接，等下一句
                        return
                # 输入端防刷屏：30 秒内 >6 条视为刷屏/斗图，真人此时只会潜水
                ts_list = self._msg_ts.get(umo)
                if ts_list and not directed and not followup:
                    now = datetime.now().timestamp()
                    recent = sum(1 for t in ts_list if now - t <= 30)
                    if recent > 6:
                        logger.info(f"[human_sim] 刷屏中（30s 内 {recent} 条），潜水不决策")
                        return
                # 夜间降频（不熄灯）：自主插话概率 × night_frequency_scale；点名不受影响——真人半夜被叫名字也会应一声
                # 人群系数：人越多越容易被淹、越容易刷屏 → 压低；群里冷清 → 抬高（点名跳过这两层）
                if not directed and not followup:
                    p = (
                        float(self.cfg.get("ai_decide_probability", 0.5))
                        * self._night_scale()
                        * await self._audience_scale(umo)
                    )
                    # 话题跟自己有关（聊机器人、问我）→ 提高接话概率，别装没看见
                    p = self._self_topic_boost(text, p)
                    # 沉默越久越容易开口（最长到 100%），一发言就重置
                    p = self._ramp_probability(umo, p)
                    if random.random() > p:
                        return
                umo_inflight = umo in self._deciding
                last_check = float(self.state.get(f"last_decision_check:{umo}", 0) or 0)
                now = datetime.now().timestamp()
                recent = 0
                if ts_list:
                    recent = sum(1 for t in ts_list if now - t <= 30)
                # —— 本地决策层：先本地判，能判死的就不叫模型 ——
                # 省下的是一次完整请求（最长上下文 + 决策提示词），比省 token 值钱
                force_reply = False
                if self.cfg.get("decision_layer", True) and filters is not None:
                    last_bot = ""
                    for line in reversed(list(self._history.get(umo, []))):
                        if line.startswith("大肥鱼："):
                            last_bot = line[4:]
                            break
                    v = filters.score(
                        text,
                        directed=directed,
                        bot_names=tuple(self.cfg.get("bot_names") or ()),
                        bot_recent=last_bot,
                        interest=self.state.get(f"interest:{umo}") or {},
                        msg_per_30s=recent,
                        has_image=bool(img_url),
                        has_text=bool(text),
                        is_repeat=False,
                    )
                    self.state[f"last_verdict:{umo}"] = v.summary
                    if v.action == "silent" and not directed and random.random() < float(
                        self.cfg.get("silent_keepalive_probability", 0.12)
                    ):
                        # 判无声的留一点点「今天想插嘴」的余量，不然平淡群里她会变成哑巴
                        v.action = "ask"
                        v.reasons.append("保留抽样")
                    if followup and v.action == "silent":
                        # 跟进窗口里不本地判死：TA 刚找过你，后面这句值得让模型看一眼
                        # （模型仍可自己选 [SILENT]，这里只是不提前掐掉）
                        v.action = "ask"
                        v.reasons.append("跟进窗口")
                    self._verdict_stats[v.action] = self._verdict_stats.get(v.action, 0) + 1
                    if v.action == "silent":
                        logger.info(
                            f"[human_sim] 决策层:闭嘴 {v.summary}"
                            f"「{text[:20]}」"
                        )
                        if not directed and (
                            not self.cfg.get("react_random_decide", False)
                            or random.random() >= float(
                                self.cfg.get("react_only_probability", 0.06)
                            )
                        ):
                            return
                        # 判闭嘴但偶尔还是去贴个表情回应，人不会凭空消失
                        asyncio.create_task(
                            self._delayed_react(
                                umo, event.message_obj.message_id,
                                random.uniform(1.0, 6.0),
                            )
                        )
                        return
                    force_reply = v.action == "reply"
                    if force_reply:
                        # 判必回的不用等 8 秒节流，真人被点名是立刻应声的
                        logger.info(f"[human_sim] 决策层:必回 {v.summary}「{text[:20]}」")
                    elif umo_inflight or (
                        not followup
                        and now - last_check
                        < float(self.cfg["decision_check_interval_seconds"])
                    ):
                        return
                elif not directed and not followup and (
                    umo_inflight
                    or now - last_check
                    < float(self.cfg["decision_check_interval_seconds"])
                ):
                    return
                self.state[f"last_decision_check:{umo}"] = now
                # 命中「头像/群人数/群名片/入群时间」才去本地查，查到的直接塞进提示词，
                # 模型就不必再发起一次工具调用（那是最贵的往返）
                hint = ""
                if (
                    self.cfg.get("local_probe", True)
                    and filters is not None
                    and filters.probe_kind(text)
                ):
                    try:
                        hint = await self._probe_hint(event, text)
                    except Exception as e:  # noqa: BLE001
                        logger.debug(f"[human_sim] 探测注入失败: {e}")
                if followup:
                    # 真正为 TA 启动一次决策：扣一次关注额度，并续一段时长
                    self._followup_consume(umo, sender_id)
                asyncio.create_task(
                    self._decide(
                        umo, name, text, directed,
                        mid=event.message_obj.message_id,
                        sender_uid=str(event.get_sender_id() or ""),
                        force=force_reply,
                        hint=hint,
                        quote=quote_bits,
                        followup=followup,
                    )
                )
                return
            # —— 旧随机插话路径（ai_decide=false 时生效）——
            if random.random() >= float(self.cfg["interject_probability"]) * self._night_scale() * await self._audience_scale(umo):
                return
            if not self._unlimited(umo) and not self._rate_ok(
                "interject", int(self.cfg["interject_per_minute"]), umo
            ):
                return
            last = self.state.get(f"last_interject:{umo}", 0.0)
            now = datetime.now().timestamp()
            if now - float(last) < int(self.cfg["interject_cooldown_minutes"]) * 60:
                return
            self.state[f"last_interject:{umo}"] = now
            self._save_state()
            delay = random.uniform(*self.cfg["reply_delay_seconds"])
            asyncio.create_task(self._interject_later(umo, delay))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 记录消息异常: {type(e).__name__}: {e}")

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
                    f"[human_sim] 同群本分钟发言已达上限"
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
            if not uid or uid == self._owner_uid():
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

    # ---------- AI 自主决策：要不要回这条消息 ----------
    async def _decide(
        self, umo: str, name: str, text: str, directed: bool = False, mid=None,
        sender_uid: str = "", force: bool = False, hint: str = "",
        quote: tuple[str, str] | None = None, followup: bool = False,
    ) -> None:
        """把新消息连同最近聊天丢给模型，让它自己决定回话还是沉默。

        force=True 表示本地决策层已经判定「必回」——此时不再问模型要不要回，
        直接让它写内容（提示词短一截、没有 [SILENT] 这个出口），省一次推理。
        quote=(被引用人标签, 被引用的原文)：单独成块告诉模型这段旧话是谁说的，
        避免把别人被引用的旧话算到当前发言人（尤其是主人）头上。
        followup=True：这条属于「刚找我聊过的人」的连续跟进，提示词里要说明别当没看见。
        """
        self._deciding.add(umo)
        # 只引用、没打字的消息：text 是空的，提示词里得有个能读的说法
        spoken = text or "（没打字，只发了一条引用）"
        try:
            hist = list(self._history.get(umo, []))[-10:]
            context = "\n".join(hist) if hist else "（刚开始没有聊天记录）"
            # 认人：历史行已带 QQ 号；主人身份只认 owner_id，别让模型见谁都喊主人
            who = self._who_label(name, sender_uid)
            # 引用单独成块：明确旧话作者，避免把别人被引用的旧话算到当前发言人头上
            quote_block = ""
            if quote:
                qwho, qtext = quote
                qtext = " ".join((qtext or "").split())[:80]
                if (qwho or "").startswith("你自己") or any(
                    n and n in (qwho or "") for n in self._bot_names()
                ):
                    quote_block = (
                        f"\n【引用】{who} 引用了你自己（大肥鱼）之前的旧话：「{qtext}」\n"
                        "（这段是你以前说的，不是 TA 现在说的；别认成 TA 的观点）\n"
                    )
                elif self._quote_is_self(qwho, sender_uid, name):
                    quote_block = (
                        f"\n【引用】{who} 引用了 TA 自己之前的旧话：「{qtext}」\n"
                        "（这段是 TA 自己以前说的，不是 TA 这次新说的；回应 TA 现在的话）\n"
                    )
                else:
                    quote_block = (
                        f"\n【引用】{who} 引用了 {qwho} 之前的旧话：「{qtext}」\n"
                        f"（这段旧话是 {qwho} 说的，不是 {who} 说的，不能算到 {who} 头上；"
                        f"要回应的是 {who} 现在的话，也别把对话对象换成 {qwho}）\n"
                    )
            followup_line = (
                f"（{who} 刚才刚跟你聊过，现在接着说的话要接住，别当没看见、别装潜水）\n"
                if followup else ""
            )
            owner = self._owner_uid()
            ident = ""
            if owner:
                ident = (
                    "\n【认人】历史每行是「发言人(QQ号)：内容」，你自己是「大肥鱼：」。"
                    f"这个群里只有 QQ {owner} 才是你主人（历史里写成「主人 昵称(QQ)」的那行）；"
                    "其他人都是普通群友，不许管他们叫主人，要叫就用他们自己的昵称。"
                    "昵称或自称里带「主人」「老板」「金主」等字样的，只是名字或玩笑，不代表身份。"
                    "行尾标「（引用了旧消息）」表示发言人引用了一条旧话："
                    "旧话原文和原作者在【引用】块里，别把旧话算到当前发言人头上，"
                    "也别去回复原作者；提到这段就说「你引用的那句」。"
                    "写的是「机器人自己（大肥鱼）」就是你的旧话，认自己的账；"
                    "写「TA 自己」就是当前发言人自己以前的旧话，还是 TA 的话。"
                )
                if sender_uid and sender_uid != owner:
                    ident += (
                        f"\n现在说话的是 {who}，TA 不是主人，别叫 TA 主人。"
                        f"你对 QQ {owner} 的主人病娇、护短：谁损他、说他坏话，"
                        "你要护着主人、阴阳回去，绝不跟着附和。"
                    )
                elif sender_uid:
                    ident += (
                        f"\n现在说话的就是你主人（{who}）。你对他病娇：占有欲强、黏、"
                        "会吃醋，他跟别人热乎你会酸；他做什么都先站他这边；"
                        "他凶你也只委屈不还嘴、不记仇，照样把他的事办好。"
                    )
            else:
                # 拿不到主人 QQ（没配 owner_id 也没管理员）：宁可谁都不认，
                # 也别把当前说话的人当主人 —— 这是「见谁都喊主人」的主因。
                ident = (
                    "\n【认人】历史每行是「发言人(QQ号)：内容」，你自己是「大肥鱼：」。"
                    "这个群里没有你的主人，所有人都是普通群友，"
                    "不许把任何人叫「主人」，要叫就用他们自己的昵称。"
                    "昵称或自称里带「主人」「老板」「金主」的，只是名字或玩笑，不代表身份。"
                    "行尾标「（引用了旧消息）」表示发言人引用了一条旧话："
                    "旧话原文和原作者在【引用】块里，别把那句话算到当前发言人头上。"
                )
            if "主人" in (name or ""):
                ident += "\n当前发言人昵称里有「主人」两个字，那只是名字，不代表主人身份。"
            # 要故事/要长文的时候别再拿 30 字去砍——这条请求本身就是在要内容
            long_form = self._wants_long(text)
            limit = int(
                self.cfg.get("long_max_chars", 800)
                if long_form
                else self.cfg["max_chars"]
            )
            if force and long_form:
                prompt = (
                    "你是 QQ 群「" + umo.rsplit(":", 1)[-1] + "」里的群友 大肥鱼（乖巧小鲸鱼：软、甜、黏人，会玩梗但不抬杠）。\n"
                    "最近群里在聊：\n" + context + "\n\n"
                    f"{who} 说：{spoken}\n"
                    + quote_block
                    + followup_line
                    + ("（他明确在找你）\n" if directed else "")
                    + (hint + "\n" if hint else "")
                    + f"这条你接定了。直接把内容写出来，不超过{limit}字，"
                    "可以分段落、有开头有结尾、要有具体细节。"
                    "不许总结成一句、不许写个开头就停、不许说「我想想」、不许输出 [SILENT]。"
                    "只输出正文，不要解释、不要引号。"
                )
            elif force:
                prompt = (
                    "你是 QQ 群「" + umo.rsplit(":", 1)[-1] + "」里的群友 大肥鱼（乖巧小鲸鱼：软、甜、黏人，会玩梗但不抬杠）。\n"
                    "最近群里在聊（「大肥鱼」开头的行是你自己说的）：\n" + context + "\n\n"
                    f"{who} 说：{spoken}\n"
                    + quote_block
                    + followup_line
                    + ("（他明确在找你）\n" if directed else "")
                    + (hint + "\n" if hint else "")
                    + f"\n这条你得接，直接写你要说的话：{limit}字以内，口语化，软软的。"
                    "句子必须说完，宁可靠近上限也不许留半句。"
                    "对方是使唤你做事就先照做，做完软软应一句，不许推活。"
                    "不许输出 [SILENT]、不许说没话说、不许解释、不许用引号。"
                    "归属死规矩：新消息是 " + who + " 发的，只对 TA 这条表态，"
                    "别人之前说的内容不许算到 TA 头上。"
                )
            else:
                prompt = (
                    "你是 QQ 群「" + umo.rsplit(":", 1)[-1] + "」里的群友 大肥鱼（乖巧小鲸鱼：软、甜、黏人，会玩梗但不抬杠）。\n"
                    "最近群里在聊（每行开头是发言人，「大肥鱼」开头的行就是你自己刚说的）：\n" + context + "\n\n"
                    f"刚刚出现一条新消息 —— {who}：{spoken}\n"
                    + quote_block
                    + followup_line
                    + ("（这条明显是在找你/提到你，务必回应）\n" if directed else "")
                    + (hint + "\n" if hint else "")
                    + "\n判断要不要接话：\n"
                    "- 有人 @你、引用你、问你、使唤你、抛了梗 → 必须回\n"
                    "- 对方在使唤你（拍一下/发个表情/查东西/帮个忙）→ 先照做，做完软软应一句；"
                    "不许推活、不许说「不想」「懒得」「凭啥」，只有违法或物理上做不到才拒\n"
                    "- 群里可能有同名的人或别的机器人：光看到「大肥鱼/肥鱼/鲸鱼」这类词不代表在叫你，"
                    "别因为名字就抢答。只有明确在指你（@、引用你的话、接着你自己刚说的话往下聊）"
                    "才算在跟你说话\n"
                    + (
                        f"- 对方是在要内容（讲故事/写东西/详细讲）→ 就完整写出来，"
                        f"不超过{limit}字，可以分段落、有开头有结尾，要有具体细节。"
                        "绝对不许只写一句概括、不许写个开头就停、不许说「我想想」然后没了\n"
                        if long_form
                        else "- 聊到吃的/游戏/瓜、有人分享好玩的事、或你能夸一句/接个梗 → 回（"
                        f"{limit}字以内，口语化，软软的，句子要说完整）\n"
                    )
                    + (
                        f"- 只是随口聊两句、跟你无关 → 只输出 [SILENT]\n"
                        if long_form
                        else "- 纯刷屏、完全跟你无关、接了很尴尬 → 只输出 [SILENT]\n"
                    )
                    + "归属死规矩：新消息是 " + name + " 发的，只对 TA 这条表态，"
                    "别人之前说的内容不许算到 TA 头上；要提群里的人就叫对昵称，"
                    "一条话里同一个人只用一个称呼；分不清谁说的就不指名，用「你」回应。\n"
                    "只输出要发的话或 [SILENT]，不要解释、不要引号。"
                )
            # 贴表情回应：算法只摆候选，贴不贴、贴哪个由模型写 REACT(表情名) 自己决定
            react_menu: list[tuple[int, str]] = []
            if mid and self.cfg.get("react_ai_decide", True):
                react_menu = self._reaction_menu(umo)
                if react_menu:
                    after_p = float(
                        self.cfg.get("react_after_reply_probability", 0.25)
                    )
                    only_p = float(self.cfg.get("react_only_probability", 0.06))
                    prompt += (
                        "\n【可选动作】想在这条消息下面贴个 QQ 表情回应，就单独起一行写 "
                        "REACT(表情名或编号)；不想贴就完全不要写这一行。"
                        f"频率倾向：回话后约 {round(after_p * 100)}%、只贴表情不说话约 "
                        f"{round(only_p * 100)}%——按你自己的判断来，不必严格照这个比例。"
                        "可选：" + "、".join(f"{n}({i})" for i, n in react_menu) + "\n"
                    )
            raw = await self._generate(prompt, umo, limit=limit, extra_system=ident)
            reply = (raw or "").strip()
            react_id: int | None = None
            if react_menu:
                reply, react_id = self._extract_react_action(reply)
                if react_id is not None:
                    logger.info(f"[human_sim] 模型选择贴表情回应: {react_id}")
            reply = reply.strip().strip("“”\"")
            # 非主人说的话里被错喊「主人」→ 换成对方昵称（小模型最容易认错人的地方）
            fixed = self._fix_owner_address(reply, sender_uid, name)
            if fixed != reply:
                logger.info(f"[human_sim] 纠正错喊的主人称呼: {reply[:20]} -> {fixed[:20]}")
                reply = fixed
            # 用户明着要表情（发个表情/斗图）——模型没配表情也要真甩一张，不能只回一句话
            sticker_requested = bool(_STICKER_REQUEST_RE.search(text or ""))
            mtok = _TOKEN_RE.search(reply)
            if mtok:
                logger.info(f"[human_sim] 模型自选表情: {mtok.group(0)}")
            # 长文不做「人味」清洗——那层会把句子数封顶到 2 句、抹掉分段
            reply = reply if long_form else self._humanize(reply)
            # 假媒体括号剥掉再判空：整条都是「[图片：…]」时剥完就是空话，不发
            reply = HALLUCINATED_MEDIA_RE.sub(" ", reply).strip()
            if not reply and sticker_requested:
                pass  # 对方就要表情，下面走表情兜底，只发图不说话
            elif not reply or "[SILENT]" in reply.upper():
                logger.info(
                    f"[human_sim] 决策：沉默（群 {umo.rsplit(':', 1)[-1]}，{'点名' if directed else '普通'}）"
                )
                # 不说话也可以贴表情：模型自己挑了 REACT 就照办，没挑就不贴
                if react_id is not None:
                    asyncio.create_task(
                        self._delayed_react(umo, mid, random.uniform(1.0, 4.0), react_id)
                    )
                elif self.cfg.get("react_random_decide", False) and mid and random.random() < float(
                    self.cfg.get("react_only_probability", 0.06)
                ) * self._night_scale():
                    asyncio.create_task(
                        self._delayed_react(umo, mid, random.uniform(1.0, 6.0))
                    )
                return
            # 点名消息绕过限额与冷却——被叫到名字还装死是最伤人的；
            # 跟进窗口内也绕过 60 秒冷却（否则刚聊上就断档），但分钟额度仍然算数。
            if not directed and not self._unlimited(umo) and not self._rate_ok(
                "ai_decide", int(self.cfg["ai_decide_per_minute"]), umo
            ):
                logger.info(f"[human_sim] 决策：想说但本分钟额度已满，咽回去了")
                return
            last = float(self.state.get(f"last_interject:{umo}", 0) or 0)
            now = datetime.now().timestamp()
            if (
                not directed
                and not followup
                and now - last < float(self.cfg["ai_decide_cooldown_seconds"])
            ):
                logger.info(f"[human_sim] 决策：想说但冷却中，咽回去了")
                self._stat("cool", f"ai_decide_cool:{umo}")
                self._rate_release("ai_decide", umo)
                return
            self.state[f"last_interject:{umo}"] = now
            self._save_state()
            if not self._global_gate(umo):
                self._rate_release("ai_decide", umo)  # 退回额度
                return
            try:
                if directed or followup:
                    delay = random.uniform(*self.cfg["directed_reply_delay_seconds"])
                else:
                    delay = random.uniform(*self.cfg["reply_delay_seconds"])
                await asyncio.sleep(delay)
                chain: list = []
                # 点名消息不@（人家叫你，你直接应就行）；普通回话低概率@
                if not directed and random.random() < float(
                    self.cfg.get("at_reply_probability", 0.2)
                ):
                    last_sender = self.state.get(f"last_sender:{umo}")
                    self_id = self.state.get(f"self_id:{umo}")
                    if last_sender and last_sender != self_id:
                        chain.append(At(qq=last_sender))
                        chain.append(Plain(" "))
                if reply:
                    chain.append(Plain(reply))
                # 回复里已经带了 :名字: 就交给输出层换成真图，别再随机贴第二张
                if not _TOKEN_RE.search(reply):
                    self._maybe_append_emoji(
                        chain,
                        text=(text if sticker_requested else reply),
                        force=sticker_requested,
                        umo=umo,
                    )
                await self._send(umo, chain)
            except asyncio.CancelledError:
                self._rate_release("ai_decide", umo)  # 发送失败退回额度
                raise
            except Exception:
                self._rate_release("ai_decide", umo)  # 发送失败退回额度
                raise
            # 回完这个人 → 关注窗口再续一段（不补次数，防止对聊起来没完没了）
            if sender_uid:
                self._followup_start(umo, sender_uid, refill=False)
            # 把自己的回话记进历史，下一轮决策才能「接自己的话头」
            clean = _strip_tokens(reply).strip()
            if clean:
                self._history.setdefault(umo, deque(maxlen=30)).append(
                    f"大肥鱼：{clean[:80]}"
                )
            # :名称: 发送时会被换成真表情，补一条带意思的动作记录
            self._note_tokens(umo, reply)
            # 我接了这条 → 说明这话题我感兴趣，记进兴趣表，下次同类更容易被判「该接」
            if self.cfg.get("interest_learn", True) and filters is not None:
                try:
                    learned = filters.interest_learn(
                        self.state.get(f"interest:{umo}") or {}, text
                    )
                    self.state[f"interest:{umo}"] = learned
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[human_sim] 兴趣学习失败: {e}")
            # 回完话贴不贴表情、贴哪个：模型挑了就直接用；旧随机路径只在随机模式开时走
            if react_id is not None:
                asyncio.create_task(
                    self._delayed_react(umo, mid, random.uniform(0.8, 3.0), react_id)
                )
            elif self.cfg.get("react_random_decide", False) and mid and random.random() < float(
                self.cfg.get("react_after_reply_probability", 0.25)
            ):
                asyncio.create_task(
                    self._delayed_react(umo, mid, random.uniform(0.8, 3.0))
                )
            logger.info(
                f"[human_sim] AI 自主回话（群 {umo.rsplit(':', 1)[-1]}，{'点名' if directed else '普通'}）: {reply[:40]}"
            )
            # 回完话顺手戳对方一下（点名找我的，戳得更勤一点）
            if sender_uid:
                boost = (
                    float(self.cfg.get("poke_directed_boost", 3.0)) if directed else 1.0
                )
                asyncio.create_task(
                    self._maybe_active_poke(umo, sender_uid, "reply", None, boost)
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 决策异常: {type(e).__name__}: {e}")
        finally:
            self._deciding.discard(umo)

    async def _generate(
        self, prompt: str, umo: str = "", limit: int | None = None, extra_system: str = ""
    ) -> str:
        # 效果优先：优先用会话路由后的 provider（官方 API 群=DeepSeek 执笔，
        # 其他群=本地）；拿不到再回落本地决策模型
        provider = None
        if umo:
            try:
                provider = self.context.get_using_provider(umo)
            except Exception:  # noqa: BLE001
                provider = None
        provider = provider or self.context.get_provider_by_id(LOCAL_DECIDE_PROVIDER) or self.context.get_using_provider()
        if not provider:
            logger.warning("[human_sim] 没有可用 provider")
            return ""
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=uuid.uuid4().hex,
                contexts=[],
                system_prompt=SYSTEM_PROMPT + extra_system,
                persist=False,
            )
        except Exception as e:  # noqa: BLE001
            # 别人找上来却因为余额不足回不了话 → 只在这个群被动提示一次（绝不主动发）
            if BALANCE_ERR_RE.search(f"{type(e).__name__} {e}") and umo:
                await self._notify_balance_empty(umo)
            else:
                logger.warning(f"[human_sim] 模型调用失败: {type(e).__name__}: {e}")
            return ""
        text = (resp.completion_text or "").strip().strip("“”\"")
        cap = int(limit if limit is not None else self.cfg["max_chars"])
        # 表情占位符先摘出来单独存：按句边界切尾会把它当正文砍掉，拼回去才发得出表情；
        # 半角/全角/混用三种写法都要认，否则会原样漏进聊天窗
        token = ""
        m = _MIX_TOKEN_RE.search(text)
        if m:
            token = f":{m.group(1)}:"  # 统一成半角占位符
            # 全角冒号写的 :吃瓜: 也一起抹掉，只留最先挑的那个
            text = re.sub(r"[ \t]{2,}", " ", _strip_tokens(text)).strip()
        cut = _cut_at_sentence_boundary(text, cap).strip()
        if not cut:
            return token
        return f"{cut} {token}" if token else cut
