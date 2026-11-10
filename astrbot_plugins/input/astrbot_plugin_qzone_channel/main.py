"""
astrbot_plugin_qzone_channel —— QQ 空间独立信道

和群聊/私聊完全分开的一条信道：
  1. 定时轮询机器人自己 QQ 空间的最新说说（走 NapCat get_cookies 拿 qzone cookie + g_tk，
     再直连 Qzone Web 接口，不依赖任何群消息事件）
  2. 每条说说下出现「别人新留的评论」就按大肥鱼口吻回一条
  3. 会话上下文用独立 session_id（qzone:{tid}），不写进群聊记忆，群里的对话也进不来

NapCat 本身没有空间事件推送，所以走轮询；reply_enabled 关闭时只读不回（先验证抓取）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .qzone_actions import QzoneActionsMixin
from .qzone_client import QzoneClientMixin
from .qzone_config import DATA_ROOT, DEFAULTS, PLUGIN_DIR, STATE_PATH
from .qzone_login import QzoneLoginMixin
from .qzone_reply import QzoneReplyMixin
from .qzone_web import _strip_html


@register(
    "astrbot_plugin_qzone_channel",
    "dafeiyu-local",
    "QQ空间独立信道：轮询自己说说下的新评论，独立上下文回评，不混入群聊记忆",
    "v1.0.0",
)
class QzoneChannel(QzoneClientMixin, QzoneReplyMixin, QzoneActionsMixin, QzoneLoginMixin, Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = dict(DEFAULTS)
        cfg_path = os.path.join(PLUGIN_DIR, "config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8-sig") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.cfg.update({k: loaded[k] for k in loaded if k in self.cfg})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[qzone_channel] 读取 config.json 失败: {e}")

        self._bot = None
        self.state: dict = {"replied": {}}
        self._load_state()
        self._task: asyncio.Task | None = None
        self._qr_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._last_report = ""
        self._post_blocked = False   # 回评接口被 qzonetoken 拦住（本轮）
        self._last_post_code = None  # 最近一次回评/留言的接口 code，排查用

    # ---------- 状态 ----------
    def _load_state(self) -> None:
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.state.update(data)
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"[qzone_channel] 状态保存失败: {e}")

    # ---------- 生命周期 ----------
    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())
        self._watch_task = asyncio.create_task(self._watch_scan_request())
        logger.info(
            f"[qzone_channel] 已启动：每 {self.cfg['poll_interval_minutes']} 分钟轮询一次空间评论，"
            f"回评={'开' if self.cfg.get('reply_enabled') else '关（只读）'}，"
            f"每条说说最多回 {self.cfg.get('max_replies_per_feed', 20)} 条"
            f"（每轮最多 {self.cfg.get('max_replies_per_round', 3)} 条），"
            "会话按 qzone:<tid> 隔离，不混群聊记忆"
        )

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
        if self._qr_task:
            self._qr_task.cancel()
        if getattr(self, "_watch_task", None):
            self._watch_task.cancel()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _grab_bot(self, event: AstrMessageEvent):
        """只借消息事件拿 aiocqhttp 客户端，不参与任何聊天流程。

        事件里的 bot 一定是正在收发消息的那个实例，比平台管理器里翻出来的可靠。
        """
        if str(event.get_platform_name() or "").lower() != "aiocqhttp":
            return
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        if self._bot is not bot:
            logger.info("[qzone_channel] 已从消息事件捕获 aiocqhttp 客户端")
        self._bot = bot

    @staticmethod
    def _bot_online(bot) -> bool:
        """有没有已注册的反向 ws 连接；没有就调不了 OneBot API。"""
        try:
            return bool(getattr(bot, "_wsr_api_clients", None))
        except Exception:  # noqa: BLE001
            return False

    def _ensure_bot(self):
        """拿一个真正连着 NapCat 的客户端：先看事件捕获的，再在平台实例里挑有连接的。"""
        if self._bot is not None and self._bot_online(self._bot):
            return self._bot
        fallback = self._bot
        try:
            for inst in self.context.platform_manager.get_insts():
                meta = inst.meta()
                if str(getattr(meta, "name", "")).lower() != "aiocqhttp":
                    continue  # 别的适配器（桌宠/官方 API）不支持 send_private_msg
                getter = getattr(inst, "get_client", None)
                if not callable(getter):
                    continue
                client = getter()
                if client is None:
                    continue
                if fallback is None:
                    fallback = client
                if self._bot_online(client):
                    self._bot = client
                    logger.info("[qzone_channel] 已从平台适配器拿到在线 aiocqhttp 客户端")
                    return self._bot
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[qzone_channel] 平台适配器取客户端失败: {e}")
        self._bot = fallback
        return self._bot

    # ---------- 主循环 ----------
    async def _loop(self) -> None:
        await asyncio.sleep(8)
        while True:
            replied_anything = False
            try:
                replied_anything = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._warn_once(f"[qzone_channel] 轮询失败: {type(e).__name__}: {e}")
            # 刚回完一条就 60 秒后补查：对方很可能马上接话，等满 5 分钟才看见太像机器人。
            # 没新评论就照常按配置间隔轮询，不白白多打请求。
            wait = 60 if replied_anything else max(2, int(self.cfg["poll_interval_minutes"])) * 60
            await asyncio.sleep(wait)

    def _warn_once(self, msg: str) -> None:
        """同一个错误 30 分钟只喊一次，免得日志被刷爆。"""
        now = time.time()
        if now - float(self.state.get("last_warn_ts") or 0) < 1800:
            return
        self.state["last_warn_ts"] = now
        self._save_state()
        logger.warning(msg)

    async def _tick(self) -> bool:
        """轮询一次。返回本轮有没有真的回出去评论（用于决定 60 秒后补查）。"""
        if not self.cfg.get("enabled"):
            return False
        if not self._ensure_bot():
            logger.warning("[qzone_channel] 还没拿到 QQ 客户端，本轮跳过")
            return False
        cookies, bkn, uin = await self._get_auth()
        self_nick = await self._get_self_nick()
        feeds = await self._fetch_feeds(cookies, bkn, uin)
        if not feeds:
            return False
        replied = self.state.setdefault("replied", {})
        if not self.state.get("seeded"):
            # 第一次接入：把已有的历史评论全部记账、不回。否则一开回评就会给半年前的评论补回复。
            n = 0
            for feed in feeds:
                tid = str(feed.get("tid") or "")
                if not tid:
                    continue
                keys = [self._comment_key(c) for c in (feed.get("commentlist") or [])]
                replied[tid] = list(dict.fromkeys(keys))[-200:]
                n += len(keys)
            self.state["seeded"] = True
            self._save_state()
            logger.info(f"[qzone_channel] 首次接入：记录 {n} 条历史评论，不补评")
            return False
        budget = max(1, int(self.cfg.get("max_replies_per_round", 3)))
        feed_cap = max(1, int(self.cfg.get("max_replies_per_feed", 20)))
        counts = self.state.setdefault("reply_counts", {})
        done = 0
        for feed in feeds:
            tid = str(feed.get("tid") or feed.get("id") or "")
            if not tid:
                continue
            used = int(counts.get(tid) or 0)
            if used >= feed_cap:
                # 这条说说已经回满上限：剩下的新评论一律不回，防止两个 AI 对刷到天亮
                if not self.state.get(f"cap_logged:{tid}"):
                    logger.info(
                        f"[qzone_channel] 说说 {tid[:10]} 已回满 {feed_cap} 条，"
                        "后续评论不再自动回复"
                    )
                    self.state[f"cap_logged:{tid}"] = True
                    self._save_state()
                continue
            feed_text = _strip_html(feed.get("content") or "")
            seen = set(replied.get(tid) or [])
            own_texts = set(self.state.get("own_texts", {}).get(tid) or [])
            min_chars = int(self.cfg.get("min_comment_chars", 2))
            fresh = []
            for c in feed.get("commentlist") or []:
                key = self._comment_key(c)
                if key in seen:
                    continue
                cuin = str(c.get("uin") or "")
                cname = str(c.get("name") or c.get("nickname") or "")
                if cuin and cuin == uin:  # 自己发的评论/回复，不算
                    continue
                if self_nick and cname == self_nick:  # 老接口没带 uin 时靠昵称兜底
                    continue
                text = _strip_html(c.get("content") or "")
                if text and text in own_texts:  # 自己刚回出去的文本，别当成新评论
                    continue
                if len(text) < min_chars:
                    continue
                # 楼中楼：别人回别人（比如 C 回 B）不插嘴，只回明确回我的
                target = str(c.get("target") or "")
                parent = str(c.get("parent") or "")
                if target:
                    if target != uin:
                        continue
                elif parent and parent != uin:
                    continue
                fresh.append(
                    (
                        int(c.get("createTime") or c.get("create_time") or 0),
                        c, key, text, bool(target or parent),
                    )
                )
            fresh.sort(key=lambda x: x[0])
            for _, c, key, text, replying_me in fresh:
                if done >= budget:
                    break
                who = c.get("name") or c.get("nickname") or str(c.get("uin") or "有人")
                logger.info(f"[qzone_channel] 新评论 tid={tid[:10]} {who}: {text[:40]}")
                if not self.cfg.get("reply_enabled"):
                    seen.add(key)
                    continue
                out = await self._reply_one(tid, str(who), text, feed_text, replying_me)
                if not out:
                    logger.warning("[qzone_channel] 没生成出有效回复，跳过")
                    continue
                ok = await self._post_comment(cookies, bkn, uin, tid, out)
                if ok:
                    logger.info(
                        f"[qzone_channel] 已回评 {who}: {out[:60]}"
                        f"（本条说说第 {used + 1}/{feed_cap} 条）"
                    )
                    own = self.state.setdefault("own_texts", {}).setdefault(tid, [])
                    own.append(out[:120])
                    del own[:-50]
                    seen.add(key)
                    done += 1
                    used += 1
                    counts[tid] = used
                    if used >= feed_cap:
                        self.state[f"cap_logged:{tid}"] = True
                        logger.info(
                            f"[qzone_channel] 说说 {tid[:10]} 已回满 {feed_cap} 条，"
                            "停止自动回复"
                        )
                        break
                else:
                    logger.warning("[qzone_channel] 回评失败，留下一轮重试")
                    if self._post_blocked:
                        # 回评接口要 qzonetoken，当前账号拿不到 → 把拟好的回复转给主人，别白生成
                        await self._notify_owner(feed_text, str(who), text, out)
                        seen.add(key)
            replied[tid] = list(seen)[-200:]
        self._save_state()
        return done > 0

    # ---------- LLM 工具 / 命令 ----------
    @filter.llm_tool(name="get_qzone_feeds")
    async def tool_qzone_feeds(
        self, event: AstrMessageEvent, target: str = "", count: int = 0
    ):
        """查看某个 QQ 号最近的空间动态（说说）。当用户说"看看XX的空间"、"XX最近发说说没"、"查一下TA的空间动态"时使用。

        用户说"看看你的空间""看看空间"时 target 留空，默认看机器人自己的空间。
        主人和好友的空间一律不扫（会返回一句说明），别反复换号试；群聊里聊到的内容照常可以看。
        对方设了仅好友可见/私密时只会返回说明，不要编造内容。

        Args:
            target(string): 目标 QQ 号；消息里有 @ 某人时可以留空
            count(int): 要看几条，至少 3 条（资料太少没法用），默认 5，最多 20；用户没指定就留 0
        """
        t, n = self._parse_view_args(event, target, count)
        n = max(3, n)  # 素材下限：至少 3 条，太少不够当资料
        return await self.view_qzone_feeds(t, n)

    @filter.command("看空间")
    async def cmd_view_space(
        self, event: AstrMessageEvent, target: str = "", count: int = 0
    ):
        """/看空间 <QQ号|@某人> [条数] —— 看空间，逐条点赞+评论；做过的自动跳过"""
        if str(event.get_platform_name() or "").lower() in (
                "wechatauto", "weixin_oc"):
            yield event.plain_result("微信端用不了 QQ 空间，/看空间 只在 QQ 用。")
            return
        t, n = self._parse_view_args(event, target, count)
        if not t:
            yield event.plain_result("给个 QQ 号或 @ 某人。")
            return
        yield event.plain_result(
            await self.view_like_comment(t, n)
        )

    @filter.llm_tool(name="qzone_comment")
    async def tool_qzone_comment(
        self, event: AstrMessageEvent, target: str = "", content: str = ""
    ):
        """给某个 QQ 号最近一条说说留言（评论）。用户说"去TA空间留个言"、"评论一下TA的说说"时使用。
        好友和主人的空间不扫，遇到会返回一句说明。

        Args:
            target(string): 目标 QQ 号；消息里有 @ 某人时可以留空
            content(string): 留言内容
        """
        t, content = self._parse_message_args(event, target, content)
        if not t:
            return "没拿到要留言的 QQ 号"
        if not content:
            return "留言内容为空"
        return await self.leave_message(t, content)

    @filter.llm_tool(name="qzone_like")
    async def tool_qzone_like(
        self, event: AstrMessageEvent, target: str = "", index: int = 1
    ):
        """给某个 QQ 号最近的空间说说点赞。当用户说"给XX点个赞"、"去赞一下TA的说说"时使用。

        Args:
            target(string): 目标 QQ 号；消息里有 @ 某人时可以留空
            index(int): 第几条，1=最近一条，默认 1
        """
        t, idx = self._parse_like_args(event, target, index)
        if not t:
            return "没拿到要点赞的 QQ 号"
        return await self.like_qzone_feed(t, idx)

