"""把 QQ / 微信机器人状态同步到官方大肥鱼 3D 桌宠。

数据流：
    AstrBot 消息事件 -> 本插件 -> pet-relay.js (127.0.0.1:23334)
        -> 本地 9B 生成一句旁白 -> DSH dsh-dafeiyu /plugins/dsh-dafeiyu/inject
        -> 官方原生桌宠显示动作与气泡

边界：桌宠只做状态面板，不读取也不共享 QQ / 微信机器人的人格、记忆和上下文。
"""

import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register

# 协议端有时把图片落成本地临时文件（NapCat / AstrBot 媒体下载）：
# 复制到 relay 能读取的固定目录，气泡就能显示本地图片而不只是「发来一张图片」。
try:
    PET_IMAGE_DIR = Path(__file__).resolve().parents[5] / "bots" / "pet-images"
    PET_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # 目录结构不同就退化为只支持 http 图片
    PET_IMAGE_DIR = None


@register(
    "dafeiyu_pet",
    "dafeiyu",
    "把 QQ / 微信机器人状态同步到官方大肥鱼 3D 桌宠",
    "0.1.0",
    "dafeiyu",
)
class DafeiyuPetPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        cfg = config or {}
        self.relay_url = str(
            cfg.get("relay_url") or "http://127.0.0.1:23334/state"
        )
        self.enabled = bool(cfg.get("enabled", True))
        self.notify = {
            "qq": bool(cfg.get("notify_qq", True)),
            "wechat": bool(cfg.get("notify_wechat", True)),
        }
        self.min_interval = float(cfg.get("min_interval", 1.5))
        # 微信头像由 io-wechat 服务从 head_image.db 解密提供（GET /avatar?id=）
        self.io_wechat_url = str(
            cfg.get("io_wechat_url") or "http://127.0.0.1:23392"
        ).rstrip("/")
        # 机器人自己发出的消息在桌宠上显示成谁：on_decorating_result 的 event 仍是用户消息事件，
        # 不能沿用对方的昵称/头像，这里单独给一个「本机」身份。
        self.self_name = str(cfg.get("self_name") or "肥鱼").strip() or "肥鱼"
        # 「开始思考」只是背景提示，同一会话里不需要每条消息都播，给个更长的节流；
        # 真正值得播的是「回复已生成」（working）。
        self.thinking_interval = float(cfg.get("thinking_interval_seconds", 30))
        # 桌宠不在线时 relay 要花几秒生成旁白才返回 502，别让它拖慢消息流水线；
        # 连续失败就冷却一段时间，期间不再推送。
        self.fail_cooldown = float(cfg.get("fail_cooldown_seconds", 60))
        self._last_push: dict[str, float] = {}
        self._cooldown_until = 0.0
        # 每个会话一条推送任务：不同群/私聊互不阻塞，也不会互相覆盖
        self._tasks: dict[str, asyncio.Task] = {}
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=8.0)
        return self._client

    @staticmethod
    def _source(event: AstrMessageEvent) -> str | None:
        platform = str(event.get_platform_name() or "").lower()
        if any(key in platform for key in ("weixin", "wechat", "wcf", "gewe")):
            return "wechat"
        if any(key in platform for key in ("aiocqhttp", "onebot", "qq")):
            return "qq"
        return None

    def _identity(self, event: AstrMessageEvent, is_self: bool = False) -> dict:
        """提取发言人/会话身份；机器人自己的消息强制归到本机，绝不复用对方的昵称/头像。"""
        sender_name = str(event.get_sender_name() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        if self_id and sender_id == self_id:
            is_self = True
        group_id = str(event.get_group_id() or "").strip()
        group = getattr(getattr(event, "message_obj", None), "group", None)
        group_name = str(getattr(group, "group_name", "") or "").strip() if group else ""
        chat_type = "group" if group_id else "private"
        if chat_type == "group":
            chat_name = group_name or f"群 {group_id}"
        else:
            chat_name = "私聊"
        if is_self:
            sender_id = self_id or sender_id
            sender_name = self.self_name
        # 发言人头像：QQ 用公开头像地址；微信走 io-wechat 的 /avatar（head_image.db 解密），
        # 查不到时 relay/渲染端会退回纯文字气泡。
        sender_avatar = ""
        src = self._source(event)
        if sender_id and src == "qq":
            sender_avatar = f"https://q1.qlogo.cn/g?b=qq&nk={sender_id}&s=100"
        elif sender_id and src == "wechat":
            sender_avatar = (
                f"{self.io_wechat_url}/avatar?id={quote(sender_id, safe='')}"
            )
        return {
            "sender_name": sender_name,
            "sender_id": sender_id,
            "sender_avatar": sender_avatar,
            "is_self": is_self,
            "group_id": group_id,
            "group_name": group_name,
            "chat_type": chat_type,
            "chat_name": chat_name,
        }

    @staticmethod
    def _local_path(value: str) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        if value.startswith("file://"):
            path = unquote(urlparse(value).path)
            if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
                path = path[1:]
            value = path
        if os.path.isabs(value) and os.path.isfile(value):
            return value
        return ""

    @staticmethod
    def _cache_local(src: str) -> str:
        """把本地图片复制到 relay 的固定目录，返回文件名（relay 会转成 http 地址）。"""
        if PET_IMAGE_DIR is None:
            return ""
        suffix = Path(src).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            suffix = ".img"
        name = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}{suffix}"
        try:
            # 大图/长动图不往桌宠灌：超过 20MB 直接退回纯文字，避免复制和渲染把桌宠拖住
            if os.path.getsize(src) > 20 * 1024 * 1024:
                return ""
            # 目录可能被清理脚本删掉，复制前自愈重建，避免图片推送整体失效
            PET_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, PET_IMAGE_DIR / name)
            return name
        except Exception:
            return ""

    @classmethod
    def _images(cls, event: AstrMessageEvent) -> list[str]:
        """提取消息里的图片：优先网络地址，本地临时文件则复制给 relay 托管。"""
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        refs: list[str] = []
        for seg in chain:
            if not isinstance(seg, Image):
                continue
            ref = ""
            for attr in ("url", "file", "path"):
                value = str(getattr(seg, attr, "") or "").strip()
                if value.startswith("http://") or value.startswith("https://"):
                    ref = value
                    break
            if not ref:
                for attr in ("path", "file", "url"):
                    name = cls._cache_local(cls._local_path(getattr(seg, attr, "")))
                    if name:
                        ref = f"local:{name}"
                        break
            if ref and ref not in refs:
                refs.append(ref)
            if len(refs) >= 3:
                break
        return refs

    async def _push(
        self,
        event: AstrMessageEvent,
        state: str,
        what: str,
        detail: str,
        message: str | None = None,
        images: list[str] | None = None,
        is_self: bool = False,
    ) -> bool:
        if not self.enabled:
            return False
        source = self._source(event)
        if not source or not self.notify.get(source, True):
            return False

        ident = self._identity(event, is_self)
        # 以「会话」为单位节流：群聊按群、私聊按对方，避免每个发言人都推一条
        chat_key = f"{source}:{ident['group_id'] or ident['sender_id']}"
        if is_self:
            # 自身发言（回复/字幕）用独立节流钟：刚推过「正在看消息」这类来话提示，
            # 不该把几秒后就出来的回复字幕一起挡在节流门外（这就是「发言有时不显示」的主因）。
            chat_key += ":self"

        now = time.monotonic()
        if now < self._cooldown_until:
            return False
        last = self._last_push.get(chat_key, 0.0)
        # 回复生成是重点，按 min_interval 轻节流；「开始思考」按更长间隔节流，
        # 避免每个消息都往桌宠灌一条。
        gap = self.min_interval if state == "working" else self.thinking_interval
        if now - last < gap:
            return False
        self._last_push[chat_key] = now

        payload = {
            "source": source,
            "state": state,
            "event": what,
            "detail": detail,
            **ident,
        }
        if images:
            payload["images"] = images[:3]
        if message:
            payload["message"] = message

        try:
            client = await self._ensure_client()
            resp = await client.post(self.relay_url, json=payload)
            if resp.status_code >= 400:
                self._cooldown_until = time.monotonic() + self.fail_cooldown
                logger.debug(
                    f"[dafeiyu_pet] relay {resp.status_code}: {resp.text[:200]}"
                )
                return False
            return True
        except Exception as exc:  # 桌宠不在线不影响机器人本身
            self._cooldown_until = time.monotonic() + self.fail_cooldown
            logger.debug(f"[dafeiyu_pet] relay unreachable: {exc}")
            return False

    def _schedule(
        self,
        event: AstrMessageEvent,
        state: str,
        what: str,
        detail: str,
        message: str | None = None,
        images: list[str] | None = None,
        is_self: bool = False,
    ) -> None:
        """把推送丢到后台跑：桌宠只是状态面板，绝不能在消息链条路上 await。"""
        if not self.enabled:
            return
        ident = self._identity(event, is_self)
        source = self._source(event) or ""
        chat_key = f"{source}:{ident['group_id'] or ident['sender_id']}"
        for key, task in list(self._tasks.items()):
            if task.done():
                del self._tasks[key]
        current = self._tasks.get(chat_key)
        if current is not None and not current.done():
            return  # 同一个会话上一条还在推，别堆积
        self._tasks[chat_key] = asyncio.create_task(
            self._push(event, state, what, detail, message, images, is_self)
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        # 不截断：桌宠气泡要显示完整正文；语音侧会自己截到 60 字
        text = (event.message_str or "").strip()
        images = self._images(event)
        detail = text or ("发来一张图片" if images else "收到一条消息")
        self._schedule(
            event,
            "thinking",
            "收到新消息，开始思考",
            detail,
            images=images,
        )

    @filter.on_decorating_result()
    async def on_reply(self, event: AstrMessageEvent) -> None:
        try:
            result = event.get_result()
            reply = (result.get_plain_text() if result else "").strip()
        except Exception:
            reply = ""
        # 语音/唱歌是直发的，文字链已被 basic_tools 清空：拿实际念出的文本兜底，
        # 否则桌宠只会显示笼统的「正在回复」，听得到语音却看不到对应字幕。
        if not reply:
            reply = str(
                getattr(event, "_fy_voice_text", "")
                or getattr(event, "_fy_sing_text", "")
                or ""
            ).strip()
        self._schedule(
            event,
            "working",
            "回复已生成，准备发出",
            reply if reply else "正在回复",
            is_self=True,
        )

    async def terminate(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
