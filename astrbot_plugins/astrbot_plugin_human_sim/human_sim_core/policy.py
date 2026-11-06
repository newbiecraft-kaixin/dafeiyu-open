"""回复决策的概率模型：时段、人数/活跃度缩放、沉默爬升与跟进窗口。"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from astrbot.api import logger

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class PolicyMixin:

    def _in_hours(self) -> bool:
        h = datetime.now().hour
        return int(self.cfg["start_hour"]) <= h < int(self.cfg["end_hour"])

    def _night_scale(self) -> float:
        """时段外（夜间）的频率系数；活动时段内恒为 1.0。"""
        try:
            return 1.0 if self._in_hours() else float(
                self.cfg.get("night_frequency_scale", 1.0)
            )
        except (TypeError, ValueError):
            return 1.0

    async def _group_member_count(self, umo: str) -> int:
        """群人数（带缓存）。取不到就返回 0，调用方按「小群」处理。"""
        gid = self._gid_of_umo(umo)
        if not gid.isdigit():
            return 0
        now = time.time()
        cached = self._gsize_cache.get(gid)
        ttl = float(self.cfg.get("audience_size_ttl_seconds", 600) or 600)
        if cached and now - cached[0] < ttl:
            return cached[1]
        bot = self._bots.get(umo)
        if bot is None:
            return cached[1] if cached else 0
        try:
            res = await bot.api.call_action("get_group_info", group_id=int(gid))
            n = int((res or {}).get("member_count") or 0)
            if n > 0:
                self._gsize_cache[gid] = (now, n)
                return n
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 取群人数失败 {gid}: {type(e).__name__}: {e}")
        return cached[1] if cached else 0

    def _audience_scale_sync(self, umo: str, count: int) -> float:
        """人数系数 × 活跃度系数。人数越多越低；越冷场越高（免得小群没内容）。"""
        try:
            lo = float(self.cfg.get("audience_min_size", 100) or 100)
            hi = float(self.cfg.get("audience_max_size", 2000) or 2000)
            floor = float(self.cfg.get("audience_min_factor", 0.25) or 0.25)
        except (TypeError, ValueError):
            lo, hi, floor = 100.0, 2000.0, 0.25
        # 人数系数：≤lo 人恒为 1；到 hi 人降到 floor（对数插值，200 人不会一下砍太狠）
        if count and count > 0 and hi > lo:
            c = min(max(float(count), lo), hi)
            ratio = math.log(c / lo) / math.log(hi / lo)
            size_f = 1.0 - (1.0 - floor) * min(max(ratio, 0.0), 1.0)
        else:
            size_f = 1.0
        # 活跃度系数：拿近 5 分钟条数和「这个规模的群正常该有多少条」比
        try:
            window = float(self.cfg.get("audience_activity_window", 300) or 300)
            f_min = float(self.cfg.get("audience_activity_min", 0.35) or 0.35)
            f_max = float(self.cfg.get("audience_activity_max", 3.0) or 3.0)
        except (TypeError, ValueError):
            window, f_min, f_max = 300.0, 0.35, 3.0
        ts = self._msg_ts.get(umo)
        now = datetime.now().timestamp()
        n = sum(1 for t in ts if now - t <= window) if ts else 0
        expect = max(2.0, min(30.0, (count or lo) * 0.012))   # 2000 人群≈24 条/5min
        # 直接线性比值：5 分钟消息越多 → 系数越小（不取平方根，衰减更明确）
        act_f = (expect + 1.0) / (n + 1.0)
        act_f = min(max(act_f, f_min), f_max)
        # 总系数封顶：小群冷场时 act_f 能顶到 3.0，乘上人数系数 1.0 会让抽样概率 >100%
        # （每条都开口）。封顶 1.6 后，冷场是「更容易接话」，而不是「逢话必接」，
        # 真正的节奏还有冷却和每分钟限额兜着。
        return min(size_f * act_f, 1.6)

    def _self_topic_boost(self, text: str, p: float) -> float:
        """话题跟自己有关（在聊机器人、问「你在吗」这类）→ 概率按倍数抬高。

        点名（bot_names 命中）在调用处已经是「必进决策」，这里补的是没被点名、
        但明显在说我/讨论我的情况——真人听到别人聊自己会忍不住插一句。
        """
        try:
            factor = float(self.cfg.get("self_topic_boost", 2.5) or 2.5)
        except (TypeError, ValueError):
            factor = 2.5
        if factor <= 1.0:
            return p
        kws = self.cfg.get("self_topic_keywords") or []
        if any(k and k in text for k in kws):
            return min(1.0, p * factor)
        return p

    def _ramp_probability(self, umo: str, p: float) -> float:
        """沉默补偿：长时间没开口就把概率往 1.0 拉，发过言后自动落回基准。

        p 是已经乘过夜间/人群系数的概率；沉默是从「上次这个会话里发过任何消息」
        算起的（_send 里统一记 last_bot_say），所以回话、发表情都算开口。
        """
        p = max(0.0, min(1.0, p))
        if p <= 0:
            # 夜间/熄灯系数算出 0：再沉默也别把它「补偿」回 1.0，否则半夜照样冒泡
            return 0.0
        if not self.cfg.get("silence_ramp", True) or p >= 1.0:
            return p
        key = f"last_bot_say:{umo}"
        last = float(self.state.get(key, 0) or 0)
        now = datetime.now().timestamp()
        if not last:
            # 刚重启/新会话还没有记录：从这一刻开始计时，别一上来就 100%
            self.state[key] = now
            return p
        try:
            start = float(self.cfg.get("silence_ramp_start_minutes", 45) or 45)
            full = float(self.cfg.get("silence_ramp_full_minutes", 180) or 180)
        except (TypeError, ValueError):
            start, full = 45.0, 180.0
        if full <= start:
            full = start + 1.0
        gap_min = (now - last) / 60.0
        if gap_min <= start:
            return p
        t = min(1.0, (gap_min - start) / (full - start))
        return p + (1.0 - p) * t

    # ---------- 跟进窗口：被找过之后专心盯住一个人 ----------
    def _followup_start(self, umo: str, uid: str, refill: bool = True) -> None:
        """有人 @/点名/引用机器人之后，开一段「专项关注」窗口。

        refill=True：又有人正式找你 → 时长续上、次数补满；
        refill=False：只是回完你一句 → 只续时长，不补次数（否则对聊起来没完没了）。
        """
        if not uid or not uid.isdigit():
            return
        try:
            span = float(self.cfg.get("followup_seconds", 180) or 0)
            full = int(self.cfg.get("followup_max_messages", 8) or 0)
        except (TypeError, ValueError):
            span, full = 180.0, 8
        if span <= 0 or full <= 0:
            return
        now = datetime.now().timestamp()
        old = self.state.get(f"followup:{umo}") or {}
        same = str(old.get("uid") or "") == uid
        if same:
            if refill:
                left = full
                until = now + span
            else:
                # 回完你一句只保留原窗口，不再往后顺延：到期就结束，
                # 否则一直对聊会把窗口无限续下去，越聊越频繁
                left = int(old.get("left") or 0)
                until = float(old.get("until") or 0)
        else:
            if not refill:
                # 回的是别人，不替 TA 新开窗口，避免每回一句就换个人接着盯
                return
            left = full
            until = now + span
        self.state[f"followup:{umo}"] = {
            "uid": uid, "until": until, "left": left, "others": 0,
        }

    def _followup_window(self, umo: str, uid: str) -> dict | None:
        """当前是否在盯这个人；窗口过期或额度用完就自动清掉。"""
        if not uid or not uid.isdigit():
            return None
        fu = self.state.get(f"followup:{umo}") or {}
        if not fu or str(fu.get("uid") or "") != uid:
            return None
        now = datetime.now().timestamp()
        if now >= float(fu.get("until") or 0) or int(fu.get("left") or 0) <= 0:
            self.state.pop(f"followup:{umo}", None)
            return None
        return fu

    def _followup_active(self, umo: str) -> bool:
        """窗口是否开着（不管盯的是谁）；过期或额度用完顺手清掉。"""
        fu = self.state.get(f"followup:{umo}") or {}
        if not fu:
            return False
        now = datetime.now().timestamp()
        if now >= float(fu.get("until") or 0) or int(fu.get("left") or 0) <= 0:
            self.state.pop(f"followup:{umo}", None)
            return False
        return True

    def _followup_note_other(self, umo: str) -> None:
        """窗口盯的是 A，别人插了一句：计数 +1，超过容忍值才放走窗口。

        活跃群里对话间隙几乎必然有人插话，直接因此清窗会把「被 @ 后盯住
        同一个人」掐成普通概率插话；容忍几条，TA 再开口就清零。
        """
        fu = self.state.get(f"followup:{umo}") or {}
        if not fu:
            return
        fu["others"] = int(fu.get("others") or 0) + 1
        tol = int(self.cfg.get("followup_other_tolerance", 2) or 0)
        if tol > 0 and fu["others"] > tol:
            self.state.pop(f"followup:{umo}", None)
        else:
            self.state[f"followup:{umo}"] = fu

    def _followup_reset_others(self, umo: str, uid: str) -> None:
        """TA 自己又开口了：插话计数清零，窗口继续盯。"""
        fu = self.state.get(f"followup:{umo}") or {}
        if not fu or str(fu.get("uid") or "") != uid:
            return
        if fu.get("others"):
            fu["others"] = 0
            self.state[f"followup:{umo}"] = fu

    def _followup_gap_ok(self, umo: str, uid: str) -> bool:
        """窗口内也要有最小间隔：刚为 TA 决策过，紧跟着的碎句先不重复接。"""
        fu = self.state.get(f"followup:{umo}") or {}
        if not fu:
            return False
        gap = float(self.cfg.get("followup_min_gap_seconds", 10) or 0)
        if gap <= 0:
            return True
        return datetime.now().timestamp() - float(fu.get("last") or 0) >= gap

    def _followup_consume(self, umo: str, uid: str) -> None:
        """为这个人真正启动一次决策：只扣一次额度，不顺延窗口时长。"""
        fu = self.state.get(f"followup:{umo}") or {}
        if not fu or str(fu.get("uid") or "") != uid:
            return
        fu["left"] = max(0, int(fu.get("left") or 0) - 1)
        fu["last"] = datetime.now().timestamp()
        self.state[f"followup:{umo}"] = fu

    async def _audience_scale(self, umo: str) -> float:
        """自主发言概率的统一缩放系数（关闭 audience_scaling 时恒为 1.0）。"""
        if not self.cfg.get("audience_scaling", True):
            return 1.0
        try:
            count = await self._group_member_count(umo)
            scale = self._audience_scale_sync(umo, count)
            logger.debug(
                f"[human_sim] 人群系数 {self._gid_of_umo(umo) or umo}: "
                f"人数={count or '(未知)'} → x{scale:.2f}"
            )
            return scale
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 人群系数计算失败: {type(e).__name__}: {e}")
            return 1.0
