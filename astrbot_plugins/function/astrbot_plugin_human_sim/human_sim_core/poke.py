"""拍一拍：被拍回应、台词池、主动拍人（纯规则，不进 AI）。"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import aiohttp

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

from .prompts import GRUDGE_TTL, POKE_ACK_LINES, POKE_AFFECTION_RE, POKE_COLD_LINES, POKE_LINES, POKE_LINES_NAMED, POKE_REPEAT_LINES, POKE_REPEAT_LINES_NAMED
from .text import POKE_REQUEST_RE

# 桌宠同步：戳戳是 notice 动作，不走消息管道，桌宠插件看不见；
# 这里直接 POST 给桌宠中转（23334），让桌宠也能「看见」戳戳互动。
# 即发即忘，失败只写 debug 日志，绝不影响 QQ 消息链路。
PET_RELAY_URL = os.environ.get("PET_RELAY_URL", "http://127.0.0.1:23334/state")

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class PokeMixin:

    def _pet_poke_push(
        self, what: str, detail: str, *, sender_name: str, sender_id: str = "",
        is_self: bool = False, is_group: bool = False, group_id: str = "",
    ) -> None:
        """把戳戳动作推给桌宠（pet-relay /state），即发即忘。"""
        payload = {
            "source": "qq",
            "state": "working" if is_self else "idle",
            "event": what,
            "detail": str(detail or "")[:400],
            "sender_name": str(sender_name or "").strip() or ("肥鱼" if is_self else "群友"),
            "sender_id": str(sender_id or ""),
            "is_self": is_self,
            "group_id": str(group_id or ""),
            "chat_type": "group" if is_group else "private",
        }

        async def _post():
            try:
                timeout = aiohttp.ClientTimeout(total=5)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.post(PET_RELAY_URL, json=payload) as resp:
                        await resp.text()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[human_sim] 戳戳推送桌宠失败: {type(e).__name__}: {e}")

        try:
            asyncio.get_running_loop().create_task(_post())
        except RuntimeError:
            pass

    async def _do_poke(
        self, umo: str, user_id: str, is_group: bool, bot=None, note: bool = True
    ) -> bool:
        """发一次拍一拍。成功返回 True。

        NapCat 的 group_poke / friend_poke 是 OneBot 扩展 action；多客户端连接时
        必须带 self_id 才路由得到（api_impl.py:131）。
        """
        uid = str(user_id or "").strip()
        if not uid.isdigit():
            return False
        client = bot or self._bots.get(umo)
        if client is None:
            logger.warning(f"[human_sim] 拍一拍失败：没有可用的 bot 客户端（{umo}）")
            return False
        sid = str(self.state.get(f"self_id:{umo}", "") or "")
        try:
            if is_group:
                gid = umo.rsplit(":", 1)[-1]
                if not gid.isdigit():
                    return False
                kwargs = {"group_id": int(gid), "user_id": int(uid)}
                if sid:
                    kwargs["self_id"] = sid
                await client.api.call_action("group_poke", **kwargs)
            else:
                kwargs = {"user_id": int(uid)}
                if sid:
                    kwargs["self_id"] = sid
                await client.api.call_action("friend_poke", **kwargs)
            # 拍出去了就记进上下文（带目标主体）：回戳/主动拍/「拍拍我」三个入口全走这里
            # 斜杠指令触发的拍一拍不写回（/拍我 由命令层传 note=False）
            if note:
                names = self.cfg.get("bot_names") or ["大肥鱼"]
                me_name = str(names[0]) if names else "大肥鱼"
                try:
                    tn = await self._member_name(umo, uid)
                except Exception:
                    tn = uid
                # 历史每行必须是「发言人：内容」，动作也要挂在发言人身上；
                # 昵称里带「主人」的只是名字，别把拍的对象写成一个假主人
                tn = re.sub(r"主人", "", str(tn or "")).strip() or "群友"
                self._history.setdefault(umo, deque(maxlen=30)).append(
                    f"{me_name}：[拍了拍 {tn}]"
                )
            else:
                names = self.cfg.get("bot_names") or ["大肥鱼"]
                me_name = str(names[0]) if names else "大肥鱼"
                tn = uid
            self._poke_stat_bump("out")
            # 戳出去的动作也让桌宠看见（回戳/主动拍/「拍拍我」共用这条链）
            self._pet_poke_push(
                "拍了拍",
                f"拍了拍 {tn}",
                sender_name=me_name,
                sender_id=sid,
                is_self=True,
                is_group=is_group,
                group_id=(umo.rsplit(":", 1)[-1] if is_group else ""),
            )
            if sid:
                self._poke_pair_bump(sid, uid, poker_name=me_name, target_name=tn)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 拍一拍调用失败（{type(e).__name__}）: {e}")
            return False

    def _poke_stat_bump(self, direction: str) -> None:
        """戳戳计数：direction=in 被戳 / out 戳出去。

        只记当日；_save_state 里保留今天与昨天，更早的自动清掉。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"poke_stat:today:{today}:{direction}"
        try:
            self.state[key] = int(self.state.get(key, 0) or 0) + 1
        except (TypeError, ValueError):
            self.state[key] = 1
        self._save_state()

    def _poke_pair_bump(
        self,
        poker: str,
        target: str,
        poker_name: str = "",
        target_name: str = "",
    ) -> None:
        """按人对记戳戳账：poke_stat:ptoday:{日期}:{戳人QQ}:{被戳QQ}。

        「戳戳统计」只按查询对象（默认调用方，可用 @ 指定）展示相关配对，
        其他人的配对不显示。
        只记当日，_save_state 保留今天与昨天；昵称快照存 poke_stat:name:{uid}，
        私聊等拿不到群名片的场合用它兜底。
        """
        poker, target = str(poker or ""), str(target or "")
        if not poker or not target:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"poke_stat:ptoday:{today}:{poker}:{target}"
        try:
            self.state[key] = int(self.state.get(key, 0) or 0) + 1
        except (TypeError, ValueError):
            self.state[key] = 1
        for uid, nm in ((poker, poker_name), (target, target_name)):
            nm = str(nm or "").strip()
            if nm and not nm.isdigit():
                self.state[f"poke_stat:name:{uid}"] = nm[:12]
        self._save_state()

    # ---------- 被拍台词池：本地随机挑，模型负责补新、删旧 ----------
    def _poke_pool_key(self, repeat: bool) -> str:
        return "poke_lines:repeat" if repeat else "poke_lines:normal"

    def _poke_pool(self, repeat: bool) -> list[dict]:
        """取台词池；第一次用内置台词播种，之后由模型陆续替换。"""
        key = self._poke_pool_key(repeat)
        pool = self.state.get(key)
        if isinstance(pool, list) and pool:
            return pool
        now = int(time.time())
        ttl = max(1, int(self.cfg.get("poke_pool_ttl_days", 30))) * 86400
        seeds = (
            POKE_REPEAT_LINES_NAMED + POKE_REPEAT_LINES
            if repeat
            else POKE_LINES_NAMED + POKE_LINES
        )
        # 播种时间错开，老台词才会陆续过期，不会同一天集体消失
        pool = []
        for i, t in enumerate(seeds):
            age = int(ttl * 0.9 * (len(seeds) - i) / max(1, len(seeds)))
            pool.append({"t": t, "ts": now - age})
        self.state[key] = pool
        return pool

    def _prune_poke_pool(self, repeat: bool) -> list[dict]:
        """删掉太老的、空的和重复的，只留最新的最多 poke_pool_max 条。"""
        key = self._poke_pool_key(repeat)
        pool = self._poke_pool(repeat)
        ttl = max(1, int(self.cfg.get("poke_pool_ttl_days", 30))) * 86400
        now = int(time.time())
        seen = set()
        fresh: list[dict] = []
        for e in pool:
            if isinstance(e, dict):
                t = str(e.get("t") or "").strip()
                try:
                    ts = int(e.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0
            else:  # 兼容旧格式：纯字符串
                t, ts = str(e or "").strip(), now
            if not t or t in seen or now - ts > ttl:
                continue
            seen.add(t)
            fresh.append({"t": t[:30], "ts": ts})
        fresh.sort(key=lambda e: e["ts"])
        fresh = fresh[-max(4, int(self.cfg.get("poke_pool_max", 30))):]
        self.state[key] = fresh
        return fresh

    def _poke_grudge(self, umo: str, uid: str) -> bool:
        """这个人是不是还在让肥鱼不爽（被骂过，GRUDGE_TTL 内）。

        时间一过就自然消气；戳戳是他被骂/被烦之后最容易被看穿的场合，
        这里得跟上脾气，不能一边记着仇一边回「好喜欢你」。
        """
        key = f"{umo}|{uid}"
        ts = self._grudge.get(key)
        if not ts:
            return False
        if time.time() - ts > GRUDGE_TTL:
            self._grudge.pop(key, None)
            return False
        return True

    def _poke_pick_line(
        self, umo: str, poker_name: str, is_group: bool, repeat: bool,
        cold: bool = False,
    ) -> str:
        recent = set(self._poke_used.get(umo) or ())
        render = lambda t: t.replace("{n}", poker_name or "你")  # noqa: E731
        if cold:
            texts = [t for t in POKE_COLD_LINES if render(t) not in recent]
            return render(random.choice(texts or POKE_COLD_LINES))
        pool = self._prune_poke_pool(repeat)
        if not pool:  # 全过期了，重新用内置台词播种，别让回话崩掉
            pool = self._poke_pool(repeat)
        named = [
            e["t"] for e in pool
            if "{n}" in e["t"] and not POKE_AFFECTION_RE.search(e["t"])
        ]
        plain = [
            e["t"] for e in pool
            if "{n}" not in e["t"] and not POKE_AFFECTION_RE.search(e["t"])
        ]
        # 池子被模型写满了撒娇句时别把回话搞没，退回只滤名字的版本
        if not named and not plain:
            named = [e["t"] for e in pool if "{n}" in e["t"]]
            plain = [e["t"] for e in pool if "{n}" not in e["t"]]
        # 最近刚说过的先排掉，免得同一句反复出现（全排空了再退回原池）
        named = [t for t in named if render(t) not in recent] or named
        plain = [t for t in plain if render(t) not in recent] or plain
        if poker_name and is_group:
            # 认识人时优先带名字的台词，偶尔用通用句，免得听着像复读
            texts = named if named and random.random() >= 0.25 else (plain or named)
        else:
            texts = plain or named
        return render(random.choice(texts))

    def _poke_remember(self, umo: str, line: str) -> None:
        """记住刚说过的被拍台词，后面挑台词时先避开这几句。"""
        line = (line or "").strip()
        if not line:
            return
        self._poke_used.setdefault(umo, deque(maxlen=6)).append(line)

    async def _poke_ai_line(
        self, umo: str, poker_name: str, is_group: bool, repeat: bool,
        cold: bool, nth: int = 1,
    ) -> str:
        """看最近聊天记录和当前会话上下文，现写一句被拍反应；拿不到/不合适就返回空，交给本地池兜底。"""
        try:
            hist = list(self._history.get(umo) or ())[-10:]
            scene = "\n".join(hist) if hist else "（这会儿没人说话）"
            recent = list(self._poke_used.get(umo) or ())[-4:]
            tips = [
                "你刚被人戳了一戳（拍一拍），接着刚才的聊天随口回一句，只写这一句。",
                "最近的聊天记录（你也在场，顺着这个话头说）：",
                scene,
                f"戳你的是 {poker_name or '对方'}，这是 TA 短时间内第 {nth} 次戳你。",
                "要求：口语化、8~20 个字、只写一句；不要引号、不要解释、不要表情符号；"
                "如果刚才在聊某件事，就着那件事回（接话、催一句、逗一句都行），"
                "别自顾自说跟上下文无关的话；没事聊就随手丢一句反应。",
                "每次换个说法，别总用同一套。",
            ]
            if is_group:
                # 群里被戳只当社交动作，不往暧昧上带
                tips.append("被戳是嫌烦不是表白，不许撒娇、不许表白、不要说喜欢/爱你这类词。")
            if repeat:
                tips.append("TA 已经连戳你好几下，语气不耐烦一点，但别真生气。")
            if cold:
                tips.append("TA 刚惹过你，你还带着气：回得冷一点、短一点，别心软。")
            if recent:
                tips.append("别跟这几句重复：" + "、".join(recent))
            raw = await asyncio.wait_for(
                self._generate("\n".join(tips), umo, limit=24), timeout=6.0
            )
            line = (raw or "").strip().strip("“”\"'`")
            line = re.sub(r"[\[\]\r\n]", "", line).strip()
            if not line or "[SILENT]" in line.upper() or line in recent:
                return ""
            if (is_group or cold) and POKE_AFFECTION_RE.search(line):
                return ""
            return line[:30]
        except asyncio.TimeoutError:
            logger.info("[human_sim] 被拍台词现写超时，改用本地台词池")
            return ""
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 被拍台词现写失败: {type(e).__name__}: {e}")
            return ""

    async def _poke_reply_text(
        self, umo: str, poker_name: str, is_group: bool, repeat: bool,
        cold: bool, nth: int = 1,
    ) -> str:
        """被拍要说一句时优先让模型现写，失败再用本地池兜底；说过的话记下来防复读。"""
        line = ""
        if self.cfg.get("poke_line_ai", True):
            line = await self._poke_ai_line(
                umo, poker_name, is_group, repeat, cold, nth
            )
        if not line:
            line = self._poke_pick_line(umo, poker_name, is_group, repeat, cold=cold)
        self._poke_remember(umo, line)
        return line

    def _maybe_refill_poke_pool(
        self, umo: str, poker_name: str, is_group: bool, repeat: bool
    ) -> None:
        """池子不够（少了 / 太老被清掉）就让模型补一条，后台跑，不挡这次回话。"""
        if not self.cfg.get("poke_line_ai", True):
            return
        pool = self._prune_poke_pool(repeat)
        if len(pool) >= max(1, int(self.cfg.get("poke_pool_min", 8))):
            return
        if getattr(self, "_poke_refilling", False):
            return
        self._poke_refilling = True
        asyncio.create_task(self._ai_poke_line(umo, poker_name, is_group, repeat))

    async def _ai_poke_line(
        self, umo: str, poker_name: str, is_group: bool, repeat: bool
    ) -> None:
        key = self._poke_pool_key(repeat)
        try:
            pool = self._prune_poke_pool(repeat)
            existing = [str(e.get("t") or "") for e in pool][-10:]
            tips = [
                "刚被人拍了一拍（戳一戳）。",
                "只写一句被戳之后的反应，口语化、像真人随手回的，不超过 15 个字。",
                "不要解释、不要引号、不要写成小作文，也不要带表情符号。",
                "别撒娇、别表白：不许出现喜欢、爱你、贴贴、抱抱、想你这类词，"
                "被戳就是嫌烦，最多损一句。",
            ]
            if repeat:
                tips.append("同一人短时间内连拍你好几次，语气不耐烦一点，但别真生气。")
            if poker_name and is_group:
                tips.append("戳你的人叫 {n}；请用 {n} 占位代表名字，不要写具体昵称。")
            if existing:
                tips.append("别跟这些已有的重复：" + "、".join(existing))
            raw = await self._generate("\n".join(tips), umo, limit=20)
            line = (raw or "").strip().strip("“”\"'")
            line = line.splitlines()[0].strip() if line else ""
            line = re.sub(r"[\[\]\r\n]", "", line)[:30].strip()
            if not line or "[SILENT]" in line.upper():
                return
            pool = self._prune_poke_pool(repeat)
            if any(str(e.get("t")) == line for e in pool):
                return
            pool.append({"t": line, "ts": int(time.time())})
            self.state[key] = pool
            self._save_state()
            logger.info(f"[human_sim] 模型补充被拍台词：{line}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 被拍台词生成失败: {type(e).__name__}: {e}")
        finally:
            self._poke_refilling = False

    def _poke_into_chat(
        self, event: AstrMessageEvent, poker: str, poker_name: str, is_group: bool
    ) -> None:
        """把一戳改写成像样的用户消息，交给正式会话回。

        Poke 事件的 message_str 只是占位串（空或 [ComponentType.Poke]），直接放行
        会在 LLM 阶段被当成空消息跳过；这里补一句「戳了戳你」，并按 face_convert
        的格式带上身份标记（主人/群友），再把唤醒标记置真，正式流水线就会按
        完整上下文和人格接这一戳。
        """
        sid = str(event.get_sender_id() or poker or "").strip()
        name = re.sub(r"[\[\]\r\n主人]", "", str(poker_name or "")).strip()[:16]
        try:
            is_owner = self._is_manager(event)
        except Exception:  # noqa: BLE001
            is_owner = False
        if is_owner:
            tag = f"主人({sid})"
        elif is_group:
            tag = f"群友 {name}({sid})" if name else f"群友({sid})"
        else:
            tag = f"非主人 {name}({sid})" if name else f"非主人({sid})"
        text = f"[{tag}] （{name or '对方'}戳了戳你）"
        event.message_str = text
        try:
            event.message_obj.message_str = text
        except Exception:  # noqa: BLE001
            pass
        # 已经有了身份标记，别再让后面的插件补一遍
        event.set_extra("_fy_tagged", True)
        # 标记「这一戳是主动交给正式会话的」，_record 看到后别再按死唤醒标记
        event.set_extra("_hs_poke_to_chat", True)
        # 群聊里 Poke 只算「插件激活」，不会置成唤醒指令；这里强制放行给 LLM 阶段
        event.is_wake = True
        event.is_at_or_wake_command = True

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _on_poke(self, event: AstrMessageEvent):
        """被别人拍时的回应：戳回去 / 冒一句 / 甩表情包。

        被拍要立刻反应，所以台词走本地池随机挑；池子太老/太少时后台让模型补一条新的。
        被拍必回（冷却内除外），同一个人连拍则必回戳。
        """
        poke = None
        try:
            for comp in event.get_messages():
                if type(comp).__name__ == "Poke":
                    poke = comp
                    break
        except Exception:
            return
        if poke is None:
            return
        self._poke_seen += 1
        self._poke_seen_at = time.time()
        mobj = event.message_obj
        self_id = str(getattr(mobj, "self_id", "") or "")
        # OneBot 戳一戳 notice：user_id / sender_id 是「发起者」，target_id 是「被戳者」。
        # 有些适配器会把 Poke 段里的 id 填成发起者，只读 target_id() 会把「谁戳的」认反；
        # 优先读原始事件字段，取不到再退回组件。
        raw = getattr(mobj, "raw_message", None)
        if not isinstance(raw, dict):
            raw = {}
        try:
            comp_target = str(poke.target_id() or "")
        except Exception:  # noqa: BLE001
            comp_target = ""
        target = str(raw.get("target_id") or comp_target or "")
        poker = str(
            raw.get("user_id")
            or raw.get("sender_id")
            or event.get_sender_id()
            or ""
        )
        # MessageType 是 Enum，取 .value 拿 "GroupMessage"/"FriendMessage"；
        # 直接 str() 会得到 "MessageType.GROUP_MESSAGE"，虽然也能匹配但容易写错
        raw_type = getattr(mobj, "type", "")
        mtype = str(getattr(raw_type, "value", raw_type) or "")
        is_group = "Group" in mtype
        umo = event.unified_msg_origin
        # 戳戳统计：只算「别人戳我」；自己戳出去的回显由 _do_poke 记过，不重复计
        if self_id and target == self_id and poker and poker != self_id:
            self._poke_stat_bump("in")
        # 配对账：别人戳谁都记（含群友互戳）；自己戳出去的回显由 _do_poke 记过
        if self_id and poker and target and poker != self_id:
            try:
                _pn = re.sub(
                    r"[\[\]\r\n]", "", (event.get_sender_name() or "").strip()
                )[:12]
            except Exception:  # noqa: BLE001
                _pn = ""
            self._poke_pair_bump(poker, target, poker_name=_pn)
        if self._is_muted(umo):
            return
        # 默认由本插件本地处理，别让唤醒阶段的「空唤醒」把 Poke 当成一句话接走：
        # 模型会把拍一戳当普通消息接话，群里于是多出一条跟拍一戳对不上的回复。
        # 但下面按 poke_to_chat_probability 有一定概率把这一戳交给正式会话，
        # 那时会由 _poke_into_chat 补好文本和身份标记、再把唤醒标记置真。
        event.is_at_or_wake_command = False
        event.is_wake = False
        logger.debug(
            f"[human_sim] 拍一戳事件：umo={umo} type={mtype} "
            f"group={event.get_group_id()} target={target} poker={poker}"
        )
        if not self.cfg.get("poke_respond", True):
            return
        # 只回应「别人拍我」；自己拍别人、拍别人都不归我管
        if not self_id or target != self_id or not poker or poker == self_id:
            if poker == self_id:
                # 自己戳出去的动作被回显，正常现象，不用占日志
                logger.debug(f"[human_sim] 跳过自己发出的拍一拍（target={target}）")
                return
            now_dbg = time.time()
            if now_dbg - float(self.state.get("poke_dbg_at", 0) or 0) > 60:
                self.state["poke_dbg_at"] = now_dbg
                logger.info(
                    f"[human_sim] 收到拍一拍但不是我：target={target} self={self_id} "
                    f"poker={poker}（事件已进来，说明入站链路是通的）"
                )
            return
        poker_name = ""
        try:
            poker_name = re.sub(
                r"[\[\]\r\n]", "", (event.get_sender_name() or "").strip()
            )[:12]
        except Exception:  # noqa: BLE001
            poker_name = ""
        if poker_name.isdigit() or poker_name in ("？", "?", "未知", "unknown"):
            # 取不到昵称时别拿问号当名字喊人（「？ 戳啥呢」看着像骂人）
            poker_name = ""

        umo = event.unified_msg_origin
        if is_group:
            if umo not in self.cfg["watch_groups"]:
                return
        elif "Friend" not in mtype:
            return
        bot = getattr(event, "bot", None)
        if bot is not None:
            self._bots[umo] = bot
        now = time.time()
        tag = umo.rsplit(":", 1)[-1]
        pair = f"{umo}|{poker}"
        # 还在气头上的人来戳：回话必须冷，不能嘴上记仇、戳戳又撒娇
        cold = self._poke_grudge(umo, poker)
        # 同一个人短时间内连着拍 = 在闹，必回戳（无视群冷却）
        window = float(self.cfg.get("poke_repeat_window_seconds", 180))
        seq = self._poke_seq.get(pair) or {}
        repeat = now - float(self._poke_last.get(pair, 0) or 0) < window
        # 同一个人这段时间里戳到第几次：每次被戳都累计，不只在回应时算
        nth = int(seq.get("n") or 0) + 1 if repeat else 1
        self._poke_seq[pair] = {"n": nth, "ts": now}
        self._poke_last[pair] = now
        # 第一次被戳必回（冷却内除外）；同一人连戳时才按 n/3 抽，越戳越容易理，
        # 免得有人一直点把聊天刷屏
        if repeat and random.random() >= min(1.0, nth / 3.0):
            logger.info(f"[human_sim] 被拍第 {nth} 次，这次仍不理（{tag}）")
            return
        if not repeat and now - float(self._poke_last.get(umo, 0) or 0) < float(
            self.cfg.get("poke_cooldown_seconds", 20)
        ):
            logger.info(f"[human_sim] 拍一拍冷却中，先不理（{tag}）")
            return
        self._poke_last[umo] = now
        logger.info(f"[human_sim] 被拍（{tag}，来自 {poker}{'，连拍' if repeat else ''}）")
        # notice 事件里的 sender 昵称常被填成 QQ 号，直接拿来做台词会变成
        # 「谁在戳我」这种无名句；这里补一次群名片解析，跟主输入那条记录的叫法保持一致
        if not poker_name and is_group and poker.isdigit():
            try:
                poker_name = (await self._member_name(umo, poker) or "")[:12]
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[human_sim] 拍一拍取名失败: {type(e).__name__}: {e}")
                poker_name = ""
        # 「被戳」先推桌宠再决定要不要回应：就算这次概率/冷却没接话，桌宠也知道被戳了
        self._pet_poke_push(
            "被拍一拍",
            f"{poker_name or '有人'} 戳了你一下",
            sender_name=poker_name or "群友",
            sender_id=poker,
            is_group=is_group,
            group_id=(umo.rsplit(":", 1)[-1] if is_group else ""),
        )
        # 有时候别按本地台词应付：把这一戳交给正式会话，让它带着完整上下文和人格回
        # （私聊会写进会话记忆；群聊则临时唤醒一次）。冷战状态仍走本地冷话。
        into_chat_p = float(self.cfg.get("poke_to_chat_probability", 0.3) or 0)
        if not cold and into_chat_p > 0 and random.random() < into_chat_p:
            self._poke_into_chat(event, poker, poker_name, is_group)
            logger.info(
                f"[human_sim] 这戳交给正式会话处理（{tag}，来自 {poker}）"
            )
            return
        await asyncio.sleep(random.uniform(0.3, 0.9))  # 真人不会秒回戳
        back_p = 1.0 if repeat else float(
            self.cfg.get("poke_back_probability", 0.75)
        )
        text_p = float(self.cfg.get("poke_text_probability", 0.55))
        poked_back = False
        if random.random() < back_p:
            poked_back = await self._do_poke(umo, poker, is_group, bot)
        text = ""
        if cold or random.random() < text_p:
            text = await self._poke_reply_text(
                umo, poker_name, is_group, repeat, cold, nth
            )
        # 保底：两个开关都是开着的、却都没抽中 → 至少冒一句。
        # 被人拍完全没反应不像活人；但如果用户把两个概率都设成 0，那就是真心不想要话，别硬塞。
        if not poked_back and not text and (back_p > 0 or text_p > 0):
            text = await self._poke_reply_text(
                umo, poker_name, is_group, repeat, cold, nth
            )
        # 台词池不够或太老被清掉时，后台请模型补一条新的，不耽误这次回话
        if not cold:
            self._maybe_refill_poke_pool(umo, poker_name, is_group, repeat)
        if text:
            try:
                await self._send(umo, [Plain(text)])
                self._note_act(umo, text, spoken=True)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[human_sim] 拍一拍应答发送失败: {type(e).__name__}: {e}")
        # 还在气头上就不甩表情包：随机抽一张可爱的出去，人设当场崩
        if not cold and self._stickers and random.random() < float(
            self.cfg.get("poke_sticker_probability", 0.35)
        ):
            # 表情包单独成条，跟真人一样；发什么交给模型按气氛挑
            await asyncio.sleep(random.uniform(0.6, 1.5))
            await self._poke_send_sticker(umo, poker_name)

    async def _poke_send_sticker(self, umo: str, poker_name: str) -> None:
        """被拍之后的表情包：让模型按当下气氛挑一个，挑不出来就不发。

        以前这里从表情目录随机抽一张，机器人还在生气也会甩个可爱的出去。
        现在统一走 _ai_pick_emoji，模型说「不发」就真的不发。
        """
        scene = f"{poker_name or '对方'}又戳了你一下"
        pick = await self._ai_pick_emoji(umo, scene)
        if not pick:
            return
        kind, name = pick
        try:
            if kind == "super":
                sent = await self._send_super_series(umo, [name])
                if sent:
                    self._note_act(umo, f"发了超级表情：{'、'.join(sent)}")
                return
            path = next((p for p, n, _ in self._sticker_meta if n == name), None)
            if not path:
                return
            await self._send(umo, [Image.fromFileSystem(path)])
            self._note_act(umo, f"发了表情包：{self._sticker_desc(path)}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 拍一拍表情发送失败: {type(e).__name__}: {e}")

    async def _maybe_active_poke(
        self, umo: str, user_id: str, source: str = "reply", bot=None, boost: float = 1.0
    ) -> bool:
        """主动拍某个群友：只在刚跟他聊完（回完他的话）顺手拍一下。

        不聊天就别戳人：冷场巡查、潜水看到别人说话、跟着群友 +1 都不再戳，
        除非对方先戳了自己（被拍回应在 _handle_poke 里，不受这里限制）。
        """
        if source != "reply":
            return False
        if not self.cfg.get("poke_active", True):
            return False
        uid = str(user_id or "").strip()
        if not uid.isdigit() or uid == str(self.state.get(f"self_id:{umo}", "") or ""):
            return False
        # 还在气头上的人别主动去拍，真人不会上赶着找讨厌的人互动
        if self._poke_grudge(umo, uid):
            return False
        p = float(self.cfg.get("poke_active_probability", 0.18))
        p = min(1.0, p * max(0.0, float(boost or 1.0)))
        self._poke_rolls += 1
        if random.random() >= p * self._night_scale():
            return False
        now = time.time()
        last = float(self.state.get(f"last_active_poke:{umo}", 0) or 0)
        if now - last < int(self.cfg.get("poke_active_cooldown_minutes", 20)) * 60:
            self._poke_cools += 1
            return False
        self.state[f"last_active_poke:{umo}"] = now
        self._save_state()
        await asyncio.sleep(random.uniform(1.0, 4.0))
        ok = await self._do_poke(umo, uid, ":GroupMessage:" in umo, bot)
        if ok:
            self._poke_hits += 1
            logger.info(
                f"[human_sim] 主动拍一拍 -> {uid}（{source}，{umo.rsplit(':', 1)[-1]}）"
            )
        else:
            self._poke_fails += 1
            # 没拍出去就别占着冷却额度，否则一次失败要等 20 分钟才有下次机会
            self.state.pop(f"last_active_poke:{umo}", None)
            self._save_state()
        return ok

    async def _handle_poke_request(self, event: AstrMessageEvent) -> bool:
        """「拍拍我 / 戳我」这种纯动作：本地直接拍回去，根本不进 AI。

        返回 True 表示已处理，调用方应 stop_event 把消息截下来。
        """
        if not self.cfg.get("poke_request_local", True):
            return False
        text = (event.message_str or "").strip()
        if not text or len(text) > 8 or not POKE_REQUEST_RE.match(text):
            return False
        umo = event.unified_msg_origin
        is_group = bool(event.get_group_id())
        if is_group and umo not in self.cfg["watch_groups"]:
            return False
        uid = str(event.get_sender_id() or "")
        if not await self._do_poke(umo, uid, is_group, getattr(event, "bot", None)):
            return False
        logger.info(f"[human_sim] 纯动作请求「{text}」本地处理，不进 AI")
        await asyncio.sleep(random.uniform(0.4, 1.2))
        line = (
            random.choice(POKE_COLD_LINES).replace("{n}", "你")
            if self._poke_grudge(umo, uid)
            else random.choice(POKE_ACK_LINES)
        )
        if line:
            try:
                await self._send(umo, [Plain(line)])
                self._note_act(umo, line, spoken=True)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[human_sim] 拍一拍应答失败: {type(e).__name__}: {e}")
        return True

    # ---------- 私聊也认「拍拍我」这种纯动作请求 ----------
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def _poke_request_private(self, event: AstrMessageEvent):
        try:
            # 私聊里要表情 / 超级表情：先让模型按场景挑，挑不出来再走硬规则
            if await self._handle_emoji_request(event):
                event.stop_event()
                return
            if await self._handle_super_face_request(event):
                event.stop_event()
                return
            if await self._handle_sticker_request(event):
                event.stop_event()
                return
            # 点赞统一走 /赞我 命令，输入层口语点赞链路已永久关闭
            if await self._handle_poke_request(event):
                event.stop_event()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 私聊拍一拍处理异常: {type(e).__name__}: {e}")
