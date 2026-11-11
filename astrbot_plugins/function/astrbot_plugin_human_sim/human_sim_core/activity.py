"""生命周期与主动行为：监控循环、插话、潜水巡查、主动私聊与问候。"""

from __future__ import annotations

import asyncio
import os
import random
import time
from datetime import datetime, timedelta, timezone
from astrbot.api import logger
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

from .config import REACTION_IDS

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class ActivityMixin:

    # ---------- 生命周期 ----------
    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())
        self._mon_task = asyncio.create_task(self._monitor_loop())
        self._face_task = asyncio.create_task(self._face_warmup())
        self._pm_task = asyncio.create_task(self._pm_loop())
        logger.info(
            f"[human_sim] 已启动：潜水池={len(self.cfg['watch_groups'])} 个群，"
            f"决策模式={'AI自主决定' if bool(self.cfg.get('ai_decide')) else '随机概率'}，"
            f"冷场≥{self.cfg['group_idle_minutes']}分钟，"
            f"表情包 {len(self._stickers)} 张，"
            f"消息回应 {len(self.cfg.get('reaction_ids') or REACTION_IDS)} 种表情"
            f"（AI自选{'开' if bool(self.cfg.get('react_ai_decide', True)) else '关'}"
            f"/旧随机抽样{'开' if bool(self.cfg.get('react_random_decide', False)) else '关'}"
            f" {self.cfg.get('react_after_reply_probability', 0.25)}·{self.cfg.get('react_only_probability', 0.06)}），"
            f"活动时段 {self.cfg['start_hour']}:00-{self.cfg['end_hour']}:00"
            f"（夜间频率 x{self._night_scale()}，点名照常），"
            f"自主发言基准 {self.cfg.get('ai_decide_probability', 0.5)}"
            + (
                f" × 人群系数（{self.cfg.get('audience_min_size', 100)}→"
                f"{self.cfg.get('audience_max_size', 2000)}人最低 x{self.cfg.get('audience_min_factor', 0.15)}，"
                f"近{int(self.cfg.get('audience_activity_window', 300)) // 60}分钟活跃度反向补偿）"
                if bool(self.cfg.get("audience_scaling", True))
                else "（人群缩放已关）"
            )
        )
        # 占位群号通常是模板/默认值被直接保存进配置，表现为「群在潜水池里但机器人从不插话、
        # 在真实群里被戳也没反应」。启动时显式喊一声，省得再靠日志猜。
        if any(":GroupMessage:100000000" in str(g) for g in self.cfg.get("watch_groups") or []):
            logger.warning(
                "[human_sim] 潜水池里是占位群号（1000000001-3），真实群不会插话、也不回戳；"
                "请把 watch_groups 换成实际群 UMO"
            )

    async def _face_warmup(self) -> None:
        """启动后先等一批消息把 bot 客户端填好，再补一次群历史表情。"""
        try:
            await asyncio.sleep(120)
            await self._sync_faces_from_history()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 启动补学表情失败: {type(e).__name__}: {e}")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
        if self._mon_task:
            self._mon_task.cancel()
        if self._face_task:
            self._face_task.cancel()
        if self._pm_task:
            self._pm_task.cancel()

    # ---------- 后台限额度量：每 5 分钟上报一次，判断限额是否合理 ----------
    async def _monitor_loop(self) -> None:
        interval = 300
        while True:
            await asyncio.sleep(interval)
            try:
                # 内存兜底：这些「按人/按消息」的时间戳容器只会在用的时候裁剪，
                # 没人再用的旧键会一直留着，长跑后 dict 只涨不缩。定期扫掉过期项。
                now_ts = time.time()
                for k in [k for k, v in self._rate.items()
                          if not v or now_ts - float(v[-1]) > 60]:
                    self._rate.pop(k, None)
                for k in [k for k, v in self._poke_last.items()
                          if now_ts - float(v or 0) > 1800]:
                    self._poke_last.pop(k, None)
                for k in [k for k, v in self._poke_seq.items()
                          if now_ts - float((v or {}).get("ts") or 0) > 1800]:
                    self._poke_seq.pop(k, None)
                for k in [k for k, v in self._react_join_at.items()
                          if now_ts - float(v or 0) > 1800]:
                    self._react_join_at.pop(k, None)
                for k in [k for k, v in self._last_echo.items()
                          if now_ts - float(v[1] if v else 0) > 1800]:
                    self._last_echo.pop(k, None)
                for k in [k for k, v in self._probe_cache.items()
                          if now_ts - float(v[0] if v else 0) > 600]:
                    self._probe_cache.pop(k, None)
                for k in [k for k, v in self._gsize_cache.items()
                          if now_ts - float(v[0] if v else 0) > 3600]:
                    self._gsize_cache.pop(k, None)
                stats = self._rate_stats
                self._rate_stats = {"pass": {}, "block": {}, "cool": {}}
                lines = []
                for u in self.cfg["watch_groups"]:
                    grp = u.rsplit(":", 1)[-1]
                    p = stats["pass"].get(f"ai_decide:{u}", 0)
                    b = stats["block"].get(f"ai_decide:{u}", 0)
                    c = stats["cool"].get(f"ai_decide_cool:{u}", 0)
                    pi = stats["pass"].get(f"idle_topic:{u}", 0)
                    if p or b or c or pi:
                        tag = "（无限制）" if self._unlimited(u) else ""
                        lines.append(
                            f"{grp}{tag}：回话 通过{p}/撞顶{b}/冷却拦{c}，话头 {pi}"
                        )
                gp = stats["pass"].get("global", 0)
                gb = stats["block"].get("global", 0)
                gc = stats["cool"].get("global_gap", 0)
                if gp or gb or gc:
                    lines.append(f"全局：通过{gp}/撞顶{gb}/间隔拦{gc}")
                if lines:
                    logger.debug("[human_sim] 近5分钟限额度量 | " + " | ".join(lines))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[human_sim] 监测上报失败: {type(e).__name__}: {e}")

    # ---------- 插话 ----------
    async def _interject_later(self, umo: str, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self._interject(umo)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 插话失败: {type(e).__name__}: {e}")

    async def _interject(self, umo: str, note: bool = True) -> None:
        if not self._global_gate(umo):
            return
        hist = list(self._history.get(umo, []))[-10:]
        if not hist:
            return
        context = "\n".join(hist)
        roster = await self._roster_brief(umo)
        roster_line = f"（群成员参考：{roster}）\n" if roster else ""
        prompt = (
            f"{roster_line}这是群里最近的聊天记录（「大肥鱼」开头的是你自己说的）：\n{context}\n"
            f"你只是潜水路过，随口插一句（不超过{int(self.cfg['max_chars'])}字）。"
            "像群友唠嗑时冒出来的那种：吐槽、接梗、起哄、就一两个词都行，"
            "不用回应某个人、更不要@人。不要总结，不要客套，不要像起来答话。"
        )
        text = await self._generate(prompt, umo)
        text = self._humanize(text)
        if not text:
            return
        chain: list = []
        # 对着最后说话的人讲（概率 @，像真人对话）
        if random.random() < float(self.cfg["at_last_speaker_probability"]):
            last_sender = self.state.get(f"last_sender:{umo}")
            self_id = self.state.get(f"self_id:{umo}")
            if last_sender and last_sender != self_id:
                chain.append(At(qq=last_sender))
                chain.append(Plain(" "))
        chain.append(Plain(text))
        self._maybe_append_emoji(chain, text=text, umo=umo if note else "")
        await self._send(umo, chain)
        if note:
            self._note_act(umo, text, spoken=True)
        logger.info(f"[human_sim] 插话（群 {umo.rsplit(':', 1)[-1]}）: {text[:40]}")

    # ---------- 定时巡查：冷场开话头 / 冒泡表情 ----------
    async def _loop(self) -> None:
        base = max(1, int(self.cfg["lurk_check_interval_minutes"]))
        while True:
            await asyncio.sleep(base * 60 * random.uniform(0.6, 1.4))
            try:
                await self._lurk_check()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[human_sim] 巡查失败: {type(e).__name__}: {e}")

    async def _lurk_check(self) -> None:
        if not self.cfg["enabled"]:
            return
        # 定时把群历史里的超级表情补进外部表（每 6 小时一次，静默执行）
        try:
            await self._sync_faces_from_history()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 群历史补学表情失败: {type(e).__name__}: {e}")
        # 夜间降频（不熄灯）：冷场开话头/冒泡表情的概率 × night_frequency_scale
        night = self._night_scale()
        if night <= 0:
            return
        # 定时问候带时段闸门，不走群冷场逻辑；主动私聊有独立计时器（_pm_loop）
        try:
            await self._greeting_check()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 主动问候/私聊失败: {type(e).__name__}: {e}")
        now = datetime.now().timestamp()
        idle_gap = int(self.cfg["group_idle_minutes"]) * 60
        lurk_base = max(1, int(self.cfg["lurk_check_interval_minutes"])) * 60
        for umo in self.cfg["watch_groups"]:
            # 每群独立巡查相位：以前所有群共用一轮 tick，几个群会在同一秒一起冒泡
            next_lurk = float(self.state.get(f"lurk_next:{umo}", 0) or 0)
            if not next_lurk:
                self.state[f"lurk_next:{umo}"] = now + random.uniform(0, lurk_base)
                continue
            if now < next_lurk:
                continue
            self.state[f"lurk_next:{umo}"] = now + lurk_base * random.uniform(0.5, 1.5)
            await asyncio.sleep(random.uniform(0, 60))
            # 巡查是跨点调度的：这一轮可能从 22:59 拖到 23:00 之后。
            # 睡醒后再看一眼时段，别让「时间节点到了」还发出去一条
            if self._night_scale() <= 0:
                continue
            # +1 接龙中：只复读，不冷场开话头、不冒泡发表情
            if self._plus1_state(umo, now):
                continue
            # 看群里上次有人说话的时间戳；门槛每次抖动 0.8~1.6 倍，
            # 固定间隔会像定时器一样准点开话头
            gap = idle_gap * random.uniform(0.8, 1.6)
            idle_for = self.state.get(f"last_msg:{umo}")
            if idle_for is None:
                # 没记录不等于「冷场很久」：以前这么当成冷场，门槛等于没设，
                # 只剩沉默补偿在推，于是每隔一个多小时准点冒一句
                self.state[f"last_msg:{umo}"] = now
                idle_for = now
            idle_ok = (now - float(idle_for)) >= gap
            # 自己刚开过话头就再等一个窗口：没人接话也别隔二十分钟又追一句
            last_topic = float(self.state.get(f"last_idle:{umo}", 0) or 0)
            if idle_ok and last_topic and now - last_topic < gap:
                idle_ok = False
            # 人群系数：人多的群冷场了也不至于太吵（人数压低），人少又冷场的群多冒几句（活跃度抬高）
            aud = await self._audience_scale(umo)
            if idle_ok:
                base = float(self.cfg["group_idle_probability"]) * aud
                # 沉默补偿封顶 0.75：补到 100% 的话，一回话时钟清零、爬到顶刚好
                # 又一个钟头，就变成「每隔 1.5 小时必冒一句」。
                p_topic = base
                if self._in_hours():
                    p_topic = min(self._ramp_probability(umo, base), 0.75)
                if random.random() < p_topic * night:
                    if self._unlimited(umo) or self._rate_ok(
                        "idle_topic", int(self.cfg["group_idle_per_minute"]), umo
                    ):
                        await self._topic(umo)
                        continue
            # 冷场也不随手拍人：没在跟对方聊天就不戳（这个入口已关闭）
            # 没开话头时，小概率主动冒个表情：表情包图优先，没图就发黄脸
            # 本地小模型不会主动调 send_meme 工具，这一层是它「自己发表情」
            # 的唯一通路，所以概率要给够，不然一天都见不到一张。
            p_sticker = float(self.cfg.get("solo_sticker_probability", 0.08)) * night * aud
            p_face = float(self.cfg["solo_face_probability"]) * night * aud
            if self._stickers and random.random() < p_sticker:
                path = random.choice(self._stickers)
                if await self._send(umo, [Image.fromFileSystem(path)]):
                    self._note_act(umo, f"冒了个表情包：{self._sticker_desc(path)}")
                logger.info(
                    f"[human_sim] 冒泡表情包 -> {umo.rsplit(':', 1)[-1]}：{os.path.basename(path)}"
                )
            elif random.random() < p_face:
                fid = random.choice(self._face_pool())
                if await self._send(umo, [Face(id=fid)]):
                    self._note_act(umo, f"冒了个表情：{self._face_desc(fid)}")
                logger.info(f"[human_sim] 冒泡表情 -> {umo.rsplit(':', 1)[-1]}")

    # ---------- 主动私聊：只找最近私聊过的人，绝不打扰陌生人 ----------
    def _pm_gap_interval(self, stamps: list[float]) -> int | None:
        """按这个人自己的「安静间隔」定主动间隔：取窗口内间隔的 p75。

        以前用「聊天速度 → 间隔」换算，量纲是错的：聊得越快，算出来的间隔越短，
        于是最该被放过的人反而被敲得最勤。这里直接量「他说完一句之后隔多久才会
        再开口」——安静间隔p75 越大，说明他本来就习惯隔很久才回来，越不该去打扰。
        样本不足（少于 3 段安静）返回 None，回落到速度档位。
        """
        try:
            pct = float(self.cfg.get("pm_active_gap_percentile", 0.75))
        except (TypeError, ValueError):
            pct = 0.75
        # 只统计 >=5 分钟的间隔：几秒到一两分钟的是同一段对话里的停顿，不是「人走了」
        gaps = sorted(b - a for a, b in zip(stamps, stamps[1:]) if (b - a) >= 300)
        if len(gaps) < 3:
            return None
        idx = min(len(gaps) - 1, int(len(gaps) * min(1.0, max(0.0, pct))))
        try:
            low = float(self.cfg.get("pm_active_gap_min_minutes", 15))
        except (TypeError, ValueError):
            low = 15.0
        return max(1, int(min(1440.0, max(low, gaps[idx] / 60.0))))

    def _pm_interval_minutes(self, count: int, span_seconds: float) -> int:
        """兜底：按「条数 / 期间时间」算聊天速度定档（样本不够时才用）；0 = 永久不主动。"""
        if count <= 0:
            return 0
        if count <= 1:
            return 1440  # 只寒暄过一句：第二天问候一次，仅此而已
        rate = count / (max(60.0, float(span_seconds)) / 60.0)  # 条/分钟
        tiers = self.cfg.get("pm_active_rate_tiers") or [
            [2.0, 1], [0.5, 3], [0.15, 10], [0.05, 30],
            [0.015, 120], [0.003, 360], [0.0005, 1440],
        ]
        for need, minutes in sorted(tiers, key=lambda it: -float(it[0])):
            if rate >= float(need):
                return max(1, int(minutes))
        return 0

    async def _pm_loop(self) -> None:
        """主动私聊单独计时：每秒级看不出来，按 1 分钟粒度轮询就够。"""
        interval = max(1, int(self.cfg.get("pm_active_check_minutes", 1)))
        while True:
            await asyncio.sleep(interval * 60)
            try:
                await self._proactive_pm()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[human_sim] 主动私聊失败: {type(e).__name__}: {e}")

    async def _proactive_pm(self) -> None:
        if not bool(self.cfg.get("pm_active_enable", True)):
            return
        # 宵禁：只在自己醒着的时段（start_hour~end_hour）主动找人，夜里一条都不发
        if not self._in_hours():
            return
        now = time.time()
        window = max(1, int(self.cfg.get("pm_active_count_window_days", 3))) * 86400
        hist_all = self.state.get("pm_hist") or {}
        # 主动开口之前先核好友关系：被删好友的人绝不能主动去私聊。
        # 这里用 assume_friend=False——连好友列表都查不到时宁可不发。
        bot = next(iter(self._bots.values()), None)
        if bot is None:
            return
        cands = []
        max_miss = max(1, int(self.cfg.get("pm_active_max_unanswered", 3)))
        max_silence = max(1.0, float(self.cfg.get("pm_active_max_silence_hours", 30))) * 3600
        for umo, rec in (self.state.get("pm_last") or {}).items():
            stamps = sorted(float(t) for t in (hist_all.get(umo) or []) if now - float(t) <= window)
            last_in = float(rec.get("ts") or 0)
            # 长期不回：他这么久没吭声，就别再主动了（等他再说话才恢复）
            if now - last_in > max_silence:
                continue
            # 连发上限：没被回应的情况下最多主动这么多次，之后永久闭嘴，直到他回一句
            miss = int(self.state.get(f"pm_miss:{umo}", 0) or 0)
            if miss >= max_miss:
                continue
            span = (stamps[-1] - stamps[0]) if len(stamps) > 1 else 0.0
            # 优先按他自己的安静间隔；样本不够才回落到速度档位
            interval = self._pm_gap_interval(stamps)
            if interval is None:
                interval = self._pm_interval_minutes(len(stamps), span)
            if interval <= 0:
                continue
            # 退避：3 次机会里也逐次拉长（1 → 1.7 → 2.9 倍，最多 2 小时）
            eff = min(interval * (1.7 ** miss), max(120, interval))
            # 上次说话 / 上次她开口（回复或主动），谁更近按谁算：
            # 她已经发过言了就重置倒计时，不会连珠炮
            anchor = max(
                last_in,
                float(self.state.get(f"pm_ping:{umo}", 0) or 0),
                float(self.state.get(f"last_bot_say:{umo}", 0) or 0),
            )
            if now - anchor < eff * 60 * random.uniform(0.85, 1.2):
                continue
            if not await self._is_friend(bot, str(rec.get("qq") or ""), assume_friend=False):
                logger.debug(f"[human_sim] 跳过非好友的主动私聊（{rec.get('qq')}）")
                continue
            cands.append(umo)
        if not cands:
            return
        umo = random.choice(cands)
        rec = self.state["pm_last"][umo]
        tail = "\n".join(rec.get("lines") or []) or "（就寒暄过几句）"
        prompt = (
            f"你和这个人之前私聊过：\n{tail}\n"
            "隔了一段时间，你想主动找他说句话"
            f"（不超过{int(self.cfg['max_chars'])}字）。像朋友想起他那样："
            "接上次没聊完的事、随口问候、或分享一件小事都行。"
            "别问「在吗」，别像客服，也别一口气说太多。"
        )
        text = await self._generate(prompt, umo, extra_system="现在是你在主动找老朋友说话。")
        text = self._humanize(text)
        if not text or not self._global_gate(umo):
            return
        await self._send(umo, [Plain(text)])
        self._note_act(umo, text, spoken=True)
        self.state[f"pm_ping:{umo}"] = now
        self.state[f"pm_miss:{umo}"] = int(self.state.get(f"pm_miss:{umo}", 0) or 0) + 1
        self._save_state()
        logger.info(f"[human_sim] 主动私聊 -> {rec.get('qq')}：{text[:30]}")

    # ---------- 定时问候：早/晚/饭点，每天每槽位最多一次 ----------
    async def _greeting_check(self) -> None:
        slots = self.cfg.get("greeting_slots") or []
        if not slots or not self.cfg["watch_groups"]:
            return
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        used = self.state.setdefault("greet_day", {})
        if used.get("day") != day:
            used.clear()
            used["day"] = day
        for slot in slots:
            name = str(slot.get("name") or "").strip()
            if not name or used.get(name):
                continue
            # 巡查 20 分钟一轮，命中该小时的前 50 分钟即可，不会漏
            if now.hour != int(slot.get("hour") or -1) or now.minute > 50:
                continue
            if random.random() >= float(slot.get("probability", 0.5)):
                continue
            used[name] = True
            umo = self.cfg.get("greeting_group") or random.choice(self.cfg["watch_groups"])
            if umo not in self.cfg["watch_groups"]:
                umo = random.choice(self.cfg["watch_groups"])
            # 时间点到了也不硬发：群里刚有人说话 / 我自己刚开过口，
            # 这时候再发「早安晚安」就像在自言自语，跳过这个槽位
            quiet = float(self.cfg.get("greeting_quiet_minutes", 15)) * 60
            last_in = float(self.state.get(f"last_msg:{umo}", 0) or 0)
            last_out = float(self.state.get(f"last_bot_say:{umo}", 0) or 0)
            if quiet > 0 and max(last_in, last_out) and time.time() - max(last_in, last_out) < quiet:
                logger.info(
                    f"[human_sim] 定时问候跳过（{name}，群 {umo.rsplit(':', 1)[-1]} 不安静）"
                )
                continue
            prompt = (
                f"现在{now.hour}点，群里正安静。你随口发一句「{name}」那种话，"
                "不超过15字，像朋友随口一提，别像闹钟，别用「大家」「各位」。"
            )
            text = await self._generate(prompt, umo, limit=20)
            text = self._humanize(text)
            if not text or not self._global_gate(umo):
                continue
            await self._send(umo, [Plain(text)])
            self._note_act(umo, text, spoken=True)
            logger.info(
                f"[human_sim] 定时问候（{name}）-> {umo.rsplit(':', 1)[-1]}：{text[:20]}"
            )

    async def _topic(self, umo: str) -> None:
        if not self._global_gate(umo):
            return
        hist = list(self._history.get(umo, []))[-8:]
        # 冷场话头不看机器人自己以前的发言：否则它会把自己上一条冷场胡话
        # 当成别人说的，越编越野（「大肥鱼又在偷吃零食了」就是这么来的）
        own = set(self._bot_names())
        hist = [
            ln for ln in hist
            if str(ln).split("：", 1)[0] not in own
        ]
        tail = ("\n".join(hist) if hist else "（很久没人说话了）")
        me = next(iter(self._bot_names()), "大肥鱼")
        roster = await self._roster_brief(umo)
        roster_line = f"（群成员参考：{roster}）\n" if roster else ""
        prompt = (
            f"{roster_line}群里最近聊过：\n{tail}\n"
            f"你自己就是「{me}」，聊天记录里「{me}：」开头的是你自己说过的话，"
            f"绝不能用第三人称说「{me}又……」「{me}在……」这种话。\n"
            f"你冷不丁冒出来随口说一句（不超过{int(self.cfg['max_chars'])}字），"
            "可以是吐槽最近的事、抛个话题、或者就一句感慨。"
            "只说你此刻的状态或眼前的事，不要编自己没做过的经历，也别重复以前说过的话题。"
            "别像通知，别问「在吗」。"
        )
        text = await self._generate(prompt, umo)
        text = self._humanize(text)
        if not text:
            return
        chain: list = []
        # 冷场捞人：随机 @ 一位群友
        if random.random() < float(self.cfg["at_random_member_probability"]):
            at_id = await self._pick_random_member(umo)
            if at_id:
                chain.append(At(qq=at_id))
                chain.append(Plain(" "))
        chain.append(Plain(text))
        self._maybe_append_emoji(chain, text=text, umo=umo)
        await self._send(umo, chain)
        self._note_act(umo, text, spoken=True)
        # 记下这次开话头的时间：巡查再看到冷场时得先等过一整个（带抖动的）窗口
        self.state[f"last_idle:{umo}"] = datetime.now().timestamp()
        logger.info(f"[human_sim] 冷场开话头（群 {umo.rsplit(':', 1)[-1]}）: {text[:40]}")
