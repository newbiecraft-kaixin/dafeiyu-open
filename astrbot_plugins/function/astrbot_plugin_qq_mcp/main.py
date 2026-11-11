"""把 QQ 能力包成 MCP（streamable-http），给 DSH 直接调用。

设计要点：
- 只做「补发气泡 / 查历史 / 戳一戳」，最终回复仍由 AstrBot 正常发出，避免重复。
- 补发支持拆条 + 按字数模拟打字停顿，并可用 delay_seconds 让第一条先到。
- 会话缺省取最近 10 分钟内活跃的会话，也兼容 bridge 传来的 "provider:<umo>" 形式。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import socket
import time
from collections import deque
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

from .auth import _BearerAuth

try:
    import uvicorn
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # noqa: BLE001
    uvicorn = None
    FastMCP = None
    _MCP_IMPORT_ERROR = exc
else:
    _MCP_IMPORT_ERROR = None

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.dirname(os.path.dirname(PLUGIN_DIR))  # .../data
CMD_CONFIG_PATH = os.path.join(DATA_DIR, "cmd_config.json")
# human_sim 用 mute_until:<umo> 决定自己是否闭麦；平台侧改了禁言必须写回这里，
# 否则机器人会一直以为自己还被禁着（或者解禁后还继续装哑巴）。
HUMAN_SIM_STATE_PATH = os.path.join(
    DATA_DIR, "plugin_data", "astrbot_plugin_human_sim", "state.json"
)


def _port_free(host: str, port: int) -> bool:
    """端口没被监听才算可用；连得上就说明已有实例在跑。"""
    host = "127.0.0.1" if str(host) in ("", "0.0.0.0", "::") else str(host)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex((host, int(port))) != 0
    except OSError:
        return True


DEFAULTS = {
    "enabled": True,
    "host": "127.0.0.1",
    "port": 18083,
    "path": "/mcp",
    "token_file": "~/.dsh/qq-mcp-token.txt",
    "fallback_token_file": "~/.dsh/qq-bridge-token.txt",
    "transcripts_path": "~/.dsh/astriax-native/bridge/transcripts.json",
    "recent_ttl_seconds": 900,
    "max_bubbles": 6,
    "max_chars_per_bubble": 160,
    "typing_base_seconds": 0.25,
    "typing_per_char_seconds": 0.06,
    "max_typing_delay_seconds": 4.0,
    "max_initial_delay_seconds": 20.0,
    "max_events": 500,
    "suppress_default_reply": False,
}


@register(
    "astrbot_plugin_qq_mcp",
    "dafeiyu-local",
    "QQ 能力 MCP 服务：补发气泡、查历史、戳一戳",
    "v1.0.0",
)
class QQMcp(Star):
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
                logger.warning(f"[qq_mcp] 读取 config.json 失败: {e}")

        self._recent: dict[str, dict[str, Any]] = {}
        # 关注名单：user_id -> {until, umo, note, at}。命中的消息会带 watch=true
        self._watches: dict[str, dict[str, Any]] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=int(self.cfg["max_events"]))
        self._seq = 0
        self._event_cv = asyncio.Condition()
        self._mcp = None
        self._server = None
        self._server_task: asyncio.Task | None = None
        self._send_tasks: set[asyncio.Task] = set()
        self._token_source = ""
        self.running = False

    # ---------- 生命周期 ----------
    async def initialize(self) -> None:
        if not self.cfg.get("enabled", True):
            logger.info("[qq_mcp] 已在配置中禁用")
            return
        if FastMCP is None or uvicorn is None:
            logger.error(f"[qq_mcp] 缺少 mcp/uvicorn 依赖: {_MCP_IMPORT_ERROR}")
            return
        token = self._read_token()
        if not token:
            logger.error("[qq_mcp] 找不到 token 文件，MCP 不启动")
            return

        mcp = FastMCP("qq")
        mcp.settings.streamable_http_path = str(self.cfg["path"])
        # 无状态模式：AstrBot 重启后 MCP 服务端会话全丢，而 DSH 那边的客户端
        # 还握着旧的 Mcp-Session-Id，之后每次调用都被回「Session not found」，
        # 表现成「机器人有工具但一个都调不动」。无状态时服务端不再校验会话，
        # 重启前后都能直接调。
        mcp.settings.stateless_http = True
        mcp.tool()(self.qq_send)
        mcp.tool()(self.qq_history)
        mcp.tool()(self.qq_poke)
        mcp.tool()(self.qq_recall)
        mcp.tool()(self.qq_status)
        mcp.tool()(self.qq_watch_user)
        mcp.tool()(self.qq_unwatch_user)
        mcp.tool()(self.qq_events)
        mcp.tool()(self.qq_wait_events)
        mcp.tool()(self.qq_call)
        mcp.tool()(self.qq_mute)
        mcp.tool()(self.qq_conversations)
        self._mcp = mcp

        host = str(self.cfg["host"])
        port = int(self.cfg["port"])
        if not _port_free(host, port):
            # 端口已被另一个实例占用：直接跳过，绝不让 uvicorn 的 sys.exit(1)
            # 逃出任务（SystemExit 会把整个 AstrBot 事件循环一起停掉）。
            logger.error(
                f"[qq_mcp] MCP 端口 {port} 已被占用，本次不启动（不影响 AstrBot 主进程）"
            )
            return

        app = _BearerAuth(mcp.streamable_http_app(), token)
        config = uvicorn.Config(
            app,
            host=str(self.cfg["host"]),
            port=int(self.cfg["port"]),
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # 交给 AstrBot 统一收尾
        self._server = server

        async def _serve_guarded() -> None:
            try:
                await server.serve()
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # noqa: BLE001
                # uvicorn 绑定失败会 sys.exit(1)；SystemExit 逃出任务会终止事件循环。
                logger.error(f"[qq_mcp] MCP 服务退出: {type(e).__name__}: {e}")

        self._server_task = asyncio.create_task(_serve_guarded())

        for _ in range(40):
            await asyncio.sleep(0.1)
            if server.started:
                break
        self.running = bool(server.started)
        url = f"http://{self.cfg['host']}:{self.cfg['port']}{self.cfg['path']}"
        if self.running:
            logger.info(f"[qq_mcp] MCP 已启动: {url}（token 来源 {self._token_source}）")
        else:
            logger.error(f"[qq_mcp] MCP 启动失败，端口 {self.cfg['port']} 可能被占用")

    async def terminate(self) -> None:
        for task in list(self._send_tasks):
            task.cancel()
        self._send_tasks.clear()
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._server_task.cancel()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[qq_mcp] 停止 MCP 时出错: {e}")
        logger.info("[qq_mcp] MCP 已停止")

    # ---------- 记录最近会话 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _track(self, event: AstrMessageEvent):
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            return
        group_id = str(event.get_group_id() or "") if hasattr(event, "get_group_id") else ""
        sender_id = str(event.get_sender_id() or "")
        self._recent[umo] = {
            "at": time.time(),
            "bot": getattr(event, "bot", None),
            "sender_id": sender_id,
            "group_id": group_id,
        }
        if len(self._recent) > 50:
            self._prune_recent()

        # 入站事件队列：MCP 客户端按 seq 游标拉取，实现「双向 MCP」
        now_ts = time.time()
        if self._watches:
            for k in [k for k, v in self._watches.items() if v["until"] <= now_ts]:
                self._watches.pop(k, None)
        w = self._watches.get(sender_id)
        watched = bool(w) and (not w["umo"] or w["umo"] == umo)
        self._seq += 1
        message_obj = getattr(event, "message_obj", None)
        group = getattr(message_obj, "group", None)
        self._events.append({
            "seq": self._seq,
            "at": int(now_ts),
            "umo": umo,
            "watch": watched,
            "platform": str(event.get_platform_name() or ""),
            "message_type": "group" if group_id else "private",
            "self_id": str(event.get_self_id() or ""),
            "sender_id": sender_id,
            "sender_name": str(event.get_sender_name() or ""),
            "group_id": group_id,
            "group_name": str(getattr(group, "group_name", "") or "") if group else "",
            "message_id": str(getattr(message_obj, "message_id", "") or ""),
            "text": str(getattr(event, "message_str", "") or ""),
            "segments": self._segments(event),
        })
        async with self._event_cv:
            self._event_cv.notify_all()
        # 由 DSH 侧接管回复时，禁止 AstrBot 默认 LLM 再回一遍
        if bool(self.cfg.get("suppress_default_reply", False)):
            event.call_llm = True

    def _segments(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        """把 AstrBot 消息链转成 OneBot v11 风格的分段数组。"""
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        out: list[dict[str, Any]] = []
        for component in chain:
            try:
                data = component.toDict() if hasattr(component, "toDict") else None
            except Exception:  # noqa: BLE001
                data = None
            if isinstance(data, dict) and data.get("type"):
                out.append(data)
                continue
            out.append({"type": str(getattr(component, "type", "unknown")), "data": {}})
        return out

    def _any_bot(self):
        """取任意一个已连接的 QQ 客户端，供尚未收到入站消息时兜底。"""
        try:
            insts = self.context.platform_manager.get_insts()
        except Exception:  # noqa: BLE001
            return None
        for platform in insts or []:
            bot = getattr(platform, "bot", None)
            if bot is not None and hasattr(bot, "call_action"):
                return bot
        return None

    def _master_ids(self) -> set[str]:
        """主人 = AstrBot 主配置 cmd_config.json 里的 admins_id。"""
        try:
            with open(CMD_CONFIG_PATH, encoding="utf-8-sig") as f:
                raw = json.load(f).get("admins_id") or []
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq_mcp] 读取 admins_id 失败: {type(e).__name__}: {e}")
            return set()
        if isinstance(raw, (str, int)):
            raw = [raw]
        return {str(x).strip() for x in raw if str(x).strip()}

    def _prune_recent(self) -> None:
        ttl = float(self.cfg["recent_ttl_seconds"])
        now = time.time()
        for umo in [k for k, v in self._recent.items() if now - float(v.get("at", 0)) > ttl]:
            self._recent.pop(umo, None)

    def _normalize_conversation(self, conversation: str) -> str:
        text = str(conversation or "").strip().strip("\"'")
        if text.startswith("provider:"):
            text = text[len("provider:"):].strip()
        return text

    def _pick_conversation(self, conversation: str, prefer_sender: str = "") -> tuple[str, dict[str, Any] | None]:
        umo = self._normalize_conversation(conversation)
        if umo:
            return umo, self._recent.get(umo)
        self._prune_recent()
        if not self._recent:
            return "", None
        # dsh 常不传 conversation：全局「最近活跃」可能是别的群，权限会张冠李戴。
        # 有发起人时，优先用他最近一次发言所在的会话。
        sender = str(prefer_sender or "").strip()
        if sender.isdigit():
            for ev in reversed(self._events):
                if str(ev.get("sender_id") or "") != sender:
                    continue
                umo = str(ev.get("umo") or "")
                if umo and umo in self._recent:
                    return umo, self._recent[umo]
        umo, record = max(self._recent.items(), key=lambda kv: kv[1].get("at", 0))
        return umo, record

    # ---------- token / 路径 ----------
    def _read_token(self) -> str:
        for key in ("token_file", "fallback_token_file"):
            path = os.path.expanduser(str(self.cfg.get(key) or ""))
            if not path:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    token = f.read().strip()
                if token:
                    self._token_source = path
                    return token
            except OSError:
                continue
        return ""

    @staticmethod
    def _transcript_path() -> str:
        return os.path.expanduser(DEFAULTS["transcripts_path"])

    # ---------- 拆条 / 节奏 ----------
    @staticmethod
    def _find_cut(text: str, limit: int) -> int:
        enders = "。！？!?…；;，,、\n "
        for i in range(min(limit, len(text)) - 1, max(0, limit // 2), -1):
            if text[i] in enders:
                return i + 1
        return limit

    def _split_bubbles(self, text: str) -> list[str]:
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("||", "\n")
        limit = max(10, int(self.cfg["max_chars_per_bubble"]))
        out: list[str] = []
        for line in raw.split("\n"):
            line = line.strip()
            while len(line) > limit:
                cut = self._find_cut(line, limit)
                piece = line[:cut].strip()
                if piece:
                    out.append(piece)
                line = line[cut:].strip()
            if line:
                out.append(line)
        dedup: list[str] = []
        for item in out:
            if dedup and dedup[-1] == item:
                continue
            dedup.append(item)
        return dedup[: max(1, int(self.cfg["max_bubbles"]))]

    def _typing_delay(self, bubble: str) -> float:
        base = float(self.cfg["typing_base_seconds"]) + float(
            self.cfg["typing_per_char_seconds"]
        ) * len(bubble)
        base *= random.uniform(0.8, 1.25)
        return max(0.2, min(float(self.cfg["max_typing_delay_seconds"]), base))

    async def _send_bubbles(self, umo: str, bubbles: list[str], initial_delay: float) -> str:
        if initial_delay > 0:
            await asyncio.sleep(min(initial_delay, float(self.cfg["max_initial_delay_seconds"])))
        sent = 0
        for index, bubble in enumerate(bubbles):
            if index:
                await asyncio.sleep(self._typing_delay(bubble))
            try:
                await self.context.send_message(umo, MessageChain([Plain(bubble)]))
                sent += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[qq_mcp] 发送失败({umo}): {type(e).__name__}: {e}")
                return f"发送中断，已发 {sent}/{len(bubbles)} 条: {e}"
        logger.info(f"[qq_mcp] 已补发 {sent} 条到 {umo}")
        return f"已发送 {sent} 条到 {umo}"

    # ---------- MCP 工具 ----------
    async def qq_send(self, text: str, conversation: str = "", delay_seconds: float = 0) -> str:
        """补发 QQ 气泡。正文用换行或 || 分隔成多条，条间按字数模拟打字停顿。

        Args:
            text: 要发的内容；换行表示不同气泡
            conversation: 会话 ID（形如 平台:私聊/群聊:ID）；留空使用最近 10 分钟内活跃会话
            delay_seconds: 发送前的等待秒数；补话时填 2~4，让正常回复先到
        """
        umo, _record = self._pick_conversation(conversation)
        if not umo:
            return "没有可用会话：请传 conversation，或等用户先发一条消息"
        bubbles = self._split_bubbles(text)
        if not bubbles:
            return "text 为空，没有发送"

        delay = max(0.0, float(delay_seconds or 0))
        if delay > 0:
            task = asyncio.create_task(self._send_bubbles(umo, bubbles, delay))
            self._send_tasks.add(task)
            task.add_done_callback(self._send_tasks.discard)
            return f"已排队 {len(bubbles)} 条，约 {delay:.1f}s 后发到 {umo}"
        return await self._send_bubbles(umo, bubbles, 0.0)

    async def qq_history(self, conversation: str = "", limit: int = 20) -> str:
        """查看某个 QQ 会话的最近聊天记录（来自 bridge 的 transcripts.json）。

        Args:
            conversation: 会话 ID；留空使用最近活跃会话
            limit: 返回最近多少条，默认 20
        """
        path = self._transcript_path()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return f"还没有聊天记录文件: {path}"
        except Exception as e:  # noqa: BLE001
            return f"读取聊天记录失败: {e}"
        if not isinstance(data, dict) or not data:
            return "聊天记录为空"

        umo, _record = self._pick_conversation(conversation)
        item = None
        if umo:
            item = data.get("provider:" + umo) or data.get(umo)
        if item is None:
            item = max(
                (v for v in data.values() if isinstance(v, dict)),
                key=lambda v: str(v.get("updatedAt", "")),
                default=None,
            )
        if not isinstance(item, dict):
            return "没找到对应会话的聊天记录"

        messages = [m for m in item.get("messages", []) if isinstance(m, dict)]
        count = max(1, min(100, int(limit or 20)))
        lines = [f"会话 {item.get('id', '')} 最近 {min(count, len(messages))} 条："]
        for message in messages[-count:]:
            role = "我" if str(message.get("role")) == "assistant" else (message.get("senderName") or "对方")
            at = str(message.get("at", ""))
            stamp = at[11:16] if len(at) >= 16 else ""
            text = str(message.get("text", "")).replace("\n", " ").strip()
            if len(text) > 220:
                text = text[:220] + "…"
            lines.append(f"[{stamp}] {role}: {text}")
        return "\n".join(lines)

    async def qq_poke(self, conversation: str = "", user_id: str = "") -> str:
        """在 QQ 里戳一戳（拍一拍）。私聊戳好友，群聊戳指定群成员。

        Args:
            conversation: 会话 ID；留空使用最近活跃会话
            user_id: 要戳的 QQ 号；留空表示最近发消息的人
        """
        umo, record = self._pick_conversation(conversation)
        if not umo:
            return "没有可用会话：请传 conversation，或等用户先发一条消息"
        bot = (record or {}).get("bot")
        if bot is None or not hasattr(bot, "call_action"):
            return "拿不到 QQ 客户端句柄（等这个会话再来一条消息后重试）"
        uid = str(user_id or "").strip() or str((record or {}).get("sender_id") or "")
        if not uid.isdigit():
            return "缺少有效的 QQ 号"
        gid = str((record or {}).get("group_id") or "").strip()
        try:
            if gid:
                await bot.call_action(action="group_poke", group_id=int(gid), user_id=int(uid))
                return f"已在群 {gid} 戳了 {uid}"
            await bot.call_action(action="friend_poke", user_id=int(uid))
            return f"已戳了 {uid}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq_mcp] 戳一戳失败: {type(e).__name__}: {e}")
            return f"戳一戳失败: {e}"

    async def qq_recall(self, conversation: str = "", count: int = 1) -> str:
        """撤回我（机器人）在这个 QQ 会话里最近自己发的消息。

        Args:
            conversation: 会话 ID；留空使用最近活跃会话
            count: 撤回最近几条，默认 1，最多 10（QQ 只允许撤 2 分钟内的消息）
        """
        umo, record = self._pick_conversation(conversation)
        if not umo:
            return "没有可用会话：请传 conversation，或等用户先发一条消息"
        bot = (record or {}).get("bot") or self._any_bot()
        if bot is None or not hasattr(bot, "call_action"):
            return "拿不到 QQ 客户端句柄（等这个会话再来一条消息后重试）"
        try:
            login = await bot.call_action(action="get_login_info")
        except Exception as e:  # noqa: BLE001
            return f"获取机器人账号失败: {e}"
        self_id = str((login or {}).get("user_id") or "")
        if not self_id:
            return "拿不到机器人自己的 QQ 号"

        gid = str((record or {}).get("group_id") or "").strip()
        fuid = ""
        if not gid:
            # umo 形如 大肥鱼QQ:FriendMessage:123456789，末段是好友 QQ
            parts = umo.split(":")
            if len(parts) >= 3 and parts[-1].isdigit():
                fuid = parts[-1]
            else:
                return "私聊撤回需要好友 QQ 号，请传带会话 ID 的 conversation"
        try:
            if gid:
                hist = await bot.call_action(
                    action="get_group_msg_history", group_id=int(gid), count=40
                )
            else:
                hist = await bot.call_action(
                    action="get_friend_msg_history", user_id=int(fuid), count=40
                )
        except Exception as e:  # noqa: BLE001
            return f"拉聊天记录失败: {e}"
        msgs = (hist or {}).get("messages") or []
        mine = [
            str(msg.get("message_id"))
            for msg in msgs
            if str((msg.get("sender") or {}).get("user_id") or "") == self_id
        ]
        if not mine:
            return "最近没找到我自己发的消息，没得撤"

        n = max(1, min(10, int(count or 1)))
        ok, fail = 0, 0
        for mid in mine[-n:]:
            try:
                await bot.call_action(action="delete_msg", message_id=int(mid))
                ok += 1
            except Exception:  # noqa: BLE001
                fail += 1
        if not ok:
            return "一条都没撤下来，可能超过 2 分钟了（QQ 只让撤短时间内的）"
        return f"已撤回 {ok} 条" + (f"，{fail} 条撤不动" if fail else "")

    async def qq_watch_user(
        self, user_id: str, minutes: int = 30, conversation: str = "", note: str = ""
    ) -> str:
        """持续关注某个 QQ 用户，他发的消息会标记为 watch=true。

        配合 qq_wait_events(watch_only=true) 使用：只等这个人的消息，
        其他人的消息不会打断等待。适合「帮我盯着某人，他一说话就告诉我」。

        Args:
            user_id: QQ 号；也可以传昵称（按最近收到的消息里匹配）
            minutes: 关注时长（分钟），默认 30，上限 240
            conversation: 只关注这个会话；留空 = 他所在的全部会话
            note: 备注，方便自己区分多个关注对象
        """
        uid = str(user_id or "").strip()
        if not uid:
            return "缺少 user_id"
        if not uid.isdigit():
            hit = next(
                (e for e in reversed(self._events) if e.get("sender_name") == uid),
                None,
            )
            if not hit:
                return f"没找到昵称「{uid}」，请直接传 QQ 号"
            uid = str(hit.get("sender_id") or "")
        mins = max(1, min(240, int(minutes)))
        umo = str(conversation or "").strip()
        self._watches[uid] = {
            "until": time.time() + mins * 60,
            "umo": umo,
            "note": str(note or ""),
            "at": time.time(),
        }
        scope = f"仅 {umo}" if umo else "全部会话"
        return (
            f"已关注 {uid}（{scope}，{mins} 分钟）；"
            "他的消息会在 qq_events / qq_wait_events 里带 watch=true"
        )

    async def qq_unwatch_user(self, user_id: str = "") -> str:
        """取消关注某人；user_id 留空表示清空全部关注。

        Args:
            user_id: 要取消的 QQ 号；留空清空全部
        """
        uid = str(user_id or "").strip()
        if not uid:
            n = len(self._watches)
            self._watches.clear()
            return f"已清空全部关注（{n} 个）"
        if self._watches.pop(uid, None) is None:
            return f"{uid} 不在关注列表里"
        return f"已取消关注 {uid}"

    async def qq_events(
        self, since: int = 0, limit: int = 50, watch_only: bool = False
    ) -> str:
        """拉取自 since 游标之后收到的入站 QQ 消息事件（JSON）。

        返回 {"last_seq": 最新序号, "events": [...]}；事件含 seq / umo /
        watch / message_type / self_id / sender_id / sender_name / group_id /
        group_name / message_id / text / segments / at。把 last_seq 作为下次
        since 传入即可不重不漏。

        Args:
            since: 上次返回的 last_seq；首次传 0
            limit: 一次最多返回多少条，默认 50，上限 200
            watch_only: 只返回被关注的人（qq_watch_user）发的消息
        """
        cap = max(1, min(200, int(limit)))
        items = [
            e for e in list(self._events)
            if e["seq"] > int(since) and (not watch_only or e.get("watch"))
        ]
        page = items[:cap]
        return json.dumps(
            {
                # watch_only 只推进到最后一个命中事件，避免把别人的消息游标吞掉
                "last_seq": page[-1]["seq"] if (watch_only and page) else self._seq,
                "events": page,
            },
            ensure_ascii=False,
        )

    async def qq_wait_events(
        self,
        since: int = 0,
        timeout_seconds: float = 20,
        limit: int = 50,
        watch_only: bool = False,
    ) -> str:
        """长轮询等待新消息事件，最多等 timeout_seconds 秒；超时返回空 events。

        返回结构与 qq_events 相同。适合 DSH 插件做常驻收件循环，比定时轮询省
        CPU，延迟接近实时。watch_only=true 时只等被关注的人的消息，
        其他人的消息不会让等待提前返回。

        Args:
            since: 上次返回的 last_seq；首次传 0
            timeout_seconds: 最长等待秒数，默认 20，上限 60
            limit: 一次最多返回多少条，默认 50
            watch_only: 只返回被关注的人（qq_watch_user）发的消息
        """
        timeout = max(0.0, min(60.0, float(timeout_seconds)))
        deadline = time.monotonic() + timeout
        cap = max(1, min(200, int(limit)))
        items: list[dict[str, Any]] = []
        async with self._event_cv:
            while True:
                items = [
                    e for e in list(self._events)
                    if e["seq"] > int(since)
                    and (not watch_only or e.get("watch"))
                ]
                if items or time.monotonic() >= deadline:
                    break
                try:
                    await asyncio.wait_for(
                        self._event_cv.wait(),
                        timeout=max(0.05, deadline - time.monotonic()),
                    )
                except asyncio.TimeoutError:
                    break
        page = items[:cap]
        return json.dumps(
            {
                "last_seq": page[-1]["seq"] if (watch_only and page) else self._seq,
                "events": page,
            },
            ensure_ascii=False,
        )

    async def qq_call(self, action: str, params: str = "{}", conversation: str = "") -> str:
        """透传 OneBot v11 动作到已登录的 QQ 客户端（如 send_group_msg）。

        参数 params 是 JSON 字符串；conversation 留空则用最近活跃会话对应的
        客户端。返回动作结果（JSON）。

        Args:
            action: OneBot v11 动作名，如 send_group_msg / get_group_list
            params: JSON 对象字符串，如 {"group_id":123,"message":[{"type":"text","data":{"text":"hi"}}]}
            conversation: 会话 ID；留空使用最近活跃会话
        """
        umo, record = self._pick_conversation(conversation)
        bot = (record or {}).get("bot")
        if bot is None or not hasattr(bot, "call_action"):
            bot = self._any_bot()
        if bot is None:
            return "没有可用的 QQ 客户端句柄：请先让目标会话发一条消息，或传 conversation"
        data: dict[str, Any]
        if isinstance(params, dict):
            data = dict(params)
        elif str(params or "").strip():
            try:
                data = json.loads(params)
            except Exception as e:  # noqa: BLE001
                return f"params 不是合法 JSON: {e}"
            if not isinstance(data, dict):
                return "params 必须是 JSON 对象"
        else:
            data = {}
        act = str(action)
        try:
            result = await bot.call_action(action=act, **data)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq_mcp] 调用 {act} 失败: {type(e).__name__}: {e}")
            return f"调用 {act} 失败: {type(e).__name__}: {e}"
        if result is None:
            # OneBot 的写操作成功时多数返回 null。原样回一句 "null"，模型会当成
            # 「没执行」，转头自己编理由（实测：set_group_ban 解禁成功后它回
            # 「解不了，我没那群的权限」）。这里翻译成明确结果，并写回禁言状态。
            gid = str(data.get("group_id") or "")
            uid = str(data.get("user_id") or "")
            if act == "set_group_ban" and gid.isdigit() and uid.isdigit():
                duration = int(data.get("duration") or 0)
                self._sync_human_sim_mute(
                    self._group_umo(umo, gid),
                    0 if duration == 0 else time.time() + duration,
                )
                if duration == 0:
                    return f"已解除 {uid} 在群 {gid} 的禁言"
                return f"已禁言 {uid} {max(1, duration // 60)} 分钟（群 {gid}）"
            return f"{act} 已执行成功（该动作无返回数据）"
        return json.dumps(result, ensure_ascii=False, default=str)

    # ---------- 禁言状态写回 human_sim ----------
    @staticmethod
    def _group_umo(umo: str, group_id: str) -> str:
        """由已有会话 umo 推出同平台的群会话 ID（human_sim 的键格式）。"""
        gid = str(group_id or "").strip()
        if not gid.isdigit():
            return ""
        platform = str(umo or "").split(":", 1)[0].strip()
        if not platform:
            return ""
        return f"{platform}:GroupMessage:{gid}"

    def _sync_human_sim_mute(self, group_umo: str, until: float) -> None:
        """把平台禁言状态写回 human_sim 的 state.json；until<=0 表示解禁。

        两个插件是独立进程里的独立目录，只能通过 state.json 交换状态；
        human_sim 侧按 mtime 热同步（_sync_mutes），改完文件立刻生效。
        """
        if not group_umo:
            return
        key = f"mute_until:{group_umo}"
        try:
            data: dict[str, Any] = {}
            if os.path.exists(HUMAN_SIM_STATE_PATH):
                with open(HUMAN_SIM_STATE_PATH, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            if float(until or 0) > 0:
                data[key] = float(until)
            else:
                data.pop(key, None)
            os.makedirs(os.path.dirname(HUMAN_SIM_STATE_PATH), exist_ok=True)
            with open(HUMAN_SIM_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            logger.info(
                f"[qq_mcp] 写回禁言状态 {key}="
                f"{'解除' if float(until or 0) <= 0 else int(until)}"
            )
        except (OSError, ValueError) as e:  # noqa: BLE001
            logger.warning(f"[qq_mcp] 写回 human_sim 禁言状态失败: {e}")

    async def qq_mute(
        self,
        user_id: str,
        minutes: int = 10,
        requester: str = "",
        conversation: str = "",
        group_id: str = "",
    ) -> str:
        """群里禁言/解禁某个成员，只有主人（AstrBot 管理员）发起才允许。

        Args:
            user_id: 要禁言的 QQ 号
            minutes: 禁言分钟数，0 表示解除禁言，默认 10
            requester: 发起这条指令的人的 QQ 号，必须是在册主人
            conversation: 群会话 ID，留空用最近活跃会话
            group_id: 目标群号；在私聊里发起时填这个（留空则用最近会话所属的群）
        """
        target = str(user_id or "").strip()
        who = str(requester or "").strip()
        umo, record = self._pick_conversation(conversation, prefer_sender=who)
        group = str(group_id or "").strip()
        if group:
            umo = self._group_umo(umo, group) or umo
        bot = (record or {}).get("bot")
        if bot is None or not hasattr(bot, "call_action"):
            bot = self._any_bot()
        if bot is None:
            return "没有可用的 QQ 客户端句柄：先让目标群来一条消息"
        gid = group or str((record or {}).get("group_id") or "")
        if not gid and umo:
            # 显式传了群会话时可能还没有任何入站消息（重启后 _recent 为空），
            # 这时从 umo（平台:群聊/私聊:群号）里解析群号。
            parts = umo.split(":")
            if len(parts) >= 3 and "group" in parts[1].lower() and parts[-1].isdigit():
                gid = parts[-1]
        if not gid:
            return "禁言只能在群聊里用，当前会话不是群；私聊里请把 group_id 一起带上"
        who = who or str((record or {}).get("sender_id") or "")
        if not target.isdigit():
            return "user_id 要填被禁言那个人的 QQ 号"
        if not who.isdigit():
            return "拿不到发起人身份，先在群里说句话再让我禁言"

        async def _role(uid: str) -> str:
            if not uid or not str(uid).isdigit():
                return ""
            try:
                info = await bot.call_action(
                    "get_group_member_info", group_id=int(gid), user_id=int(uid)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[qq_mcp] 查 {uid} 群身份失败: {e}")
                return ""
            return str((info or {}).get("role") or "")

        # aiocqhttp 的 bot.self_id 是 functools.partial（不是号码），直接 str() 会拿到
        # "<functools.partial ...>" 去查群身份，int() 必然报错，于是误判成「不是管理员」。
        # 只认真正的数字 QQ 号，拿不到就现查 get_login_info。
        raw_self = getattr(bot, "self_id", "")
        self_id = str(raw_self) if isinstance(raw_self, (str, int)) and str(raw_self).isdigit() else ""
        if not self_id:
            try:
                login = await bot.call_action(action="get_login_info") or {}
                uid = str(login.get("user_id") or "")
                self_id = uid if uid.isdigit() else ""
            except Exception:  # noqa: BLE001
                self_id = ""
        if not self_id:
            return "拿不到机器人自己的 QQ 号，先确认 NapCat 在线"
        if await _role(self_id) not in ("owner", "admin"):
            return "我在这个群不是管理员，没有禁言权限，先让群主给我个头衔"

        if who not in self._master_ids():
            return "只有主人才能让我禁言别人，其他身份说的不算数"

        who_role = await _role(who)
        target_role = await _role(target)
        if target_role == "owner":
            return "群主不能被禁言"
        if target_role == "admin" and who_role != "owner":
            return "管理员只有群主能禁言，这种情况得群主出面"

        duration = max(0, int(minutes)) * 60
        try:
            await bot.call_action(
                "set_group_ban",
                group_id=int(gid),
                user_id=int(target),
                duration=duration,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq_mcp] 禁言 {target} 失败: {type(e).__name__}: {e}")
            return f"禁言失败: {type(e).__name__}: {e}"
        # 平台侧改完必须写回 human_sim：解禁要清掉 mute_until，禁言要写上到期时间，
        # 否则机器人还会按旧状态继续装哑巴。
        self._sync_human_sim_mute(
            self._group_umo(umo, gid), 0 if duration == 0 else time.time() + duration
        )
        return f"已解除 {target} 的禁言" if duration == 0 else f"已禁言 {target} {int(minutes)} 分钟"

    async def qq_conversations(self, limit: int = 20) -> str:
        """列出最近收到过消息的 QQ 会话（JSON）。

        Args:
            limit: 最多返回多少个会话，默认 20
        """
        self._prune_recent()
        cap = max(1, min(50, int(limit)))
        rows = [
            {
                "umo": umo,
                "group_id": record.get("group_id", ""),
                "sender_id": record.get("sender_id", ""),
                "idle_seconds": int(time.time() - float(record.get("at", 0))),
            }
            for umo, record in sorted(
                self._recent.items(), key=lambda kv: kv[1].get("at", 0), reverse=True
            )[:cap]
        ]
        return json.dumps({"conversations": rows}, ensure_ascii=False)

    async def qq_status(self) -> str:
        """查看 MCP 服务状态、最近会话和聊天记录文件情况。"""
        self._prune_recent()
        lines = [
            f"运行中: {'是' if self.running else '否'}",
            f"地址: http://{self.cfg['host']}:{self.cfg['port']}{self.cfg['path']}",
            f"token 来源: {self._token_source or '未找到'}",
            f"待发送任务: {len(self._send_tasks)}",
            f"入站事件: {len(self._events)} 条 (last_seq {self._seq}, suppress={bool(self.cfg.get('suppress_default_reply', False))})",
        ]
        if self._recent:
            lines.append("最近会话:")
            for umo, record in sorted(
                self._recent.items(), key=lambda kv: kv[1].get("at", 0), reverse=True
            )[:5]:
                idle = int(time.time() - float(record.get("at", 0)))
                group = f" 群{record.get('group_id')}" if record.get("group_id") else ""
                bot = "有" if record.get("bot") is not None else "无"
                lines.append(f"- {umo}{group} | {idle}s 前 | 客户端{bot}")
        else:
            lines.append("最近会话: 无（等用户先发一条消息）")

        path = self._transcript_path()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            lines.append(f"聊天记录: {len(data)} 个会话 ({path})")
        except Exception:  # noqa: BLE001
            lines.append(f"聊天记录: 尚未生成 ({path})")
        return "\n".join(lines)

    # ---------- AstrBot 原生工具 ----------
    # 上面那批 qq_* 是 MCP 工具，只有 DSH 外部 agent 能调；AstrBot 直连的模型
    # （DeepSeek / 智谱等）走原生 function calling，这里把同一批能力补成 llm_tool，
    # 换模型不用换能力。

    def _tool_umo(
        self, event: AstrMessageEvent, conversation: str = ""
    ) -> tuple[str, dict[str, Any]]:
        umo, record = self._pick_conversation(
            conversation or str(getattr(event, "unified_msg_origin", "") or ""),
            prefer_sender=str(event.get_sender_id() or ""),
        )
        return umo, record or {}

    def _tool_is_master(self, event: AstrMessageEvent) -> bool:
        """当前说话人是不是在册主人（跨会话操作只给主人用）。"""
        return str(event.get_sender_id() or "") in self._master_ids()

    @filter.llm_tool(name="qq_history")
    async def tool_qq_history(
        self, event: AstrMessageEvent, conversation: str = "", limit: int = 20
    ):
        """查看某个 QQ 会话最近的聊天记录，也能看别的会话。

        Args:
            conversation(string): 会话 ID，形如 大肥鱼QQ:FriendMessage:123456；留空＝当前会话
            limit(number): 最近多少条，默认 20
        """
        umo, _ = self._tool_umo(event, conversation)
        if not umo:
            return "没有可用会话"
        current = str(getattr(event, "unified_msg_origin", "") or "")
        if umo != current and not self._tool_is_master(event):
            return "只有主人才能看别的会话的聊天记录"
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return "会话记忆不可用"
        try:
            cid = await cm.get_curr_conversation_id(umo)
            conv = await cm.get_conversation(umo, cid) if cid else None
            history = json.loads(getattr(conv, "history", None) or "[]") if conv else []
        except Exception as e:  # noqa: BLE001
            return f"读取聊天记录失败: {type(e).__name__}: {e}"
        msgs = [
            m
            for m in history
            if isinstance(m, dict) and str(m.get("content") or "").strip()
        ]
        if not msgs:
            return f"{umo} 还没有聊天记录"
        n = max(1, min(100, int(limit or 20)))
        lines = [f"{umo} 最近 {min(n, len(msgs))} 条："]
        for m in msgs[-n:]:
            role = "我" if str(m.get("role")) == "assistant" else "对方"
            text = str(m.get("content") or "").replace("\n", " ").strip()
            lines.append(f"{role}: {text[:220]}")
        return "\n".join(lines)

    @filter.llm_tool(name="qq_recall")
    async def tool_qq_recall(self, event: AstrMessageEvent, count: int = 1):
        """撤回我最近在这个会话里发的消息（QQ 只允许撤 2 分钟内的）。

        Args:
            count(number): 撤几条，默认 1，最多 10
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return await self.qq_recall(conversation=umo, count=count)

    @filter.llm_tool(name="qq_conversations")
    async def tool_qq_conversations(self, event: AstrMessageEvent, limit: int = 20):
        """列出最近有消息的 QQ 会话（想知道还有哪些群/好友在聊时用）。

        Args:
            limit(number): 最多几个，默认 20
        """
        if not self._tool_is_master(event):
            return "只有主人才能列会话列表"
        return await self.qq_conversations(limit=limit)

    @filter.llm_tool(name="qq_mute")
    async def tool_qq_mute(
        self,
        event: AstrMessageEvent,
        user_id: str,
        minutes: int = 10,
        group_id: str = "",
    ):
        """在群里禁言/解禁成员（只有主人能让我做，且我必须是群管理员）。

        Args:
            user_id(string): 要禁言的 QQ 号
            minutes(number): 禁言分钟数，0＝解除禁言，默认 10
            group_id(string): 目标群号；在私聊里操作时必须填，群聊里可省略
        """
        return await self.qq_mute(
            user_id=user_id,
            minutes=minutes,
            requester=str(event.get_sender_id() or ""),
            conversation=str(getattr(event, "unified_msg_origin", "") or ""),
            group_id=str(group_id or ""),
        )

    @filter.llm_tool(name="qq_call")
    async def tool_qq_call(
        self, event: AstrMessageEvent, action: str, params: str = "{}"
    ):
        """透传任意 OneBot v11 动作（高级用法，如 get_group_info、get_group_list）。

        Args:
            action(string): 动作名，如 send_group_msg / get_group_info
            params(string): JSON 对象字符串参数，如 {"group_id":123}
        """
        if not self._tool_is_master(event):
            return "只有主人才能直接调用 QQ 接口"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return await self.qq_call(action=action, params=params, conversation=umo)
