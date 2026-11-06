"""指令与群管理：帮助、点赞、进退群提醒、余额、禁言/解禁与限流。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sqlite3
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.config.default import DB_PATH
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

from .config import DATA_ROOT, PLUGIN_DIR
from .prompts import LIKE_ACK_LINES
from .text import _IDENTITY_TAG_RE

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class CommandsMixin:

    # ---------- 指令限流（防刷屏/防风控） ----------
    _CMD_HITS: dict[str, list[float]] = {}
    _CMD_WARN: dict[str, float] = {}

    @filter.event_message_type(filter.EventMessageType.ALL, priority=200)
    async def _command_rate_guard(self, event: AstrMessageEvent):
        """非管理员指令限流：单人 5 次/60 秒、单会话 25 次/60 秒。

        超过就一直静默拦掉（不再发提示），同一人 3 分钟内只警告一次，
        避免提醒本身又变成刷屏。
        """
        text = (event.message_str or "").strip()
        if not text.startswith(("/", "／")):
            return
        now = time.time()
        uid = str(event.get_sender_id() or "")
        if not uid:
            return
        owner = str(self.cfg.get("owner_id") or "").strip()
        if (owner and uid == owner) or self._is_manager(event) or event.is_admin():
            return
        win = 60.0
        ku, kg = f"u:{uid}", f"g:{event.unified_msg_origin}"
        hu = [t for t in self._CMD_HITS.get(ku, []) if now - t < win]
        hg = [t for t in self._CMD_HITS.get(kg, []) if now - t < win]
        over = len(hu) >= 5 or len(hg) >= 25
        hu.append(now)
        hg.append(now)
        self._CMD_HITS[ku] = hu
        self._CMD_HITS[kg] = hg
        if len(self._CMD_HITS) > 2000:
            for k in [k for k, v in self._CMD_HITS.items() if not v or now - v[-1] > 600]:
                self._CMD_HITS.pop(k, None)
        if not over:
            return
        event.stop_event()
        if now - self._CMD_WARN.get(uid, 0) < 180:
            return
        self._CMD_WARN[uid] = now
        yield event.plain_result("指令发太快了，歇会儿再试。")

    # ---------- 退群 / 被踢检测 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _on_group_notice(self, event: AstrMessageEvent):
        """被踢出群 / 主动退群 / 被拉进群：清状态 + 告知主人。

        为什么必须有：被踢之后巡查循环还在往那个群发消息，每轮失败刷日志；
        冷场计时、潜水池名单也全都留着；更糟的是主人以为机器人还在群里，
        喊半天没动静才发现是被踢了。这里第一时间断干净并通报。
        """
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if not isinstance(raw, dict) or raw.get("post_type") != "notice":
                return
            ntype = str(raw.get("notice_type") or "")
            # 表情回应通知（各版本 NapCat 事件名略有出入，统一按子串兜住）
            if "emoji_like" in ntype:
                # umo 在这条分支之前还没赋值（原来直接用 umo 会 NameError，
                # 异常又被外层吞掉，导致「自己消息下的表情回应」一直识别不到）
                umo = event.unified_msg_origin
                await self._on_emoji_like(umo, raw)
                return
            if ntype not in ("group_decrease", "group_increase"):
                return
            gid = str(raw.get("group_id") or "")
            self_id = str(raw.get("self_id") or "")
            uid = str(raw.get("user_id") or "")
            if not gid or not uid:
                return
            umo = event.unified_msg_origin
            tag = umo.rsplit(":", 1)[-1]
            sub = str(raw.get("sub_type") or "")
            if uid != self_id:
                # 群友自己进/退群：按本群 /入群 /退群 设置的消息发提醒（机器人自己进出走下面）
                await self._member_notice(event, umo, raw, ntype)
                return
            if ntype == "group_increase":
                logger.info(f"[human_sim] 被拉进群 {tag}（{sub}）")
                await self._notify_owner(f"被拉进新群 {tag}，要接管就把它加进 config.json 的 watch_groups")
                return
            # —— 退群 / 被踢 ——
            logger.warning(f"[human_sim] 已不在群 {tag}（sub_type={sub}），清运行时状态")
            for d in (self._history, self._bots, self._msg_ts, self._roster):
                d.pop(umo, None)
            for k in [k for k in self.state if k.endswith(f":{umo}")]:
                self.state.pop(k, None)
            self.state[f"left_group:{umo}"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_state()
            if umo in (self.cfg.get("watch_groups") or []):
                self.cfg["watch_groups"] = [
                    g for g in self.cfg["watch_groups"] if g != umo
                ]
                if self._save_watch_groups():
                    logger.info(f"[human_sim] 已把 {tag} 从 watch_groups 移除")
            why = "被人踢了" if sub == "kick_me" else "退群了"
            await self._notify_owner(
                f"{why}：{tag}（sub_type={sub}），已从这个群撤掉潜水池和巡查"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 群通知处理异常: {type(e).__name__}: {e}")

    def _save_watch_groups(self) -> bool:
        """只改 config.json 里的 watch_groups，其余键原样保留（别把用户手写的配置冲掉）"""
        path = os.path.join(PLUGIN_DIR, "config.json")
        try:
            data: dict = {}
            if os.path.exists(path):
                with open(path, encoding="utf-8-sig") as f:
                    data = json.load(f) or {}
            data["watch_groups"] = self.cfg["watch_groups"]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except (OSError, ValueError) as e:
            logger.warning(f"[human_sim] watch_groups 写回失败: {e}")
            return False

    async def _member_notice(
        self, event: AstrMessageEvent, umo: str, raw: dict, ntype: str
    ) -> None:
        """群友进群 / 退群时，把本群设置的那句话发出去。

        设置存在 state（join_notice:umo / leave_notice:umo），由 /入群 /退群 写。
        模板变量：{name}=昵称  {qq}=QQ号  {group}=群号  {operator}=操作人。
        """
        if not self.cfg.get("member_notice_enabled", True):
            return
        key = "join_notice" if ntype == "group_increase" else "leave_notice"
        tpl = str(self.state.get(f"{key}:{umo}") or "").strip()
        if not tpl:
            return
        uid = str(raw.get("user_id") or "")
        # 退群事件 NapCat 不带昵称、成员也已经不在列表里，只能显示 QQ 号
        name = str(raw.get("nickname") or "").strip()
        if not name and ntype == "group_increase":
            name = await self._member_name(umo, uid)
        op = str(raw.get("operator_id") or "")
        text = (
            tpl.replace("{name}", name or uid)
            .replace("{qq}", uid)
            .replace("{group}", umo.rsplit(":", 1)[-1])
            .replace("{operator}", op if op and op != "0" else "")
        ).strip()
        # 退群事件永远没有昵称，QQ 号是唯一能定位到人的信息：
        # 模板里没显式写 {qq} 也补在末尾，不然群里根本不知道是谁走了
        if ntype != "group_increase" and uid and "{qq}" not in tpl:
            text = f"{text}（{uid}）" if text else uid
        if not text:
            return
        bot_ref = getattr(event, "bot", None)
        if bot_ref is not None:
            self._bots[umo] = bot_ref
        try:
            sid = str(raw.get("self_id") or event.get_self_id() or "")
        except Exception:  # noqa: BLE001
            sid = str(raw.get("self_id") or "")
        if sid.isdigit():
            self.state[f"self_id:{umo}"] = sid
        try:
            await self._send(umo, [Plain(text)])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 进/退群提醒发送失败: {type(e).__name__}: {e}")
            return
        self._history.setdefault(umo, deque(maxlen=30)).append(f"大肥鱼：{text}")
        logger.info(
            f"[human_sim] 群友{'进群' if ntype == 'group_increase' else '退群'}提醒已发（{uid}）"
        )

    async def _notify_owner(self, msg: str) -> None:
        """给主人发私聊通报（没配 owner_id 就只落日志）"""
        if not self.cfg.get("group_leave_notify", True):
            return
        owner = str(self.cfg.get("owner_id") or "").strip()
        if not owner.isdigit():
            logger.info(f"[human_sim] 未配置 owner_id，事件只记日志：{msg}")
            return
        bot = next(iter(self._bots.values()), None)
        if bot is None:
            logger.info(f"[human_sim] 没有可用 bot，无法通报主人：{msg}")
            return
        sid = ""
        for k, v in self.state.items():
            if k.startswith("self_id:") and str(v).isdigit():
                sid = str(v)
                break
        params: dict = {
            "user_id": int(owner),
            "message": [{"type": "text", "data": {"text": f"[大肥鱼] {msg}"}}],
        }
        if sid:
            params["self_id"] = int(sid)
        try:
            await bot.api.call_action("send_private_msg", **params)
            logger.info(f"[human_sim] 已通报主人：{msg}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 通报主人失败: {type(e).__name__}: {e}")

    async def _is_friend(self, bot, uid: str, assume_friend: bool = True) -> bool:
        """uid 是否好友（缓存 2 分钟；查询失败默认按好友处理，避免误拦正常回复）。

        assume_friend=False 用于「主动发消息」：查不到好友列表就当作不是好友，宁可不发。
        """
        now = time.time()
        cache = self._friend_cache
        if now - float(cache.get("ts", 0) or 0) > 120:
            try:
                res = await bot.api.call_action("get_friend_list")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[human_sim] 查好友列表失败: {type(e).__name__}: {e}")
                return assume_friend
            if isinstance(res, dict):
                res = res.get("data") or []
            if not isinstance(res, list):
                return assume_friend
            cache["ids"] = {str(u.get("user_id") or "") for u in res if isinstance(u, dict)}
            cache["ts"] = now
        ids = cache.get("ids")
        if not ids:
            return assume_friend
        return str(uid) in ids

    # ---------- 禁言 / 同发送者限流 ----------
    def _is_muted(self, umo: str) -> bool:
        """本会话是否在禁言期内（/禁言 设置，/解禁 解除）。"""
        return time.time() < float(self.state.get(f"mute_until:{umo}", 0) or 0)

    def _sender_rate_limited(self, umo: str, uid: str) -> bool:
        """同一发送者短时间反复 @/引用机器人 → 限流。

        两只同名 AI 在群里互相 @ 时，每来回一轮都会带一次 @；限制单个发送者
        的定向消息频率，能把这种无休止的互 @ 直接掐断。
        """
        if not uid or not uid.isdigit():
            return False
        now = time.time()
        win = float(self.cfg.get("sender_rate_window_seconds", 60) or 60)
        mx = int(self.cfg.get("sender_rate_max_directed", 3) or 3)
        cool = float(self.cfg.get("sender_rate_cooldown_seconds", 180) or 0)
        cool_key = f"dir_cool:{umo}:{uid}"
        if now < float(self.state.get(cool_key, 0) or 0):
            return True
        key = f"dir_ts:{umo}:{uid}"
        ts = [t for t in (self.state.get(key) or []) if now - float(t) <= win]
        ts.append(now)
        self.state[key] = ts[-max(mx, 1):]
        if len(ts) > mx:
            self.state[cool_key] = now + cool
            logger.info(
                f"[human_sim] 发送者 {uid} 短时多次找机器人，限流 {cool:g}s"
                f"（{umo.rsplit(':', 1)[-1]}）"
            )
            return True
        return False

    def _can_control(self, event: AstrMessageEvent) -> bool:
        """禁言/解禁只给主人和群管理用。"""
        uid = str(event.get_sender_id() or "")
        return (
            uid == self._owner_uid()
            or event.is_admin()
            or str(getattr(event, "role", "") or "") == "owner"
        )

    # ---------- 手动测试命令 ----------
    @filter.command("普通")
    async def cmd_normal(self, event: AstrMessageEvent):
        """/普通 —— 所有人都能用的命令"""
        yield event.plain_result(self._normal_help(event))

    @filter.command("管理")
    async def cmd_manager(self, event: AstrMessageEvent):
        """/管理 —— 管理员（主人）才能用的命令"""
        if not self._is_manager(event):
            event.stop_event()
            return
        yield event.plain_result(self._manager_help())

    @staticmethod
    def _text_only(event: AstrMessageEvent) -> bool:
        """微信桥只能收发文字：媒体类和 QQ 专有指令在那边不成立。"""
        return str(event.get_platform_name() or "").lower() in (
            "wechatauto", "weixin_oc")

    def _normal_help(self, event: AstrMessageEvent) -> str:
        if self._text_only(event):
            return "\n".join([
                "会这些，直接发（/普通）",
                "",
                "/禁言 /解禁 /模型",
            ])
        lines = [
            "会这些，直接发（/普通）",
            "",
            "/视频 [b站|快手|抖音|西瓜] [词]",
            "/看视频 [链接]",
            "/赞我 /听歌 /歌曲 /模型 /禁言 /解禁",
        ]
        return "\n".join(lines)

    def _manager_help(self) -> str:
        lines = [
            "管理员命令（仅主人，/管理）",
            "",
            "【系统】",
            "/停",
            "/定时任务",
            "/余额",
            "/重置",
            "/转换",
            "",
            "【曲库】",
            "/扒歌",
            "",
            "【声线】",
            "/声线",
            "",
            "【群设置】",
            "/入群",
            "/退群",
        ]
        return "\n".join(lines)

    @staticmethod
    def _is_manager(event: AstrMessageEvent) -> bool:
        """主人/管理员：读 AstrBot 主配置 cmd_config.json 的 admins_id"""
        try:
            sid = str(event.get_sender_id() or "").strip()
            with open(
                os.path.join(DATA_ROOT, "cmd_config.json"), encoding="utf-8-sig"
            ) as f:
                admins = [str(a).strip() for a in (json.load(f).get("admins_id") or [])]
        except Exception:  # noqa: BLE001
            return False
        return bool(sid and sid in admins)

    @filter.command("赞我")
    async def cmd_like_me(self, event: AstrMessageEvent):
        """/赞我 —— 给自己的 QQ 名片点赞（只认带斜杠的 /赞我，回一句固定台词）"""
        if self._text_only(event):
            yield event.plain_result("微信端没有 /赞我，这个只在 QQ 用。")
            return
        # 只认显式打出来的 /赞我。@机器人后直接说「赞我」、私聊里直接说「赞我」，
        # 都会让 AstrBot 把这条当成 wake command，唤醒前缀被清空后命令照样命中；
        # 所以回到原始消息文本核对一次，没有斜杠就不点。
        orig = str(getattr(event.message_obj, "message_str", "") or "").strip()
        cur = _IDENTITY_TAG_RE.sub("", (event.message_str or "").strip()).strip()
        if not orig.startswith("/") and not cur.startswith("/"):
            logger.info(
                f"[human_sim] 只认 /赞我，这条没带斜杠，不点赞：{orig[:24]!r}"
            )
            return
        umo = event.unified_msg_origin
        bot = getattr(event, "bot", None)
        if bot is not None:
            self._bots[umo] = bot
        uid = str(event.get_sender_id() or "")
        sid = str(event.get_self_id() or "")
        if sid:
            self.state[f"self_id:{umo}"] = sid
        if not uid.isdigit() or bot is None:
            yield event.plain_result("没点上，过会儿再试")
            return
        done_key = f"like_done:{datetime.now().strftime('%Y-%m-%d')}:{uid}"
        if self.state.get(done_key):
            yield event.plain_result("今天已经给你点过了，明天再来")
            return
        ok, _err = await self._do_send_like(bot, uid, sid)
        line = random.choice(LIKE_ACK_LINES) if ok else "今天赞满啦，明天给你补"
        if ok:
            self.state[done_key] = True
        self.state[f"like_cool:{uid}"] = time.time()
        self._save_state()
        yield event.plain_result(line)

    @filter.command("入群")
    async def cmd_join_notice(self, event: AstrMessageEvent):
        """/入群 设置 消息｜移除｜测试 —— 有人进群时的提醒（按群生效）"""
        async for r in self._member_notice_cmd(event, "入群", "join_notice", "进群"):
            yield r

    @filter.command("退群")
    async def cmd_leave_notice(self, event: AstrMessageEvent):
        """/退群 设置 消息｜移除｜测试 —— 有人退群时的提醒（按群生效）"""
        async for r in self._member_notice_cmd(event, "退群", "leave_notice", "退群"):
            yield r

    def _notice_arg(self, event: AstrMessageEvent, word: str) -> str:
        """把「[主人(1000000000)] 入群 设置 xxx」剥成「设置 xxx」"""
        raw = _IDENTITY_TAG_RE.sub("", (event.message_str or "")).strip()
        raw = re.sub(r"[@＠]\S+\s*", "", raw).strip()
        return re.sub(rf"^[/／#！!]?\s*(?:{word})\s*", "", raw, flags=re.I).strip()

    async def _member_notice_cmd(
        self, event: AstrMessageEvent, word: str, key: str, label: str
    ):
        """进/退群提醒的命令入口（/入群 与 /退群 共用）"""
        arg = self._notice_arg(event, word)
        action = arg.split(None, 1)[0] if arg else ""
        if action in ("设置", "移除", "关闭", "测试", "试一下", "试"):
            if not self._is_manager(event):
                event.stop_event()
                return
        text = await self._notice_text(event, word, key, label, arg)
        if text:
            yield event.plain_result(text)

    async def _notice_text(
        self, event: AstrMessageEvent, word: str, key: str, label: str, arg: str
    ) -> str:
        """进/退群提醒的设置核心（命令层与 AI 工具共用），返回要回复的文本。"""
        gid = event.get_group_id()
        if not gid:
            return f"/{word} 只能在群里设置（私聊没有群可提醒）。"
        umo = event.unified_msg_origin
        cur = str(self.state.get(f"{key}:{umo}") or "").strip()
        if not arg or arg in ("状态", "查看", "帮助"):
            return f"本群{label}提醒：{'开' if cur else '关'}\n" + (
                f"当前消息：{cur}" if cur else "当前还没设置消息"
            )
        parts = arg.split(None, 1)
        action = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        if action == "设置":
            if not rest:
                return f"用法：/{word} 设置 消息内容（可以用 {{name}} 代替昵称）"
            self.state[f"{key}:{umo}"] = rest[:200]
            self._save_state()
            return f"{label}提醒已设置：\n{rest}\n有人{label}时就会发这句。"
        if action in ("移除", "关闭"):
            if not cur:
                return f"本群本来就没设{label}提醒。"
            self.state.pop(f"{key}:{umo}", None)
            self._save_state()
            return f"本群{label}提醒已关掉。"
        if action in ("测试", "试一下", "试"):
            if not cur:
                return f"先设置消息：/{word} 设置 消息内容"
            nick = (event.get_sender_name() or "").split("#")[0][:12] or "群友"
            sender = str(event.get_sender_id() or "")
            text = (
                cur.replace("{name}", nick)
                .replace("{qq}", sender)
                .replace("{group}", str(gid))
                .replace("{operator}", nick)
            )
            await self._send(umo, [Plain(text)])
            return f"已按设置发一条示例：\n{text}"
        return f"看不懂「{action}」。用法：/{word} 设置 消息内容 / /{word} 移除 / /{word} 测试"

    @filter.llm_tool(name="set_join_notice")
    async def tool_join_notice(self, event: AstrMessageEvent, action: str = "查看", message: str = ""):
        """设置本群有人进群时的欢迎提醒（按群生效）。

        Args:
            action(string): 查看 | 设置 | 移除 | 测试
            message(string): 设置时的提醒内容，可用 {name} 代替昵称
        """
        arg = "" if action in ("", "查看", "状态") else f"{action} {message}".strip()
        return await self._notice_text(event, "入群", "join_notice", "进群", arg)

    @filter.llm_tool(name="set_leave_notice")
    async def tool_leave_notice(self, event: AstrMessageEvent, action: str = "查看", message: str = ""):
        """设置本群有人退群时的提醒（按群生效）。

        Args:
            action(string): 查看 | 设置 | 移除 | 测试
            message(string): 设置时的提醒内容，可用 {name} 代替昵称
        """
        arg = "" if action in ("", "查看", "状态") else f"{action} {message}".strip()
        return await self._notice_text(event, "退群", "leave_notice", "退群", arg)

    @staticmethod
    def _fmt_tok(n) -> str:
        """token 数按万/亿缩写，一眼看清量级；小数字直接给。"""
        n = int(n or 0)
        if n >= 100000000:
            return f"{n / 100000000:.2f}亿"
        if n >= 10000:
            return f"{n / 10000:.1f}万"
        return f"{n:,}"

    def _today_token_line(self) -> str:
        """今天（本地时区）所有会话烧掉的 token：provider_stats 里按本地零点切。"""
        try:
            local = datetime.now().astimezone()
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            stamp = midnight.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5) as conn:
                rows = conn.execute(
                    "SELECT provider_id, SUM(token_input_other),"
                    " SUM(token_input_cached), SUM(token_output), COUNT(*)"
                    " FROM provider_stats WHERE created_at >= ? GROUP BY provider_id",
                    (stamp,),
                ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 今日 token 统计失败: {type(e).__name__}: {e}")
            return ""
        if not rows:
            return "今日：还没动过嘴"
        total = calls = tin = tout = 0
        parts = []
        for pid, tin_other, tin_cached, t_out, cnt in rows:
            row_in = (tin_other or 0) + (tin_cached or 0)
            row_total = row_in + (t_out or 0)
            total += row_total
            tin += row_in
            tout += t_out or 0
            calls += cnt or 0
            parts.append(f"{str(pid).rsplit('/', 1)[-1]} {self._fmt_tok(row_total)}")
        line = (
            f"今日：{self._fmt_tok(total)} tokens"
            f"（输入 {self._fmt_tok(tin)} · 输出 {self._fmt_tok(tout)} · {calls} 次）"
        )
        if len(parts) > 1:
            line += "\n明细：" + " · ".join(parts)
        return line

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("余额")
    async def cmd_balance(self, event: AstrMessageEvent):
        """/余额 —— 查 DeepSeek 余额并给充值入口（只对走官方 key 的会话生效）"""
        if not self._is_manager(event) or not self._recharge_allowed(event):
            # 静默拒绝也要掐掉事件：不然命令没产出，AstrBot 会把「余额」当普通消息丢给模型
            event.stop_event()
            return
        cur = await self._deepseek_balance_info()
        if not cur:
            yield event.plain_result("余额查不到（key 没配或接口不通），过会儿再试")
            return
        money = cur.get("total_balance", "?")
        cur_name = cur.get("currency", "CNY")
        unit = "元" if cur_name == "CNY" else cur_name
        topped = cur.get("topped_up_balance", "?")
        granted = cur.get("granted_balance", "?")
        state = "可用" if cur.get("is_available", True) else "已不可用"
        base = str(self.cfg.get("recharge_link_base") or "").rstrip("/")
        url = str(self.cfg.get("recharge_url") or "https://platform.deepseek.com/top_up")
        lines = [f"余额：{money} {unit}（充值 {topped} · 赠送 {granted}）· {state}"]
        usage = await asyncio.to_thread(self._today_token_line)
        if usage:
            lines.append(usage)
        lines.append(f"充值：{base or url}")
        yield event.plain_result("\n".join(lines))



    def _resolve_reaction_arg(self, raw: str) -> int | None:
        """表情回应参数：纯数字当表情 id；名字去 face_config.json 里匹配（黄脸/超级表情都认）。"""
        s = raw.strip().lstrip("/").strip()
        if s.isdigit():
            return int(s)
        for r in self._read_face_config():
            name = str(r.get("QDes") or "").lstrip("/").strip()
            sid = str(r.get("QSid") or "").strip()
            if sid.isdigit() and name and (s == name or s in name or name in s):
                return int(sid)
        return None

    # ---------- 禁言 / 解禁 ----------
    @filter.command("禁言")
    async def cmd_mute(self, event: AstrMessageEvent, arg: str = ""):
        """/禁言 [分钟] —— 谁都能用；默认 2 分钟，带数字按数字来"""
        umo = event.unified_msg_origin
        default = float(self.cfg.get("mute_default_minutes", 2) or 2)
        mins = default
        m = re.search(r"\d+(?:\.\d+)?", arg or "")
        if m:
            mins = float(m.group())
            # 带单位时按单位换算，方便 /禁言 30秒
            tail = (arg or "")[m.end():].strip().lower()
            if tail.startswith(("秒", "s", "sec")):
                mins = mins / 60.0
            elif tail.startswith(("小时", "h")):
                mins = mins * 60.0
        mins = max(0.1, min(mins, 1440.0))
        self.state[f"mute_until:{umo}"] = time.time() + mins * 60
        self._save_state()
        nick = str(event.get_sender_name() or "").strip() or "有人"
        logger.info(f"[human_sim] 禁言 {mins:g} 分钟（{umo}，by {nick}）")
        yield event.plain_result(f"好，我闭麦 {mins:g} 分钟。")

    @filter.command("解禁")
    async def cmd_unmute(self, event: AstrMessageEvent):
        """/解禁 —— 解除本群禁言"""
        if not self._can_control(event):
            yield event.plain_result("这个只有主人和群管理能用。")
            return
        umo = event.unified_msg_origin
        self.state.pop(f"mute_until:{umo}", None)
        self._save_state()
        logger.info(f"[human_sim] 解除禁言（{umo}）")
        yield event.plain_result("又能说话了。")
