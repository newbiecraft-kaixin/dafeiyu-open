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
        mcp.tool()(self.qq_send)
        mcp.tool()(self.qq_history)
        mcp.tool()(self.qq_poke)
        mcp.tool()(self.qq_status)
        mcp.tool()(self.qq_events)
        mcp.tool()(self.qq_wait_events)
        mcp.tool()(self.qq_call)
        mcp.tool()(self.qq_conversations)
        self._mcp = mcp

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
        self._server_task = asyncio.create_task(server.serve())

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
        self._seq += 1
        message_obj = getattr(event, "message_obj", None)
        group = getattr(message_obj, "group", None)
        self._events.append({
            "seq": self._seq,
            "at": int(time.time()),
            "umo": umo,
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

    def _pick_conversation(self, conversation: str) -> tuple[str, dict[str, Any] | None]:
        umo = self._normalize_conversation(conversation)
        if umo:
            return umo, self._recent.get(umo)
        self._prune_recent()
        if not self._recent:
            return "", None
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

    async def qq_events(self, since: int = 0, limit: int = 50) -> str:
        """拉取自 since 游标之后收到的入站 QQ 消息事件（JSON）。

        返回 {"last_seq": 最新序号, "events": [...]}；事件含 seq / umo /
        message_type / self_id / sender_id / sender_name / group_id /
        group_name / message_id / text / segments / at。把 last_seq 作为下次
        since 传入即可不重不漏。

        Args:
            since: 上次返回的 last_seq；首次传 0
            limit: 一次最多返回多少条，默认 50，上限 200
        """
        cap = max(1, min(200, int(limit)))
        items = [e for e in list(self._events) if e["seq"] > int(since)]
        return json.dumps(
            {"last_seq": self._seq, "events": items[:cap]}, ensure_ascii=False
        )

    async def qq_wait_events(
        self, since: int = 0, timeout_seconds: float = 20, limit: int = 50
    ) -> str:
        """长轮询等待新消息事件，最多等 timeout_seconds 秒；超时返回空 events。

        返回结构与 qq_events 相同。适合 DSH 插件做常驻收件循环，比定时轮询省
        CPU，延迟接近实时。

        Args:
            since: 上次返回的 last_seq；首次传 0
            timeout_seconds: 最长等待秒数，默认 20，上限 60
            limit: 一次最多返回多少条，默认 50
        """
        timeout = max(0.0, min(60.0, float(timeout_seconds)))
        deadline = time.monotonic() + timeout
        cap = max(1, min(200, int(limit)))
        items: list[dict[str, Any]] = []
        async with self._event_cv:
            while True:
                items = [e for e in list(self._events) if e["seq"] > int(since)]
                if items or time.monotonic() >= deadline:
                    break
                try:
                    await asyncio.wait_for(
                        self._event_cv.wait(),
                        timeout=max(0.05, deadline - time.monotonic()),
                    )
                except asyncio.TimeoutError:
                    break
        return json.dumps(
            {"last_seq": self._seq, "events": items[:cap]}, ensure_ascii=False
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
        try:
            result = await bot.call_action(action=str(action), **data)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq_mcp] 调用 {action} 失败: {type(e).__name__}: {e}")
            return f"调用 {action} 失败: {type(e).__name__}: {e}"
        return json.dumps(result, ensure_ascii=False, default=str)

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
