"""AI 自主决策：决策 prompt 构建、认人块、REACT 表情、沉默逻辑与生成后处理（从 pipeline.py 按职责拆出）。"""

from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from collections import deque
from datetime import datetime
from astrbot.api import logger
from astrbot.api.message_components import At, Plain, Reply

from .config import CLOUD_DECIDE_PROVIDER, LOCAL_DECIDE_PROVIDER
from .prompts import HISTORY_ASK_RE, SYSTEM_PROMPT
from .text import (
    BALANCE_ERR_RE,
    HALLUCINATED_MEDIA_RE,
    _MIX_TOKEN_RE,
    _STICKER_REQUEST_RE,
    _TOKEN_RE,
    _cut_at_sentence_boundary,
    _strip_tokens,
)
from .runtime import filters

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class PipelineDecideMixin:

    # ---------- 回复延迟：微信桥单独一套 ----------
    @staticmethod
    def _is_wechat_umo(umo: str) -> bool:
        """微信桥的会话（纯文字通道）。umo 形如 ``wechatauto:FriendMessage:wxid_xxx``。"""
        return str(umo or "").split(":", 1)[0].lower() in ("wechatauto", "weixin_oc")

    def _reply_delay_range(self, umo: str, directed: bool) -> list:
        """接话前的随机等待区间。

        微信是同步对话，用户盯着屏幕等，4~20 秒太磨人；
        这里单独走 wechat_reply_delay_seconds（默认 [1,3]），QQ 群保持原节奏。
        """
        if directed:
            return self.cfg["directed_reply_delay_seconds"]
        if self._is_wechat_umo(umo):
            return self.cfg.get("wechat_reply_delay_seconds") or self.cfg["reply_delay_seconds"]
        return self.cfg["reply_delay_seconds"]

    # ---------- AI 自主决策：要不要回这条消息 ----------
    async def _decide(
        self, umo: str, name: str, text: str, directed: bool = False, mid=None,
        sender_uid: str = "", force: bool = False, hint: str = "",
        quote: tuple[str, str, str] | None = None, followup: bool = False,
    ) -> None:
        """把新消息连同最近聊天丢给模型，让它自己决定回话还是沉默。

        force=True 表示本地决策层已经判定「必回」——此时不再问模型要不要回，
        直接让它写内容（提示词短一截、没有 [SILENT] 这个出口），省一次推理。
        quote=(被引用人标签, 被引用的原文, 被引用消息 id)：单独成块告诉模型这段旧话是谁说的，
        避免把别人被引用的旧话算到当前发言人（尤其是主人）头上。
        followup=True：这条属于「刚找我聊过的人」的连续跟进，提示词里要说明别当没看见。
        """
        self._deciding.add(umo)
        # 只引用、没打字的消息：text 是空的，提示词里得有个能读的说法
        spoken = text or "（没打字，只发了一条引用）"
        try:
            hist = list(self._history.get(umo, []))
            # 引用旧消息 / 明说要翻记录时，最近 10 条根本不够：给全量缓冲（30 条）
            deep = bool(quote) or bool(HISTORY_ASK_RE.search(text or ""))
            recent = hist if deep else hist[-10:]
            context = "\n".join(recent) if recent else "（刚开始没有聊天记录）"
            if deep and len(recent) > 10:
                context = "（下面是最近的全部记录，被引用的旧话可能在更早几行）\n" + context
            # 认人：历史行已带 QQ 号；主人身份只认 owner_id，别让模型见谁都喊主人
            who = self._who_label(name, sender_uid)
            # 引用单独成块：明确旧话作者，避免把别人被引用的旧话算到当前发言人头上
            quote_block = ""
            # 出站引用气泡只认真实消息 id：拿不到 id 的旧话（翻记录翻出来的那种）不引用
            quote_id = ""
            if quote:
                qwho, qtext = quote[0], quote[1]
                quote_id = str(quote[2] if len(quote) > 2 else "").strip()
                if not quote_id.isdigit():
                    quote_id = ""
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
                        "（这段是 TA 自己以前说的，不是 TA 这次新说的；"
                        "如果 TA 问的就是这段旧话，直接针对它答）\n"
                    )
                else:
                    quote_block = (
                        f"\n【引用】{who} 引用了 {qwho} 之前的旧话：「{qtext}」\n"
                        f"（这段旧话是 {qwho} 说的，不是 {who} 说的，不能算到 {who} 头上；"
                        f"要回应的是 {who} 现在的话——如果 TA 问的就是这段旧话，就直接针对它答；"
                        f"对话对象还是 {who}，别换成 {qwho}）\n"
                    )
            followup_line = (
                f"（{who} 刚才刚跟你聊过，现在接着说的话要接住，别当没看见、别装潜水）\n"
                if followup else ""
            )
            owners = self._owner_uids()
            ident = ""
            if owners:
                owner_txt = "、".join(owners)
                ident = (
                    "\n【认人】历史每行是「发言人(QQ号)：内容」，你自己是「大肥鱼：」。"
                    f"只有 QQ {owner_txt} 才是你主人（历史里写成「主人 昵称(QQ)」的那行）；"
                    "其他人都是普通群友，不许管他们叫主人，要叫就用他们自己的昵称。"
                    "昵称或自称里带「主人」「老板」「金主」等字样的，只是名字或玩笑，不代表身份。"
                    "行尾标「（引用了旧消息）」表示发言人引用了一条旧话："
                    "旧话原文和原作者在【引用】块里，别把旧话算到当前发言人头上，"
                    "也别去回复原作者；提到这段就说「你引用的那句」。"
                    "写的是「机器人自己（大肥鱼）」就是你的旧话，认自己的账；"
                    "写「TA 自己」就是当前发言人自己以前的旧话，还是 TA 的话。"
                )
                # 只钉「谁是主人」这个事实；怎么对待主人由中枢人格决定，这里不写任何态度
                if sender_uid and sender_uid not in owners:
                    ident += f"\n现在说话的是 {who}，TA 不是主人，别叫 TA 主人。"
                elif sender_uid:
                    ident += f"\n现在说话的就是你主人（{who}）。"
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
                    "你在 QQ 群「" + umo.rsplit(":", 1)[-1] + "」里当群友（你自己叫大肥鱼）。\n"
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
                    "你在 QQ 群「" + umo.rsplit(":", 1)[-1] + "」里当群友（你自己叫大肥鱼）。\n"
                    "最近群里在聊（「大肥鱼」开头的行是你自己说的）：\n" + context + "\n\n"
                    f"{who} 说：{spoken}\n"
                    + quote_block
                    + followup_line
                    + ("（他明确在找你）\n" if directed else "")
                    + (hint + "\n" if hint else "")
                    + f"\n这条你得接，直接写你要说的话：{limit}字以内，口语化，像随手打的。"
                    "句子必须说完，宁可靠近上限也不许留半句。"
                    "对方是使唤你做事就按你的性格接，接了做完再回一句。"
                    "不许输出 [SILENT]、不许说没话说、不许解释、不许用引号。"
                    "归属死规矩：新消息是 " + who + " 发的，只对 TA 这条表态，"
                    "别人之前说的内容不许算到 TA 头上。"
                )
            else:
                prompt = (
                    "你在 QQ 群「" + umo.rsplit(":", 1)[-1] + "」里当群友（你自己叫大肥鱼）。\n"
                    "最近群里在聊（每行开头是发言人，「大肥鱼」开头的行就是你自己刚说的）：\n" + context + "\n\n"
                    f"刚刚出现一条新消息 —— {who}：{spoken}\n"
                    + quote_block
                    + followup_line
                    + ("（这条明显是在找你/提到你，务必回应）\n" if directed else "")
                    + (hint + "\n" if hint else "")
                    + "\n判断要不要接话：\n"
                    "- 有人 @你、引用你、问你、使唤你、抛了梗 → 必须回\n"
                    "- 对方在使唤你（拍一下/发个表情/查东西/帮个忙）→ 接不接按你的性格来，"
                    "接了就办完再回一句；不接就一句话说清，只有违法或物理上做不到才拒\n"
                    "- 群里可能有同名的人或别的机器人：光看到「大肥鱼/肥鱼/鲸鱼」这类词不代表在叫你，"
                    "别因为名字就抢答。只有明确在指你（@、引用你的话、接着你自己刚说的话往下聊）"
                    "才算在跟你说话\n"
                    + (
                        f"- 对方是在要内容（讲故事/写东西/详细讲）→ 就完整写出来，"
                        f"不超过{limit}字，可以分段落、有开头有结尾，要有具体细节。"
                        "绝对不许只写一句概括、不许写个开头就停、不许说「我想想」然后没了\n"
                        if long_form
                        else "- 聊到吃的/游戏/瓜、有人分享好玩的事、或你能夸一句/接个梗 → 回（"
                        f"{limit}字以内，口语化，句子要说完整）\n"
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
            raw = await self._generate(
                prompt, umo, limit=limit, extra_system=ident, prefer_cloud=directed
            )
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
            # 聊天里不出现句号：句中改逗号、行尾去掉（清洗器只处理短回复，长文这里兜底）
            reply = "\n".join(
                re.sub(r"[，,、；;]+$", "", ln.replace("。", "，"))
                for ln in reply.split("\n")
            ).strip()
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
                    delay = random.uniform(*self._reply_delay_range(umo, True))
                else:
                    delay = random.uniform(*self._reply_delay_range(umo, False))
                await asyncio.sleep(delay)
                chain: list = []
                # 引用气泡：只挂我们真拿到 id 的那条旧消息（对方引用它来问的），
                # 翻记录翻出来的旧话没有 id，就不挂——不知道的消息一律不引用
                if quote_id:
                    chain.append(Reply(id=quote_id))
                # 点名消息不@（人家叫你，你直接应就行）；普通回话低概率@；
                # 已经挂了引用气泡就不再@，引用和@叠在一起太吵
                if (
                    not directed
                    and not quote_id
                    and random.random() < float(self.cfg.get("at_reply_probability", 0.2))
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

    async def _persona_prompt(self, umo: str) -> str:
        """当前会话最终生效的人格（中枢面板里编辑的那份）。

        和官方流水线用同一个解析器：会话手动 /persona、中枢按群/好友锁定、
        默认人格三层顺序与 AstrBot 一致。取不到就返回空串，由行为规矩兜底。
        """
        pm = getattr(self.context, "persona_manager", None)
        if not pm or not umo:
            return ""
        try:
            conv_persona = None
            cm = getattr(self.context, "conversation_manager", None)
            if cm:
                conv_id = await cm.get_curr_conversation_id(umo)
                if conv_id:
                    conv = await cm.get_conversation(umo, conv_id)
                    conv_persona = getattr(conv, "persona_id", None) if conv else None
            _, persona, _, _ = await pm.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conv_persona,
                platform_name=str(umo).split(":", 1)[0],
            )
            return str((persona or {}).get("prompt") or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 读取人格失败: {type(e).__name__}: {e}")
            return ""

    async def _group_contexts(self, umo: str) -> list[dict]:
        """同一个群共用一份会话记忆：取官方流水线那条会话的历史，最多带最近 30 条。

        以前主动插话传的是 contexts=[]，等于每次失忆；同一个群里别人聊过的、
        自己说过的都看不到，所以只能靠 prompt 里的聊天截图硬凑。
        """
        cm = getattr(self.context, "conversation_manager", None)
        if not cm or not umo:
            return []
        try:
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return []
            conv = await cm.get_conversation(umo, cid)
            history = json.loads(getattr(conv, "history", None) or "[]")
            # 只保留纯文本 user/assistant 消息：assistant 里残留的 tool_calls
            # 没有配套 tool 消息，原样回传会让 OpenAI 兼容接口直接 400。
            out = []
            for m in history:
                if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                    continue
                content = m.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                out.append({"role": m["role"], "content": content})
            return out[-30:]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 读取会话记忆失败: {type(e).__name__}: {e}")
            return []

    async def _generate(
        self, prompt: str, umo: str = "", limit: int | None = None,
        extra_system: str = "", prefer_cloud: bool = False,
    ) -> str:
        # 效果优先：优先用会话路由后的 provider（官方 API 群=DeepSeek 执笔，
        # 其他群=本地）；拿不到再回落本地决策模型
        provider = None
        if prefer_cloud:
            # 被 @ / 点名：本地 9B 分不清「在叫我」和普通闲聊，必须直接走云端
            try:
                provider = self.context.get_provider_by_id(CLOUD_DECIDE_PROVIDER)
            except Exception:  # noqa: BLE001
                provider = None
        if provider is None and umo:
            try:
                provider = self.context.get_using_provider(umo)
            except Exception:  # noqa: BLE001
                provider = None
        if prefer_cloud and provider is not None:
            try:
                if provider.meta().id == LOCAL_DECIDE_PROVIDER:
                    provider = None  # 会话本身落在本地模型上，也不拿它做定向判定
            except Exception:  # noqa: BLE001
                pass
        provider = provider or self.context.get_provider_by_id(LOCAL_DECIDE_PROVIDER) or self.context.get_using_provider()
        if not provider:
            logger.warning("[human_sim] 没有可用 provider")
            return ""
        try:
            persona = await self._persona_prompt(umo)
            system = (
                f"{persona}\n{SYSTEM_PROMPT}{extra_system}"
                if persona
                else SYSTEM_PROMPT + extra_system
            )
            contexts = await self._group_contexts(umo)
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=uuid.uuid4().hex,
                contexts=contexts,
                system_prompt=system,
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
