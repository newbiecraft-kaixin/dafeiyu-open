"""说话人标签 + 群历史块瘦身 + 每人档案（按会话隔离）。

1. 说话人标签：群聊触发回复时，给当前消息补上 [昵称/时间]: 前缀。
   AstrBot 的群聊历史块每行格式是 [昵称/HH:MM:SS]: 内容，但触发回复的当前消息
   是裸文本——模型容易把历史块里最后发言的人当成当前说话人（张冠李戴）。
   本插件把当前消息也打上同样的标签，格式与历史块完全一致。

2. 历史块截断：活跃群里两次回复之间可能累积几十条群消息，builtin
   group_chat_context 会把它们全部注入 LLM——输入 token 的大头。
   这里把历史块截到最近 N 条，省掉大量输入 token，旧消息不影响接话。

3. 每人档案：会话按群/私聊各自隔离（同一个人在不同群互不相通，私聊单独一份），
   每个人的发言再按 QQ 号分别保存到 user_memory.json。回复时只注入"当前会话里
   当前说话人"的档案，既不串群，也不会把别人的历史算到这个人头上。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.active_event_registry import active_event_registry

from .filter import RecordUserMessageFilter, set_plugin_ref

try:
    from astrbot.core.platform.message_type import MessageType
except ImportError:  # 兼容不同版本导出路径
    from astrbot.api.event import MessageType  # type: ignore

try:
    from astrbot.core.agent.message import TextPart
except ImportError:
    TextPart = None  # 截断/注入功能需要 TextPart；拿不到就只打标签

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_PATH = os.path.join(PLUGIN_DIR, "user_memory.json")

PROFILE_MARK = "【你记得的这个人】"

DEFAULTS = {
    "history_max_records": 20,  # 群历史块最多注入最近多少条（0=不截断）
    "user_memory": True,  # 是否记录并注入每个人的档案
    "user_keep_msgs": 30,  # 每人保留最近多少条发言
    "user_max_users": 200,  # 每个会话最多存多少人的档案（超出按最久未发言淘汰）
    "user_min_msgs": 3,  # 至少聊过几句才注入，避免刚冒个泡就被"摸底"
    "user_recent_inject": 6,  # 注入时带几条最近发言
    "user_inject_chars": 300,  # 注入文本总长度上限
    "user_save_interval": 60,  # 写盘节流（秒）
}


@register(
    "astrbot_plugin_speaker_tag",
    "dafeiyu-local",
    "群聊当前消息补说话人标签 + 群历史块截断 + 每人档案按会话隔离存储注入",
    "v1.3.0",
)
class SpeakerTagPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = dict(DEFAULTS)
        cfg_path = os.path.join(PLUGIN_DIR, "config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8-sig") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.cfg.update(loaded)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[speaker_tag] 读取 config.json 失败: {e}")
        # 按会话（群/私聊 umo）隔离：{umo: {uid: 档案}}
        self._sessions: dict[str, dict[str, dict]] = {}
        self._dirty = False
        self._last_save = 0.0
        self._load_memory()
        set_plugin_ref(self)

    # ==================== 事件钩子 ====================

    @filter.custom_filter(RecordUserMessageFilter, False)
    async def _collect(self, event: AstrMessageEvent):
        """占位：真正的记录在 Filter.filter 里完成，这里不会被执行"""
        return

    @filter.on_llm_request()
    async def _tag_sender(self, event: AstrMessageEvent, req):
        """群聊当前消息加 [昵称/时间]: 前缀 + 截断历史块 + 注入说话人档案"""
        try:
            if not isinstance(event, AstrMessageEvent):
                return
            if event.get_message_type() == MessageType.GROUP_MESSAGE:
                prompt = req.prompt or ""
                if prompt.strip() and not prompt.lstrip().startswith("["):
                    name = (event.get_sender_name() or "群友").split("#")[0][:16]
                    t = datetime.now().strftime("%H:%M")
                    req.prompt = f"[{name}/{t}]: {prompt}"
                self._trim_history_block(req)
            self._inject_profile(event, req)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[speaker_tag] 处理失败: {type(e).__name__}: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置")
    async def reset_cn(self, event: AstrMessageEvent):
        """/重置 —— 清空本会话的对话历史、说话人档案和群画像"""
        umo = event.unified_msg_origin
        # 先掐掉本会话正在跑的任务，免得边清边写回
        active_event_registry.stop_all(umo, exclude=event)
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        if cid:
            await self.context.conversation_manager.update_conversation(
                umo, cid, history=[]
            )
        self._sessions.pop(self._session_of(event), None)
        self._dirty = True
        self._save_memory(force=True)
        # 广播给群历史块 / 长期记忆插件，让它们也清本会话
        event.set_extra("_clean_group_context_session", True)
        event.set_result(MessageEventResult().message("这个会话的对话和档案都清掉了。"))

    async def terminate(self):
        self._save_memory(force=True)
        logger.info("[speaker_tag] 每人档案已落盘")

    # ==================== 每人档案：记录 ====================

    def _session_of(self, event: AstrMessageEvent) -> str:
        """会话标识：群=群 umo，私聊=私聊 umo，同一个人不同群各自独立"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo:
            return umo
        try:
            gid = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            gid = ""
        sid = gid or str(event.get_sender_id() or "")
        try:
            platform = event.get_platform_name()
        except Exception:  # noqa: BLE001
            platform = "unknown"
        return f"{platform}:{sid}"

    def _record(self, event: AstrMessageEvent) -> None:
        try:
            if not self.cfg.get("user_memory", True):
                return
            uid = str(event.get_sender_id() or "").strip()
            if not uid or uid == str(event.get_self_id() or ""):
                return
            text = " ".join((event.get_message_str() or "").split())
            if not text or text.startswith("/"):
                return
            text = text[:300]
            now = datetime.now()
            users = self._sessions.setdefault(self._session_of(event), {})
            u = users.get(uid)
            if u is None:
                if len(users) >= int(self.cfg.get("user_max_users", 200)):
                    self._evict_one(users)
                u = users[uid] = {
                    "name": "",
                    "first": now.strftime("%Y-%m-%d %H:%M"),
                    "last": "",
                    "n": 0,
                    "msgs": [],
                }
            u["name"] = (event.get_sender_name() or u.get("name") or "").split("#")[0][:24]
            u["last"] = now.strftime("%Y-%m-%d %H:%M")
            u["n"] = int(u.get("n", 0)) + 1
            msgs = u.setdefault("msgs", [])
            msgs.append({"t": now.strftime("%m-%d %H:%M"), "c": text})
            keep = int(self.cfg.get("user_keep_msgs", 30) or 30)
            if keep > 0:
                del msgs[:-keep]
            self._dirty = True
            self._save_memory()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[speaker_tag] 记录消息失败: {type(e).__name__}: {e}")

    def _evict_one(self, users: dict[str, dict]) -> None:
        if not users:
            return
        victim = min(users, key=lambda k: users[k].get("last", ""))
        users.pop(victim, None)

    # ==================== 每人档案：注入 ====================

    def _profile_text(self, event: AstrMessageEvent) -> str:
        uid = str(event.get_sender_id() or "").strip()
        users = self._sessions.get(self._session_of(event), {})
        u = users.get(uid) if uid else None
        if not u:
            return ""
        n = int(u.get("n", 0))
        if n < int(self.cfg.get("user_min_msgs", 3)):
            return ""
        name = u.get("name") or event.get_sender_name() or "这个人"
        name = name.split("#")[0][:24]
        recent_n = int(self.cfg.get("user_recent_inject", 6) or 0)
        recent = [m for m in u.get("msgs", []) if m.get("c")][-recent_n:] if recent_n else []
        lines = [
            f"{PROFILE_MARK} {name}（QQ:{uid}）：你们聊过 {n} 句话，"
            f"最近一次是 {u.get('last', '')}。"
        ]
        if recent:
            lines.append(
                "他最近说过："
                + "；".join(f"{m.get('t', '')}「{m.get('c', '')[:80]}」" for m in recent)
            )
        lines.append("（只是你的印象，别复述这些原话，自然相处就好。）")
        text = "".join(lines)
        limit = int(self.cfg.get("user_inject_chars", 300) or 0)
        if limit > 0 and len(text) > limit:
            text = text[:limit] + "…"
        return text

    def _inject_profile(self, event: AstrMessageEvent, req) -> None:
        block = self._profile_text(event)
        if not block:
            return
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is not None and TextPart is not None:
            # 去掉上一轮注入的同名片段，避免多轮迭代重复堆积
            try:
                req.extra_user_content_parts = [
                    p
                    for p in parts
                    if not str(getattr(p, "text", "") or "").startswith(PROFILE_MARK)
                ]
            except Exception:  # noqa: BLE001
                pass
            part = TextPart(text=block)
            if hasattr(part, "mark_as_temp"):
                part = part.mark_as_temp()
            req.extra_user_content_parts.append(part)
            return
        req.prompt = f"{req.prompt or ''}\n\n{block}"

    # ==================== 每人档案：持久化 ====================

    def _load_memory(self) -> None:
        if not os.path.exists(MEMORY_PATH):
            return
        try:
            with open(MEMORY_PATH, encoding="utf-8-sig") as f:
                data = json.load(f)
            sessions = data.get("sessions") if isinstance(data, dict) else None
            if isinstance(sessions, dict):
                self._sessions = {
                    str(s): {str(k): v for k, v in users.items() if isinstance(v, dict)}
                    for s, users in sessions.items()
                    if isinstance(users, dict)
                }
                total = sum(len(u) for u in self._sessions.values())
                logger.info(
                    f"[speaker_tag] 载入 {total} 份个人档案（{len(self._sessions)} 个会话）"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[speaker_tag] 读取 user_memory.json 失败: {e}")

    def _save_memory(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        now = time.monotonic()
        interval = float(self.cfg.get("user_save_interval", 60) or 0)
        if not force and now - self._last_save < interval:
            return
        try:
            tmp = MEMORY_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"version": 3, "saved": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "sessions": self._sessions},
                    f, ensure_ascii=False, separators=(",", ":"),
                )
            os.replace(tmp, MEMORY_PATH)
            self._dirty = False
            self._last_save = now
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[speaker_tag] 写入 user_memory.json 失败: {e}")

    # ==================== 群历史块截断 ====================

    def _trim_history_block(self, req) -> None:
        max_records = int(self.cfg.get("history_max_records", 20))
        if max_records <= 0 or TextPart is None:
            return
        parts = getattr(req, "extra_user_content_parts", None)
        if not parts:
            return
        for i, part in enumerate(parts):
            text = getattr(part, "text", None)
            if not text or "--- BEGIN CONTEXT---" not in text:
                continue
            lines = text.split("\n")
            # footer：从 '--- END CONTEXT ---' 行到末尾
            end_i = len(lines)
            for j in range(len(lines) - 1, -1, -1):
                if "--- END CONTEXT ---" in lines[j]:
                    end_i = j
                    break
            # header：第一条记录行（'[' 开头）之前的所有行
            start_i = 0
            for j, line in enumerate(lines[:end_i]):
                if line.startswith("["):
                    start_i = j
                    break
            header, records, footer = lines[:start_i], lines[start_i:end_i], lines[end_i:]
            records = [r for r in records if r.strip()]
            if len(records) <= max_records:
                continue
            omitted = len(records) - max_records
            records = [f"（此前 {omitted} 条群消息已省略）"] + records[-max_records:]
            new_text = "\n".join(header + records + footer)
            try:
                parts[i] = TextPart(text=new_text)
                logger.debug(f"[speaker_tag] 历史块截断：省略 {omitted} 条，保留最近 {max_records} 条")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[speaker_tag] 历史块截断失败: {type(e).__name__}: {e}")
