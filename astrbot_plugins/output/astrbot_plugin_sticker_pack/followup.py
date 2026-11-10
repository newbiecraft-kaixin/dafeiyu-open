"""发完文字后的补表情流程（概率/额度/冷却/互斥 + AI 选图）。"""

from __future__ import annotations

import asyncio
import random
import time

from astrbot.api import logger


class FollowupMixin:
    # ---------- 发完话后：系统分析文本 → 推荐候选 → AI 挑 → 单独发一条表情 ----------
    # 防递归四层闸：
    #   1. 补表情走 action 直发，不进 pipeline，不会再触发本插件的分析钩子
    #   2. AI 的选择调用是裸 text_chat（persist=False），产出只用来匹配候选名，绝不发出去
    #   3. 同群互斥（_follow_running）+ 同群冷却（_last_follow）
    #   4. 每分钟滑动窗口上限（_follow_rate）
    def _schedule_followup(self, event, text: str) -> None:
        if not self.stickers:
            return
        # 命令输出（/xxx）不掺和
        if (event.message_str or "").strip().startswith(("/", "#", "！", "!")):
            return
        if random.random() > float(self.config.get("follow_up_probability", 0.65)):
            self._fstat("block_prob")
            return
        umo = event.unified_msg_origin
        unl = umo in set(self.config.get("unlimited_groups", []) or [])
        now = time.time()
        cap = int(self.config.get("follow_up_per_minute", 3))
        if cap > 0 and not unl:
            cutoff = now - 60.0
            while self._follow_rate and self._follow_rate[0] < cutoff:
                self._follow_rate.pop(0)
            if len(self._follow_rate) >= cap:
                self._fstat("block_cap")
                return
        if umo in self._follow_running:
            self._fstat("block_running")
            return
        # 冷却对无限制群也生效（×2）：豁免的是分钟硬上限，不是「连轴转」
        cool = float(self.config.get("follow_up_cooldown_seconds", 60))
        if unl:
            cool *= 2
        if now - float(self._last_follow.get(umo, 0)) < cool:
            self._fstat("block_cool")
            return
        # 同群刚刚已经出过一张（单发/配脸），别再补一张，否则成了连发
        if not self._emit_allowed(umo):
            self._fstat("block_gap")
            return
        self._fstat("pass")
        self._remember_bot(event, umo)
        delay = random.uniform(*self.config.get("follow_up_delay_seconds", [2, 5]))
        asyncio.create_task(self._followup(umo, text, delay))

    async def _followup(self, umo: str, text: str, delay: float) -> None:
        self._follow_running.add(umo)
        try:
            await asyncio.sleep(delay)
            cands = self._match_candidates(
                text,
                int(self.config.get("follow_up_candidates", 3)),
                umo,
                # 0.5 = 至少一半二元组对上；单字扫尾（0.26~0.4）不进候选，
                # 否则「晚安」会被「害羞啦」靠一个「晚」字挤进来
                min_score=float(self.config.get("follow_up_min_score", 0.5)),
            )
            if not cands:
                # 兜底池都凑不出来 = 库里连基础情绪图都没有，记一笔需求
                self._fstat("pool_empty")
                logger.warning(f"[sticker_pack] 候选池为空（含兜底），本次不发：{text[:24]}")
                self._note_deficit(text, umo)
                return
            # 候选已经足够明确时直接发，不再问模型：
            #   只有「唯一候选」或「第一名分数明显领先第二名」才算明确，
            #   其余歧义场景才让 LLM 做最后一次语义判断。
            #   补表情的文本匹配本来就到了整词命中（0.85）这一档，
            #   这种分数下再花一次调用去确认，纯属白花钱。
            sc = dict(self._ranker().rank(text))
            floor = float(self.config.get("follow_up_min_score", 0.5))
            s1 = sc.get(cands[0], 0.0)
            s2 = sc.get(cands[1], 0.0) if len(cands) > 1 else 0.0
            direct = s1 >= floor and (
                len(cands) == 1
                or (
                    s1 >= float(self.config.get("follow_up_direct_score", 0.85))
                    and (
                        s2 <= 0
                        or s1 >= s2 * float(self.config.get("follow_up_direct_margin", 1.2))
                    )
                )
            )
            name = ""
            if direct:
                name = cands[0]
                self._fstat("direct")
            else:
                # 候选列表每条必须带描述：AI 只看得懂「这张图表达什么」，
                # 光给名字它没法判断贴不贴切（名字常常是「老吴」「最残忍生物」这种）。
                lines = []
                for i, n in enumerate(cands):
                    desc = (self.stickers.get(n, {}).get("meaning") or "").strip() or "无描述"
                    lines.append(f"{i + 1}. {n} ｜ {desc}")
                prompt = (
                    "你刚在群里发了这句话：" + text + "\n"
                    "现在可以像真人一样补一个表情（单独一条消息）。候选（共 "
                    + str(len(cands))
                    + " 张，格式＝名字｜这张图表达的情绪/场景）：\n"
                    + "\n".join(lines)
                    + "\n\n从候选里挑一个最贴切的，只回复它的名字（不要序号、不要解释）；"
                    "只有全都明显不贴切时才回复：不发"
                )
                choice = (await self._ask_llm(prompt, umo=umo) or "").strip()
                if "不发" in choice:
                    # 模型明确弃权才不发（歧义场景下「不发」是有效的负样本过滤）
                    self._fstat("no_pick")
                    return
                name = next((n for n in cands if n in choice), "") if choice else ""
                if not name:
                    # 模型调用失败/超时/返回空/答了候选之外的名字：
                    # 不再整次空手而归，用算法在候选里兜底挑一张
                    # （越靠前越贴切、避开刚发过的）。
                    if not bool(self.config.get("follow_up_llm_fallback", True)):
                        self._fstat("no_pick")
                        return
                    name = self._pick_fresh(cands, umo) or cands[0]
                    self._fstat("llm_fallback")
                    logger.info(
                        f"[sticker_pack] 选表情模型不可用，算法兜底「{name}」"
                        f"（原文：{text[:20]}）"
                    )
            # 延迟 + 等模型这几秒里，单发/配脸可能已经出过一张了，这里再拦一次
            if not self._emit_allowed(umo):
                self._fstat("block_gap")
                return
            now = time.time()
            cap = int(self.config.get("follow_up_per_minute", 3))
            if cap > 0 and umo not in set(self.config.get("unlimited_groups", []) or []):
                cutoff = now - 60.0
                while self._follow_rate and self._follow_rate[0] < cutoff:
                    self._follow_rate.pop(0)
                if len(self._follow_rate) >= cap:
                    return
                self._follow_rate.append(now)
            self._last_follow[umo] = now
            item = self.stickers[name]
            ok = await self._push(umo, self._to_payload(item))
            if not ok:
                self._fstat("send_fail")
                # 没发出去就别记「已用」也别占冷却，下一句还能再试
                self._last_follow.pop(umo, None)
                return
            self._mark_used(umo, name)
            self._mark_emit(umo)
            logger.info(
                f"[sticker_pack] 补表情「{name}」-> {umo.rsplit(':', 1)[-1]}"
                f"（原文：{text[:20]}）"
            )
            self._fstat("sent")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_pack] 补表情流程异常: {type(e).__name__}: {e}")
        finally:
            self._follow_running.discard(umo)
