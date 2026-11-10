"""群消息收集：指令识别、+1 接龙、复读分发与决策调度（从 pipeline.py 按职责拆出）。"""

from __future__ import annotations

import asyncio
import random
import re
from collections import deque
from datetime import datetime
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import (
    Face,
    File,
    Forward,
    Image,
    Nodes,
    Plain,
    Record,
    Video,
)

from .text import (
    _FY_CONTENT_RE,
    _IDENTITY_TAG_RE,
    _NO_STICKER_REQ_RE,
    _QUOTE_RE,
    _VOICE_REQUEST_RE,
    _clean_quote,
)
from .runtime import filters

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class PipelineRecordMixin:

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

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def _record(self, event: AstrMessageEvent):
        try:
            blk = self._blocked_by(event.get_sender_id())
            if blk:
                # 厌烦/拉黑窗口内：群消息不回应也不记，@唤醒一并拦掉
                logger.info(
                    f"[human_sim] 已拉黑，忽略消息（原因：{blk.get('reason') or '没说'}）"
                )
                event.stop_event()
                return
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
                # 与 _on_poke 一致：发起者取原始事件的 user_id / sender_id，
                # 别被 Poke 段里的 id 带偏，否则历史里会记成「被戳的人拍了自己」。
                _raw = getattr(event.message_obj, "raw_message", None)
                _raw = _raw if isinstance(_raw, dict) else {}
                poker = str(
                    _raw.get("user_id")
                    or _raw.get("sender_id")
                    or event.get_sender_id()
                    or ""
                )
                if poker and poker != str(event.get_self_id() or ""):
                    # 机器人自己拍的回显（poker=自己）已由 _do_poke 成功路径记录，这里跳过防重复
                    try:
                        bot_ref = getattr(event, "bot", None)
                        if bot_ref is not None:
                            self._bots[umo] = bot_ref
                        target = str(_raw.get("target_id") or poke_comp.target_id() or "")
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
                # 这里也按死一次，保证拍一戳不会被 LLM 当成一句话接走。
                # 但 _on_poke 主动把这一戳交给正式会话时（_hs_poke_to_chat），不能按死。
                if not event.get_extra("_hs_poke_to_chat"):
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
                ) or any(isinstance(c, Face) for c in chain) or any(
                    # 动图斗图（capoo 这类）是裸 Image 组件、一个字都没有，
                    # 靠 .gif 后缀认出来，不然整场斗图机器人只会装死
                    isinstance(c, Image)
                    and str(getattr(c, "file", "") or getattr(c, "url", "") or "")
                    .lower()
                    .endswith(".gif")
                    for c in chain
                )
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
            # 明确要语音（「发条语音/语音回我/说给我听」）：本地决策链没有工具，
            # 自己编只会回一句「我没法发语音」。抬成定向消息交给正式流水线去调 send_voice。
            if _VOICE_REQUEST_RE.search(text) and not _NO_STICKER_REQ_RE.search(text):
                logger.info(
                    f"[human_sim] 语音请求交给正式流水线（{umo.rsplit(':', 1)[-1]}）"
                )
                event.is_at_or_wake_command = True
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
                    # 窗口开着且盯的就是 TA：TA 再说的话一律交给模型自主判断
                    # 接不接（_decide(followup=True) 里模型可以自己选 [SILENT]），
                    # 不再因别人插话就清窗——插话只在超过容忍值时才放走窗口
                    self._followup_reset_others(umo, sender_id)
                    if self._followup_gap_ok(umo, sender_id):
                        followup = True
                    else:
                        # 窗口内但刚为 TA 决策过：这条先不重复接，等下一句
                        return
                elif not directed and self._followup_active(umo):
                    # 窗口盯的是别人：别人的插话计数 +1（机器人自己的回声不算）；
                    # 超过 followup_other_tolerance 条才放走窗口，退回普通概率
                    if sender_id and sender_id != str(
                        self.state.get(f"self_id:{umo}") or ""
                    ):
                        self._followup_note_other(umo)
                # 输入端防刷屏：按群人数自适应（大群消息本来就密）。
                # 活跃群只降插话概率，不直接判死；只有极端斗图才完全潜水
                flood_penalty = 1.0
                ts_list = self._msg_ts.get(umo)
                if ts_list and not directed and not followup:
                    now = datetime.now().timestamp()
                    recent = sum(1 for t in ts_list if now - t <= 30)
                    try:
                        flood_limit = max(
                            12, int((await self._group_member_count(umo) or 0) / 120)
                        )
                    except Exception:  # noqa: BLE001
                        flood_limit = 12
                    if recent > flood_limit * 3:
                        logger.info(
                            f"[human_sim] 极端刷屏（30s 内 {recent} 条 > {flood_limit * 3}），潜水不决策"
                        )
                        return
                    if recent > flood_limit:
                        # 越活跃压得越低，最低保留 5%
                        over = min(1.0, (recent - flood_limit) / max(1, flood_limit))
                        flood_penalty = 0.3 - 0.25 * over
                        logger.info(
                            f"[human_sim] 群偏活跃（30s 内 {recent} 条 > {flood_limit}），插话概率降至 {int(flood_penalty * 100)}%"
                        )
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
                    # 活跃群只在最后统一降权，不打断上面的加成逻辑
                    p *= flood_penalty
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
            delay = random.uniform(*self._reply_delay_range(umo, False))
            asyncio.create_task(self._interject_later(umo, delay))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 记录消息异常: {type(e).__name__}: {e}")
