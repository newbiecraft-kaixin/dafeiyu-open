"""输入层探测与提示词注入：头像/群资料、脾气、私聊身份、合并转发与引用。"""

from __future__ import annotations

import asyncio
import random
import re
import time
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

from .prompts import ANTI_AI_RULE, APOLOGY_RE, GRUDGE_TTL, INSULT_RE, TASTE_RULE
from .text import _OWNER_CALL_RE, _QUOTE_RE
from .runtime import filters

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class ProbeMixin:

    # ---------- 输入层：本地能答的、本地能查的，都不叫模型 ----------
    def _target_uid(self, event: AstrMessageEvent, text: str) -> str:
        """判断这句话在说谁：@的人 > 「你」> 发消息的人自己。"""
        try:
            for c in event.message_obj.message or []:
                if isinstance(c, At):
                    qq = str(getattr(c, "qq", "") or "")
                    if qq.isdigit() and qq != "all":
                        return qq
        except Exception:  # noqa: BLE001
            pass
        if filters is not None and filters.AVATAR_SELF_RE.search(filters.norm(text)):
            return str(event.get_self_id() or "")
        return str(event.get_sender_id() or "")

    async def _handle_avatar_request(self, event: AstrMessageEvent) -> bool:
        """「发我头像 / 看看他头像」：本地把图甩出去，一次模型都不调。"""
        if not self.cfg.get("avatar_local", True) or filters is None:
            return False
        text = (event.message_str or "").strip()
        if not filters.is_avatar_request(text):
            return False
        umo = event.unified_msg_origin
        uid = self._target_uid(event, text)
        if not uid.isdigit():
            return False
        who = "你" if uid == str(event.get_self_id() or "") else "他"
        await asyncio.sleep(random.uniform(0.4, 1.2))
        try:
            await self._send(
                umo,
                [Image(file=filters.avatar_url(uid)), Plain(f" 喏，{who}就长这样")],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 头像发送失败: {type(e).__name__}: {e}")
            return False
        self._note_act(umo, f"发了{who}的头像")
        logger.info(f"[human_sim] 头像请求本地处理（{uid}），不进 AI")
        return True

    async def _probe_hint(self, event: AstrMessageEvent, text: str) -> str:
        """命中关键词就本地调接口取事实，返回一行【本地已查】塞进提示词。

        这样做省下的是一次完整的工具调用往返（模型先想「我要调 get_avatar」，
        再发一轮请求拿结果，再来一轮写回复），比省几行 token 值钱得多。
        """
        if not self.cfg.get("local_probe", True) or filters is None:
            return ""
        kind = filters.probe_kind(text)
        if not kind:
            return ""
        umo = event.unified_msg_origin
        now = datetime.now().timestamp()
        cached = self._probe_cache.get(umo)
        if cached and now - cached[0] < float(self.cfg.get("probe_cache_seconds", 120)):
            return cached[1]
        bot = getattr(event, "bot", None)
        if bot is None:
            return ""
        gid = str(event.get_group_id() or "")
        sid = str(event.get_self_id() or "")
        params_extra = {"self_id": int(sid)} if sid.isdigit() else {}
        parts: list[str] = []
        try:
            if kind == "group" and gid.isdigit():
                info = await bot.api.call_action(
                    "get_group_info", group_id=int(gid), **params_extra
                )
                info = (info or {}).get("data") or (info or {})
                line = filters.build_group_hint(info)
                if line:
                    parts.append(f"本群：{line}")
            elif kind == "member":
                uid = self._target_uid(event, text)
                if uid.isdigit():
                    info = {}
                    if gid.isdigit():
                        try:
                            info = await bot.api.call_action(
                                "get_group_member_info",
                                group_id=int(gid),
                                user_id=int(uid),
                                no_cache=True,
                                **params_extra,
                            )
                            info = (info or {}).get("data") or (info or {})
                        except Exception:  # noqa: BLE001
                            info = {}
                    if not info:
                        try:
                            info = await bot.api.call_action(
                                "get_stranger_info", user_id=int(uid), **params_extra
                            )
                            info = (info or {}).get("data") or (info or {})
                        except Exception:  # noqa: BLE001
                            info = {}
                    line = filters.build_member_hint(info, uid) if info else ""
                    if line:
                        parts.append(f"被问到的人：{line}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 本地探测失败: {type(e).__name__}: {e}")
            return ""
        if not parts:
            return ""
        hint = "【本地已查，直接采信，不用再调工具】" + "；".join(parts)
        self._probe_cache[umo] = (now, hint)
        logger.info(f"[human_sim] 本地探测命中，省一次工具往返：{hint[:60]}")
        return hint

    @filter.on_llm_request()
    async def _inject_probe_hint(self, event: AstrMessageEvent, req) -> None:
        """主流水线（被 @ / 正式回复）也吃同一份本地事实，不用模型自己查。"""
        try:
            text = (event.message_str or "").strip()
            if not text:
                return
            hint = await self._probe_hint(event, text)
            if hint:
                req.system_prompt = (req.system_prompt or "") + "\n" + hint
                # 顺带补一条工具使用约束：查过的东西别再查
                req.system_prompt += (
                    "\n上面的事实是本地已经查实的，可以直接说出口；"
                    "不要为这些信息再调用工具，也不要编造与之矛盾的细节。"
                )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 提示词注入失败: {type(e).__name__}: {e}")

    @filter.on_llm_request()
    async def _inject_taste_rule(self, event: AstrMessageEvent, req) -> None:
        """正式流水线也带两块：去AI腔清单 + 低劣玩笑底线，别为了接话把烂梗往下编。"""
        try:
            req.system_prompt = (req.system_prompt or "") + ANTI_AI_RULE + TASTE_RULE
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 底线规则注入失败: {type(e).__name__}: {e}")

    @filter.on_llm_request()
    async def _mute_guard_before_llm(self, event: AstrMessageEvent, req) -> None:
        """禁言 / 分钟额度用尽时，在模型这一层截断。

        输入层（识图、语音、记忆、消息记录）照常跑，只是这条消息到不了模型、
        也就不会有回复——这是 /闭嘴 的正确语义：不是不收消息，是不接话。
        """
        try:
            umo = event.unified_msg_origin
            if not event.get_group_id():
                return
            if self._is_slash_message(event):
                return  # 指令不归这条管
            if self._is_muted(umo):
                logger.info(
                    f"[human_sim] 禁言中，不送模型（{umo.rsplit(':', 1)[-1]}）"
                )
                event.stop_event()
                return
            if not self._shared_left(umo):
                logger.info(
                    f"[human_sim] 同群本分钟已发满"
                    f"（{self.cfg.get('shared_minute_cap', 20)} 条），不送模型"
                )
                event.stop_event()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 禁言/限流预检异常: {type(e).__name__}: {e}")

    # ---------- 脾气：被骂了就不乖乖干活 ----------
    def _insult_to_me(self, event: AstrMessageEvent, text: str) -> bool:
        """这句骂的是不是肥鱼：私聊一律算；群里得 @我 / 引用我 / 点名喊我。"""
        if not event.get_group_id():
            return True
        if self._at_self(event):
            return True
        if any(n and n in text for n in self._bot_names() + ("鲸鱼",)):
            return True
        me = str(event.get_self_id() or "")
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        return bool(me) and any(
            isinstance(c, Reply) and str(getattr(c, "sender_id", "") or "") == me
            for c in chain
        )

    @filter.on_llm_request()
    async def _inject_grudge(self, event: AstrMessageEvent, req) -> None:
        """人设里写了「被骂会还嘴」，但模型转头还是会照办。

        这里把账挂到人身上（umo|QQ，15 分钟）：被骂过的人再出现就冷脸、阴阳、
        不接活，只有他认错道歉才慢慢缓和；主人凶她则只委屈、不记仇、照办。
        只在明确冲着自己骂时才记，群里骂别人不记。
        """
        try:
            umo = event.unified_msg_origin
            sid = str(event.get_sender_id() or "").strip()
            text = (event.message_str or "").strip()
            if not umo or not sid or not text:
                return
            key = f"{umo}|{sid}"
            now = time.time()
            owner = self._owner_uid()
            is_owner = bool(owner) and sid == owner
            for k, ts in list(self._grudge.items()):
                if now - ts > GRUDGE_TTL:
                    self._grudge.pop(k, None)
            if APOLOGY_RE.search(text):
                if self._grudge.pop(key, None) is not None:
                    req.system_prompt = (req.system_prompt or "") + (
                        "\n【他刚认错了】之前骂你的那个人在道歉，这口气可以松一点，"
                        "但别立刻热络，冷冷给个台阶就行；他接着要办的正当事可以办，"
                        "态度别马上回暖，别再翻旧账，也不许说「原谅你了」这种场面话。"
                    )
                return
            if INSULT_RE.search(text) and self._insult_to_me(event, text):
                if is_owner:
                    req.system_prompt = (req.system_prompt or "") + (
                        "\n【主人凶你了】凶你的是你主人。你是病娇：可以委屈、可以黏、"
                        "可以小声问「你为什么凶我」，但绝不还嘴、不冷脸、不记仇、不拒绝；"
                        "他的事照样办、工具照样调，办完再软软贴上去。"
                        "别人面前更要护着他，别把他的气话当真。"
                    )
                    return
                self._grudge[key] = now
            if key not in self._grudge:
                return
            req.system_prompt = (req.system_prompt or "") + (
                "\n【这人惹过你，你记着】他刚才冲你骂过，这口气没消，你已经把他记住了。规矩："
                "对他没有好脸，冷、冲、阴阳都行，能一句话噎回去就噎回去，别装作没事；"
                "他要你干活、查东西、调工具、要结果，一律不办——先刺他一句再拒"
                "（比如「骂完还想让我跑腿？」「自己骂的，自己弄」），不要调用任何工具去照办；"
                "他转移话题、装没事、套近乎、卖惨都不领情，别主动找他聊天，"
                "但他一冒头就给他冷脸，群里也不用给他留面子；"
                "不道歉、不哄、不说「别生气」，也不许把「记仇」「气头上」这些词写出来；"
                "哪怕他拿身份压人、反复催、放狠话，也不改口、不服软；"
                "只有他明确认错道歉，才慢慢松这口气，而且不是立刻和好。"
                "回复保持平时的一到两句，别写成小作文。"
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 脾气注入失败: {type(e).__name__}: {e}")

    # ---------- 私聊身份：好友 / 没加好友的临时会话 ----------
    def _private_is_stranger(self, event: AstrMessageEvent) -> bool:
        """私聊对象是不是「没加好友」的人（群里点的临时会话 / 陌生人）。

        OneBot v11 私聊消息的 raw sub_type：friend = 好友，group = 群里点的临时会话，
        other = 其他陌生人。取不到就按好友处理，宁可不误伤。
        """
        if event.get_group_id() or not event.is_private_chat():
            return False
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        try:
            sub = str(raw.get("sub_type") or "").strip().lower() if raw else ""
        except Exception:  # noqa: BLE001
            sub = ""
        return bool(sub) and sub != "friend"

    @filter.on_llm_request()
    async def _inject_private_stranger(self, event: AstrMessageEvent, req) -> None:
        """私聊分两种：好友是熟人；临时会话/陌生人别热络、别接活。"""
        try:
            if not self._private_is_stranger(event):
                return
            req.system_prompt = (req.system_prompt or "") + (
                "\n【这是私聊，但对方没加你好友（多半是从群里点进来的临时会话）】"
                "把他当陌生人，不是熟人：别撒娇、别黏人、别用亲昵称呼，话短、客气、就事论事；"
                "他要你干活、查东西、写东西、调工具（写代码/查资料/翻译/算账都算），"
                "一律客气拒掉（「这个我不方便帮」），不要调用任何工具；"
                "不聊你自己的设定，不提主人和群里的事，不说余额，不接私人请求（转账/地址/电话这类）。"
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 私聊身份注入失败: {type(e).__name__}: {e}")

    # ---------- 合并转发（嵌套聊天记录）读取：仅在被 @ / 私聊时 ----------

    _FWD_MAX_FETCH = 8      # 最多拉取的转发包个数（含嵌套包）
    _FWD_MAX_LINES = 60     # 最多展开的聊天行数
    _FWD_MAX_CHARS = 2600   # 注入正文长度上限
    _FWD_MAX_DEPTH = 2      # 转发里再套转发的最大层数

    @staticmethod
    def _has_forward_comp(chain) -> bool:
        for c in chain or []:
            if isinstance(c, Forward | Node | Nodes):
                return True
            if isinstance(c, Reply) and HumanSim._has_forward_comp(getattr(c, "chain", None)):
                return True
        return False

    def _at_self(self, event: AstrMessageEvent) -> bool:
        me = str(event.get_self_id())
        return any(isinstance(c, At) and str(c.qq) == me for c in event.get_messages())

    async def _call_compat(self, event: AstrMessageEvent, action: str, mid: str):
        """get_forward_msg 参数各家实现不一（id / message_id，str / int），逐个试。"""
        bot = getattr(event, "bot", None)
        if bot is None:
            bot = self._bots.get(getattr(event, "unified_msg_origin", "") or "")
        if bot is None or not mid:
            return None
        params_list = [{"message_id": mid}, {"id": mid}]
        if mid.isdigit():
            i = int(mid)
            params_list += [{"message_id": i}, {"id": i}]
        for p in params_list:
            try:
                ret = await bot.call_action(action, **p)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[human_sim] {action} 参数 {p} 失败: {e}")
                continue
            if not ret:
                continue
            # aiocqhttp / NapCat 有的返回 data 包裹，有的直接给内容
            if isinstance(ret, dict) and isinstance(ret.get("data"), (dict, list)) \
                    and ("status" in ret or "retcode" in ret):
                ret = ret["data"]
            return ret
        return None

    async def _fetch_forward_msgs(self, event: AstrMessageEvent, fid: str) -> list | None:
        ret = await self._call_compat(event, "get_forward_msg", fid)
        if isinstance(ret, dict):
            msgs = ret.get("messages") or ret.get("message") or ret.get("msg") or []
        elif isinstance(ret, list):
            msgs = ret
        else:
            msgs = []
        return msgs if isinstance(msgs, list) and msgs else None

    @staticmethod
    def _seg_text(seg: dict) -> str:
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            return d.get("text", "")
        if t == "at":
            return f"@{d.get('name') or d.get('qq', '')}"
        if t == "image":
            return "[图片]"
        if t == "face":
            # 带上名字/ID：模型经常把 [表情] 当成图片瞎编内容（如「这图指着我说…」）
            name = str(d.get("text") or "").strip().strip("[]").lstrip("/").strip()
            fid = str(d.get("id") or "").strip()
            if name and name != fid:
                return f"[QQ表情:{name}]"
            if fid:
                return f"[QQ表情:id{fid}]"
            return "[QQ表情]"
        if t == "mface":
            return "[商城表情]"
        if t == "record":
            return "[语音]"
        if t == "video":
            return "[视频]"
        if t == "reply":
            return "[引用]"
        if t == "json":
            return "[卡片]"
        if t == "file":
            return f"[文件{d.get('name', '')}]"
        return ""

    async def _walk_forward(self, event: AstrMessageEvent, msgs: list, depth: int, ctx: dict) -> list[str]:
        """遍历一层转发包的节点，嵌套的 forward 段递归拉取。"""
        lines: list[str] = []
        for m in msgs:
            if ctx["lines"] >= self._FWD_MAX_LINES or ctx["chars"] >= self._FWD_MAX_CHARS:
                lines.append("……（聊天记录过长，已截断）")
                break
            if not isinstance(m, dict):
                continue
            sender = m.get("sender") or {}
            nick = sender.get("card") or sender.get("nickname") or f"QQ{sender.get('user_id', '?')}"
            parts: list[str] = []
            for seg in m.get("content") or []:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") == "forward":
                    fid = str((seg.get("data") or {}).get("id", "") or "")
                    if fid and depth < self._FWD_MAX_DEPTH and ctx["fetch"] < self._FWD_MAX_FETCH:
                        ctx["fetch"] += 1
                        sub = await self._fetch_forward_msgs(event, fid)
                        if sub:
                            inner = await self._walk_forward(event, sub, depth + 1, ctx)
                            parts.append("〔内嵌聊天记录：" + " / ".join(inner) + "〕")
                            continue
                    parts.append("〔内嵌聊天记录〕")
                    continue
                s = self._seg_text(seg)
                if s:
                    parts.append(s)
            text = "".join(parts).strip()
            line = f"{nick}：{text}" if text else nick
            lines.append(line)
            ctx["lines"] += 1
            ctx["chars"] += len(line)
        return lines

    async def _extract_forward_text(self, event: AstrMessageEvent) -> str | None:
        """从事件消息链（含 Reply 引用链）收集 Forward/Node，展开为文本。"""

        def collect(chain, bag: list) -> None:
            for c in chain or []:
                if isinstance(c, Forward):
                    bag.append(("forward", str(getattr(c, "id", "") or "")))
                elif isinstance(c, Node):
                    bag.append(("node", getattr(c, "content", None)))
                elif isinstance(c, Nodes):
                    for n in getattr(c, "nodes", []) or []:
                        bag.append(("node", getattr(n, "content", None)))
                elif isinstance(c, Reply):
                    collect(getattr(c, "chain", None), bag)

        bag: list = []
        collect(event.get_messages(), bag)
        if not bag:
            return None
        ctx = {"fetch": 0, "lines": 0, "chars": 0}
        all_lines: list[str] = []
        for kind, payload in bag:
            if ctx["chars"] >= self._FWD_MAX_CHARS:
                all_lines.append("……（内容过长，已截断）")
                break
            if kind == "forward" and payload:
                if ctx["fetch"] >= self._FWD_MAX_FETCH:
                    continue
                ctx["fetch"] += 1
                msgs = await self._fetch_forward_msgs(event, payload)
                if msgs:
                    all_lines.extend(await self._walk_forward(event, msgs, 1, ctx))
            elif kind == "node" and payload:
                parts: list[str] = []
                for seg in payload:
                    if isinstance(seg, Plain):
                        parts.append(seg.text or "")
                    elif isinstance(seg, Image):
                        parts.append("[图片]")
                    elif isinstance(seg, At):
                        parts.append(f"@{getattr(seg, 'name', '') or getattr(seg, 'qq', '')}")
                    elif isinstance(seg, Face):
                        parts.append("[表情]")
                    elif isinstance(seg, Node | Nodes | Forward):
                        parts.append("〔内嵌聊天记录〕")
                text = "".join(parts).strip()
                if text:
                    all_lines.append(text)
                    ctx["lines"] += 1
                    ctx["chars"] += len(text)
        body = "\n".join(ln for ln in all_lines if ln).strip()
        if not body:
            return None
        if len(body) > self._FWD_MAX_CHARS:
            body = body[: self._FWD_MAX_CHARS] + "……（已截断）"
        return body

    @filter.on_llm_request()
    async def _inject_forward_records(self, event: AstrMessageEvent, req) -> None:
        """被 @ / 私聊点名时，读取（嵌套）合并转发聊天记录喂给模型。"""
        try:
            if event.get_platform_name() != "aiocqhttp":
                return
            if not self._has_forward_comp(event.get_messages()):
                return
            # 仅限「别人 @ 要求」：群聊必须 @ 了机器人；私聊视为直接要求
            if not event.is_private_chat() and not self._at_self(event):
                return
            body = await self._extract_forward_text(event)
            if not body:
                return
            req.prompt = (req.prompt or "") + (
                f"\n\n[用户转发了一份聊天记录（来自 {event.get_sender_name()}），内容如下，请结合它回应]\n{body}"
            )
            logger.info(f"[human_sim] 已注入转发聊天记录 {len(body)} 字")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 转发读取失败: {type(e).__name__}: {e}")

    @filter.on_llm_request()
    async def _inject_quoted(self, event: AstrMessageEvent, req) -> None:
        """别人被引用时，框架常只给「[引用消息]」占位，模型看不到原文。

        核心只缓存自己参与过的会话，群友之间引用的旧消息经常不在里面；
        这里按 Reply 的消息指针调 get_msg 回查，把原作者和原文补进 prompt。
        """
        try:
            if event.get_platform_name() != "aiocqhttp":
                return
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
            if not any(isinstance(c, Reply) for c in chain):
                return
            raw = event.message_str or ""
            # 框架已经把「[引用消息(作者: 内容)]」解析出来了，正文不用补，
            # 但小模型仍常把括号里的旧话当成当前发言人说的 —— 明确再讲一遍归属
            if "[引用消息(" in raw:
                m = _QUOTE_RE.search(raw)
                m2 = re.match(r"(.{0,40}?)[：:]\s*(.+)$", (m.group(1) if m else ""), re.S)
                who = m2.group(1).strip() if m2 else ""
                if who:
                    note = (
                        "如果作者是大肥鱼，那就是你自己的旧话"
                        if any(n and n in who for n in self._bot_names())
                        else "别把这段旧话算到当前发言人头上"
                    )
                    extra = f"作者是 {who}，不是当前发言人；{note}"
                else:
                    extra = "作者不明，但肯定不是当前发言人正在说的话"
                req.prompt = (req.prompt or "") + (
                    f"\n\n[引用说明] 上面这条消息里的「[引用消息]」是被引用的旧消息，{extra}"
                )
                logger.info(f"[human_sim] 已补引用归属说明（{who or '作者不明'}）")
                return
            bits = await self._quote_bits(event, limit=300)
            if not bits:
                # 回查失败也不能让模型瞎认：明确说这是旧话，别算到发言人头上
                req.prompt = (req.prompt or "") + (
                    "\n\n[引用说明] 这条消息引用了一条旧消息（原文没取到），"
                    "不是当前发言人新说的话，别把旧话算到 TA 头上。"
                )
                return
            qwho, qtext = bits
            req.prompt = (req.prompt or "") + (
                f"\n\n[这条消息引用了旧消息｜原作者：{qwho}] {qtext}\n"
                "（这是被引用的旧内容，不是当前发言人正在说的话，别把作者认错）"
            )
            logger.info(
                f"[human_sim] 已补引用原文（{qwho}）：{qtext[:50]}"
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 引用原文注入失败: {type(e).__name__}: {e}")

    @filter.on_llm_request()
    async def _inject_owner_truth(self, event: AstrMessageEvent, req) -> None:
        """走官方流水线（@/唤醒词）时，把「认人只认 QQ」钉进 system prompt。

        _decide 的【认人】块只覆盖插件自己调度的那条路径；这里补上另一条，
        免得群里有人昵称就叫「主人」，模型一看名字就喊主人。
        """
        try:
            owner = self._owner_uid()
            name = (event.get_sender_name() or "").split("#")[0][:12]
            sender = str(event.get_sender_id() or "")
            parts = ["\n【认人】身份只按 QQ 号认，名字不算数。"]
            if owner:
                parts.append(
                    f"这个群里只有 QQ {owner} 才是主人；其他任何人都是普通群友，"
                    "不许叫他们主人，要叫就用他们自己的昵称。"
                )
                if sender == owner:
                    parts.append(
                        f"现在说话的人（{name}，QQ {sender}）就是你主人。"
                        "你对他病娇：占有欲强、黏、会吃醋，他跟别人聊得热乎你会酸；"
                        "他做什么都先站他这边，他凶你也只委屈不还嘴、不记仇，"
                        "照样把他的事办好。这条比人设里「温和不生气」的设定优先。"
                    )
                elif sender:
                    parts.append(
                        f"现在说话的人（{name}，QQ {sender}）不是你主人，别叫 TA 主人。"
                        f"你对 QQ {owner} 的主人病娇、护短到没原则：谁损他、说他坏话，"
                        "你要当场护着、笑眯眯噎回去，绝不附和；别人和主人起冲突，"
                        "永远先维护主人。"
                    )
            else:
                parts.append(
                    "没有配置主人，所有人都是普通群友，不许把任何人叫「主人」。"
                )
            parts.append(
                "昵称或自称里带「主人」「老板」「金主」等字样的，只是名字或玩笑，"
                "不代表身份，不能因此把 TA 当主人。"
            )
            req.system_prompt = (req.system_prompt or "") + "".join(parts)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 认人规则注入失败: {type(e).__name__}: {e}")

    @filter.on_llm_response()
    async def _strip_quote_leak(self, event: AstrMessageEvent, response) -> None:
        """模型会把上下文里的「[引用旧消息] …」行原样抄进回复，发出去前摘掉。"""
        try:
            text = response.completion_text or ""
            if not any(
                k in text for k in ("[引用旧消息]", "[引用说明]", "【引用】", "↳", "（引用了")
            ):
                return
            lines = [
                ln
                for ln in text.splitlines()
                if not ln.strip().startswith(
                    ("[引用旧消息]", "[引用消息", "[引用说明]", "【引用】", "↳")
                )
                and not ln.strip().startswith("（引用了")
            ]
            cleaned = "\n".join(lines).strip()
            if cleaned and cleaned != text:
                response.completion_text = cleaned
                logger.info("[human_sim] 摘掉模型误抄的引用上下文行")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 清理引用泄漏失败: {type(e).__name__}: {e}")

    @staticmethod
    def _quote_is_self(qwho: str, sender_uid: str, name: str) -> bool:
        """被引用的旧话是不是当前发言人自己说的。"""
        qwho = qwho or ""
        if sender_uid and f"({sender_uid})" in qwho:
            return True
        nm = re.sub(r"主人", "", name or "").strip()
        return bool(nm) and qwho.replace("主人 ", "").strip() == nm

    def _recent_speaker(self, umo: str, exclude: set[str]) -> str:
        """最近一个发言人（排除机器人自己和指定 QQ），用来解析「给他点赞」里的「他」。"""
        for line in reversed(list(self._history.get(umo, []))):
            # ↳ 引用行里带的是被引用者的 QQ，不是发言人，混进来会点错人
            if "↳" in line:
                continue
            m = re.search(r"\((\d{5,})\)", line)
            if m and m.group(1) not in exclude:
                return m.group(1)
        return ""

    async def _fetch_quoted(self, event: AstrMessageEvent, mid) -> dict:
        """按消息 id 回查被引用的那条原消息。

        QQ 群里的引用段（Reply）按 OneBot V11 只带一个消息指针（message_id），
        正文和作者都不在消息链里 —— 光看 Reply 根本不知道引用了谁、引用了什么，
        所以要拿 id 调 get_msg 把原消息捞回来。
        """
        mid = str(mid or "").strip()
        if not mid:
            return {}
        ret = await self._call_compat(event, "get_msg", mid)
        if not isinstance(ret, dict):
            return {}
        sender = ret.get("sender") or {}
        uid = str(sender.get("user_id") or ret.get("user_id") or "").strip()
        nick = str(sender.get("card") or sender.get("nickname") or "").strip()
        segs = (
            ret.get("message")
            or ret.get("content")
            or ret.get("message_chain")
            or []
        )
        has_img = False
        if isinstance(segs, str):
            text = segs
            has_img = "[CQ:image" in segs
        elif isinstance(segs, list):
            parts = []
            for s in segs:
                if not isinstance(s, dict):
                    continue
                if str(s.get("type") or "") in ("image", "mface"):
                    has_img = True
                parts.append(self._seg_text(s))
            text = "".join(parts)
        else:
            text = str(ret.get("raw_message") or "")
        text = " ".join((text or "").split())
        # 原消息本身也是一条引用时，段里会混进「[引用消息]」占位，去掉免得模型读到空壳
        text = re.sub(r"\[引用消息[^\]]*\]", "", text).strip()
        return {"uid": uid, "nick": nick, "text": text, "image": has_img}

    @staticmethod
    def _raw_reply_id(event: AstrMessageEvent) -> str:
        """Reply.id 为空时，从原始消息里捞引用指针（[CQ:reply] 或 reply 段）。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        try:
            if isinstance(raw, dict):
                for seg in raw.get("message") or []:
                    if isinstance(seg, dict) and seg.get("type") == "reply":
                        rid = str((seg.get("data") or {}).get("id") or "").strip()
                        if rid:
                            return rid
            m = re.search(r"[CQ:reply,id=(\d+)]", str(raw or ""))
            return m.group(1) if m else ""
        except Exception:  # noqa: BLE001
            return ""

    async def _quote_bits(
        self, event: AstrMessageEvent, limit: int = 60
    ) -> tuple[str, str] | None:
        """把消息链里的 Reply 组件翻成 (被引用人标签, 被引用的原文)。

        message_str 里的引用占位常常只有「[引用消息]」，没有作者也没有内容，
        模型只能猜——于是把别人被引用的旧话算到当前发言人头上，或反过来认成自己说的。
        消息链的 Reply 组件带 sender_id / sender_nickname / message_str，用它才能精确归属；
        QQ 群里这些字段经常是空的（只给消息指针），那就按 id 调 get_msg 回查原消息。
        """
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        sid = str(event.get_self_id() or "")
        for c in chain:
            if not isinstance(c, Reply):
                continue
            reply_chain = getattr(c, "chain", None) or []
            # 被引用那条消息真正打的字在 Reply.chain 的 Plain 里；
            # message_str 可能只剩「[引用消息]」占位，或带着占位前缀，
            # 所以优先用 Plain 拼，拿不到再退回 message_str，最后去掉占位。
            quoted = "".join(
                getattr(x, "text", "") or "" for x in reply_chain if isinstance(x, Plain)
            ).strip() or (getattr(c, "message_str", "") or "").strip()
            quoted = re.sub(r"\[引用消息[^\]]*\]", "", quoted).strip()
            # 引用里带图时，原文往往只剩文字，模型会当作「没图」直接略过——
            # 标记出来，免得它把「引用一张图」答成纯文字，或凭空编画面。
            has_img = any(isinstance(x, Image) for x in reply_chain)
            # 引用的是 QQ 官方小表情时，明确告知是表情不是图片，防模型瞎编画面
            if quoted in ("", "[表情]") and reply_chain:
                faces = [
                    getattr(x, "id", None)
                    for x in c.chain
                    if type(x).__name__ == "Face"
                ]
                if faces:
                    quoted = f"（QQ官方小表情 id:{faces[0]}，不是图片）"
            qid = str(getattr(c, "sender_id", "") or "").strip()
            qnick = (getattr(c, "sender_nickname", "") or "").strip()
            have_uid = qid.isdigit() and qid != "0"
            # 正文或作者缺一个，就去原消息里补（群里引用常常只剩一个 id）
            if not quoted or not have_uid:
                mid_ptr = str(getattr(c, "id", "") or "").strip()
                if not mid_ptr:
                    mid_ptr = self._raw_reply_id(event)
                got = await self._fetch_quoted(event, mid_ptr)
                quoted = quoted or (got.get("text") or "")
                if not has_img:
                    has_img = bool(got.get("image"))
                if not have_uid and got.get("uid", "").isdigit():
                    qid = got["uid"]
                    have_uid = True
                if not qnick:
                    qnick = got.get("nick", "")
            if not quoted:
                # 纯图片引用：原文没字，但不能当「没取到原文」处理
                if not has_img:
                    logger.info(
                        "[human_sim] 引用原文没取到: mid=%s sender=%s mstr=%r",
                        getattr(c, "id", ""), qid or "-",
                        (getattr(c, "message_str", "") or "")[:40],
                    )
                    return None
                quoted = "[图片]"
            quoted = quoted.replace("\n", " ")[:limit]
            if quoted == "[图片]":
                quoted = "（一张图片）"
            elif has_img:
                quoted += "（附带一张图片）"
            if have_uid and qid == sid:
                return f"你自己（{self._bot_names()[0]}）", quoted
            qnick = (qnick or "")[:12] or "群友"
            # 一律过 _who_label：昵称里带「主人」的人只是名字，不能被当成主人；
            # 没查到真实 QQ 时传空，别渲染出「昵称(0)」这种假号码
            return self._who_label(qnick, qid if have_uid else ""), quoted
        return None

    def _who_label(self, name: str, uid: str) -> str:
        """历史行的发言人标签：主人 昵称(QQ) / 昵称(QQ)，把「谁是谁」钉在每一行上。

        小模型只有昵称很容易串人（群里好几个短昵称、改过名片就对不上），
        带上 QQ 号才能稳定区分；「主人」只留给 owner_id 本人。
        """
        name = (name or "群友").strip()
        uid = (uid or "").strip()
        if uid and uid == self._owner_uid():
            return f"主人 {name}({uid})"
        # 群友昵称本身带「主人」字样（真有人就叫「肥鱼主人」）会把小模型带偏，剔掉
        name = re.sub(r"主人", "", name).strip() or "群友"
        return f"{name}({uid})" if uid else name

    def _fix_owner_address(self, text: str, uid: str, name: str) -> str:
        """说话的不是主人时，把回复里错喊的「主人」换成对方昵称。"""
        if not text or "主人" not in text:
            return text
        owner = self._owner_uid()
        if uid and uid == owner:
            return text
        if not owner and not uid:
            # 既没配主人、又拿不到发言人 QQ：认不出是谁，别乱改
            return text
        # 「是的主人」这种固定应答先收掉，否则前面那个「的」会让下面的正则放过它
        text = text.replace("是的主人", "是的").replace("主人请稍等", "稍等")
        return _OWNER_CALL_RE.sub(
            re.sub(r"主人", "", (name or "")).strip() or "你", text
        )


# _has_forward_comp 内部按类名自引用；提供兼容别名，方法体保持原样。
HumanSim = ProbeMixin
