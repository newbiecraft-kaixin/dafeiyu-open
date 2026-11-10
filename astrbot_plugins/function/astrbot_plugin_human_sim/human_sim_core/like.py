"""点赞：目标解析、发送、回话与兜底台词。"""

from __future__ import annotations

import asyncio
import random
import re
import time
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

from .prompts import LIKE_ACK_LINES
from .text import _HONORIFIC_RE, _LIKE_BOT_WORDS, _LIKE_PRONOUNS, _LIKE_SELF_WORDS, _LIKE_TARGET_RE, _LIKE_VERB_RE, _norm_like_text

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class LikeMixin:

    def _at_uids(self, event: AstrMessageEvent) -> list[str]:
        """消息里 @ 了谁的 uid 列表（不含全体）。@段在正文里被归一化吃掉了，
        但组件还在 —— 「给 @忘川 点赞」的目标只能从这里拿。"""
        out: list[str] = []
        try:
            for c in event.message_obj.message or []:
                if isinstance(c, At):
                    qq = str(getattr(c, "qq", "") or "")
                    if qq.isdigit() and qq != "all":
                        out.append(qq)
        except Exception:  # noqa: BLE001
            pass
        return out

    async def _resolve_member_uid(self, umo: str, raw_name: str) -> str:
        """把「万川姐姐」这种叫法解析成群成员 uid。

        精确命中 > 剥称呼后命中 > 互相包含 > 相似度（手误/谐音：万川↔忘川）。
        匹配池 = 群名片 + 昵称；解析不出来就返回空串，让 AI 带着名册去兜底
        （它看得见上下文，比我瞎猜一个同音名点错人强）。
        """
        members = await self._get_members(umo)
        if not members or not raw_name:
            return ""
        self_id = str(self.state.get(f"self_id:{umo}") or "")
        cands: list[tuple[str, str]] = []
        for m in members:
            uid = str(m.get("user_id") or "")
            if not uid or uid == self_id:
                continue
            for nm in (m.get("card") or "", m.get("nickname") or ""):
                nm = str(nm).strip()
                if nm:
                    cands.append((uid, nm))
        if not cands:
            return ""
        # 逐层剥称呼后缀：「万川姐姐」->「万川」；两层都试
        tried = [raw_name.strip()]
        cur = _HONORIFIC_RE.sub("", tried[-1]).strip()
        while cur and cur != tried[-1]:
            tried.append(cur)
            cur = _HONORIFIC_RE.sub("", cur).strip()
        # 1) 精确
        for t in tried:
            hits = [u for u, nm in cands if nm == t]
            if hits:
                return hits[0]
        # 2) 互相包含（目标名出现在昵称里，或昵称是目标名的一部分）
        for t in tried:
            if len(t) < 2:
                continue
            hits = [u for u, nm in cands if len(nm) >= 2 and (t in nm or nm in t)]
            if hits:
                return hits[0]
        # 3) 相似度：短名（≤2字）要 0.75 起（嘻嘻↔姬姬 0.5 这种不能瞎点），
        #    长名 0.6 —— 手误打错一个字也得是「很像」才认
        import difflib

        for t in tried:
            if len(t) < 2:
                continue
            best, best_r = "", 0.0
            for u, nm in cands:
                r = difflib.SequenceMatcher(None, t, nm).ratio()
                if r > best_r:
                    best, best_r = u, r
            if best and best_r >= (0.75 if len(t) <= 2 else 0.6):
                return best
        return ""

    async def _handle_targeted_like(
        self, event: AstrMessageEvent, raw: str
    ) -> bool | None:
        """「给 XX 点赞」：目标可以是 @段、裸 QQ 号或群昵称。

        返回 True=已处理（调用方 stop_event）；None=不是带目标的请求，交回
        旧流程；False=像是带目标但解析不出人 —— 别瞎点，交给 AI 带名册兜底。
        """
        if not _LIKE_VERB_RE.search(raw):
            return None
        umo = event.unified_msg_origin
        bot = getattr(event, "bot", None)
        sender = str(event.get_sender_id() or "")
        sid = str(event.get_self_id() or "")
        if bot is None or not sender.isdigit() or sender == sid:
            return None
        # 目标候选 ①：@段（排除 @机器人自己 和 @发消息的人）
        target_uid = ""
        at_uids = [u for u in self._at_uids(event) if u not in (sid, sender)]
        if at_uids:
            target_uid = at_uids[0]
        else:
            # 目标候选 ②：裸 QQ 号 —— 得从原文抓，_norm_like_text 会把
            # 5 位以上数字整段吃掉，「麻烦给1000000000点赞」归一化后就剩「给点赞」了
            m_num = re.search(
                r"(?:给|帮|替|为)\s*(\d{5,})\s*(?:点个|点下|点一下|点|来个|来一个|一个|个)?\s*赞",
                raw,
            )
            if m_num:
                target_uid = m_num.group(1)
            else:
                # 目标候选 ③：昵称/群名片 —— 从归一化后的句子里抠出「给」和「赞」中间的人
                m = _LIKE_TARGET_RE.fullmatch(_norm_like_text(raw))
                if not m:
                    return None
                who = (m.group("who") or "").strip()
                if who in _LIKE_PRONOUNS:
                    # 「给他点赞」：他 = 刚刚说完话的那个人（先往前翻历史找最近的发言人）
                    target_uid = self._recent_speaker(umo, {sid, sender})
                    if not target_uid:
                        logger.info(
                            f"[human_sim] 点赞目标「{who}」找不到最近的发言人，交 AI 兜底"
                        )
                        return False
                elif who in _LIKE_SELF_WORDS:
                    return None  # 给自己点 → 旧流程更熟
                elif who in _LIKE_BOT_WORDS or not who:
                    logger.info(
                        f"[human_sim] 点赞目标指到机器人头上（{who!r}），交 AI 接话"
                    )
                    return False
                elif not re.fullmatch(r"(\d{5,})", who):
                    target_uid = await self._resolve_member_uid(umo, who)
                    if not target_uid:
                        # 名字对不上就退一步找最近发言人：宁可点错人，也别嘴上答应不动手
                        target_uid = self._recent_speaker(umo, {sid, sender})
                        if not target_uid:
                            logger.info(
                                f"[human_sim] 点赞目标「{who}」解析不出，也没有最近发言人，交 AI"
                            )
                            return False
        if not target_uid.isdigit() or target_uid == sid:
            return False
        self._bots[umo] = bot
        if sid.isdigit():
            self.state[f"self_id:{umo}"] = sid
        now = time.time()
        ckey = f"like_cool:{sender}"
        if now - float(self.state.get(ckey, 0) or 0) < 30:
            logger.info(f"[human_sim] 带目标点赞 30 秒内重复，跳过（{sender}）")
            return True
        tnick = await self._member_name(umo, target_uid)
        snick = await self._member_name(umo, sender)
        done_key = f"like_done:{datetime.now():%Y-%m-%d}:{target_uid}"
        if self.state.get(done_key):
            logger.info(
                f"[human_sim] 带目标点赞：今天已给 {tnick}({target_uid}) 点过，直接应答"
            )
            await self._ack_like(
                umo, target_uid, tnick, ok=True, again=True, by=sender, by_nick=snick
            )
            return True
        ok, last_err = await self._do_send_like(bot, target_uid, sid)
        if not ok:
            logger.warning(
                f"[human_sim] 带目标点赞失败（{tnick}/{target_uid}）: {last_err}"
            )
        if ok:
            self.state[done_key] = True
            # 记住这次点的谁：「再次点赞 / 再点一次」要能接上
            self.state[f"last_like_target:{umo}"] = {
                "uid": target_uid, "nick": tnick, "ts": now,
            }
        self.state[ckey] = now
        logger.info(
            f"[human_sim] 带目标点赞「{raw.strip()[:24]}」本地处理 -> {tnick}"
            f"({target_uid})（{'成功' if ok else '失败'}，{umo.rsplit(':', 1)[-1]}）"
        )
        await asyncio.sleep(random.uniform(0.2, 0.7))
        await self._ack_like(
            umo, target_uid, tnick, ok=ok, by=sender, by_nick=snick
        )
        return True

    async def _repeat_like_target(self, event: AstrMessageEvent, raw: str) -> str:
        """「再次点赞 / 再点一次」指向谁：先看引用里提到的目标，再沿用上一次真点过的。

        只在上层确认「句子没有显式宾语」后才调用，所以「再给他点个赞」不会走到这里。
        """
        umo = event.unified_msg_origin
        sender = str(event.get_sender_id() or "")
        sid = str(event.get_self_id() or "")
        # ① 引用旧消息里写过的「给 XX 点个赞」（比如引用自己那句「这就给元元点个大赞」）
        for mt in re.finditer(
            r"给\s*([^\s，,。!！?？@]{1,16}?)\s*(?:点个|点一下|点|个)?\s*(?:大)?(?:赞|点赞)",
            raw,
        ):
            who = mt.group(1).strip()
            m_num = re.search(r"\d{5,}", who)
            if m_num:
                return m_num.group(0)
            if who in _LIKE_SELF_WORDS:
                return sender
            uid = await self._resolve_member_uid(umo, who)
            if uid:
                return uid
        # ② 上一次真正点过的目标
        last = self.state.get(f"last_like_target:{umo}") or {}
        tgt = str((last or {}).get("uid") or "").strip()
        if tgt.isdigit() and tgt != sid:
            return tgt
        return ""

    async def _do_send_like(self, bot, target_uid: str, sid: str) -> tuple[bool, str]:
        """按 10→3→1 降级调 send_like，并检查 retcode。

        QQ 每人每天 10 个赞，额度用完后 NapCat 多半是 retcode≠0 正常返回而不是抛异常；
        以前只看异常，额度耗尽也被当成点成功，用户翻资料卡一看没涨 ——「主动点赞不成功」。
        """
        last_err = ""
        for times in (10, 3, 1):
            kwargs: dict = {"user_id": int(target_uid), "times": times}
            if sid.isdigit():
                kwargs["self_id"] = int(sid)
            try:
                ret = await bot.api.call_action("send_like", **kwargs)
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                continue
            if isinstance(ret, dict):
                code = str(ret.get("retcode", 0))
                if code not in ("0", "0.0", "None"):
                    last_err = (
                        f"retcode={code} "
                        f"{ret.get('msg') or ret.get('wording') or ret.get('message') or ''}"
                    ).strip()
                    continue
            return True, ""
        return False, last_err

    async def _ack_like(
        self, umo: str, uid: str, nick: str, ok: bool, again: bool = False,
        by: str = "", by_nick: str = "",
    ) -> None:
        """点赞之后的回话：@ 上要赞的人，内容默认用固定台词（不让小模型自主发言）。

        动作本地做、话也本地出：固定台词连着用几次像播报机的话，多备几条轮着用即可，
        但说话权不从模型那边来——模型会编「今天赞不了」，也会拖两秒。
        失败（当天 10 赞额度用光/风控）如实说，不谎称点了：用户翻资料卡一看没有更尴尬。
        带目标点赞（by 非空）时：@ 的是「托我点赞的人」，事情要汇报给托付的人。
        """
        if ok:
            line = await self._like_line(umo, nick, again, by_nick=by_nick)
        else:
            line = "今天赞满啦，明天给你补"
        at_uid = by or uid
        chain: list = []
        if (
            ok
            and self.cfg.get("like_at_target", True)
            and at_uid.isdigit()
            and ":GroupMessage:" in umo  # 私聊没有 @ 这回事，硬塞会显示成一段名字
        ):
            chain.append(At(qq=at_uid))
            chain.append(Plain(" " + line))
        else:
            chain.append(Plain(line))
        try:
            await self._send(umo, chain)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 点赞应答发送失败: {type(e).__name__}: {e}")
            return
        # 记进群聊历史：AI 之后决策时得知道「自己刚给谁点过赞」，
        # 别人接着说「谢谢鱼哥」才接得住，而不是一脸茫然
        self._history.setdefault(umo, deque(maxlen=30)).append(f"大肥鱼：{line}")

    async def _like_line(
        self, umo: str, nick: str, again: bool = False, by_nick: str = ""
    ) -> str:
        """点赞后的交代：默认只回固定台词，不让小模型现编。

        like_reply_ai 打开时才走模型生成；失败/超时/空返回一律回退本地台词。
        by_nick 非空 = 带目标点赞：是「by_nick 托我给 nick 点的」，回话冲着托付的人说。
        """
        if not self.cfg.get("like_reply_ai", False):
            return random.choice(LIKE_ACK_LINES)
        cap = int(self.cfg.get("like_reply_max_chars", 16))
        who = nick or "对方"
        if by_nick:
            if again:
                prompt = (
                    f"「{by_nick}」又让你给群友「{who}」点赞，可你今天已经给 {who} 点过了。"
                    f"冲着「{by_nick}」说一句话打发掉：不超过 {cap} 字，不耐烦但不难听，"
                    "别用敬语、别解释、别问句，不要 @ 任何人，不要引号，句子里别写名字。"
                )
            else:
                prompt = (
                    f"「{by_nick}」让你给群友「{who}」点赞，你点了（一次点满 10 个）。"
                    f"冲着「{by_nick}」交代一声：不超过 {cap} 字，带点你自己的口气，"
                    f"可以在句子里提「{who}」，别用敬语、别解释、别问句，"
                    "不要 @ 任何人，不要引号。"
                )
        elif again:
            prompt = (
                f"「{who}」又来要赞，你今天已经给他点过了。"
                f"说一句话打发掉：不超过 {cap} 字，不耐烦但不难听，"
                "别用敬语、别解释、别问句，不要 @ 任何人，不要引号。"
                "开头已经 @ 过他了，句子里不要再叫他的名字。"
            )
        else:
            prompt = (
                f"你刚给群友「{who}」的 QQ 名片点了赞（一次点满 10 个）。"
                f"现在交代这件事：不超过 {cap} 字，带点你自己的口气，"
                "别用敬语、别解释、别问句，不要 @ 任何人，不要引号。"
                "开头已经 @ 过他了，句子里不要再叫他的名字。"
            )
        try:
            text = await asyncio.wait_for(
                self._generate(prompt, umo, cap), timeout=8
            )
        except Exception as e:  # noqa: BLE001
            logger.info(f"[human_sim] 点赞回话生成失败（{type(e).__name__}），用本地台词")
            text = ""
        return (text or "").strip() or random.choice(LIKE_ACK_LINES)
