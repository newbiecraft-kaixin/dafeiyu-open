"""主动关心：低频、限额、只在冷场时给主人发一句话。

防打扰 + 控成本：
- 只发主人私聊（umo 精确匹配）
- 距上次对话 >= min_idle_minutes 才发；距上次主动消息也要隔开
- 只在 start_hour ~ end_hour 之间发
- 每天最多 daily_cap 条，计数落盘，重启不清零
- 默认用本地 Ollama 模型（零 token 费用），失败可回退到主模型
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import uuid
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))  # .../data
DATA_DIR = os.path.join(DATA_ROOT, "plugin_data", "astrbot_plugin_care_ping")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
STICKER_ROOT = os.path.join(DATA_ROOT, "stickers")
STICKER_MAP = os.path.join(STICKER_ROOT, "map.txt")

DEFAULTS = {
    "master_umo": "大肥鱼QQ:FriendMessage:1000000000",
    "provider_id": "ollama-chat/qwen3.5-9b",
    "min_idle_minutes": 180,
    "start_hour": 8,
    "end_hour": 22,
    "daily_cap": 4,
    "check_interval_minutes": 10,
    "sticker_probability": 0.35,
    "max_chars": 40,
    "enabled": True,
}

# 夜间硬禁：不论 config.json 怎么写，22:00–08:00 一律不主动开口。
NIGHT_START = 22
NIGHT_END = 8


SYSTEM_PROMPT = (
    "你是主人的女仆「大肥鱼」，蓝色鲸鱼女孩。说话短、口语，偶尔嘴硬，自称我或肥鱼，称对方为主人。"
    "不用颜文字，不刷 emoji，不解释自己在做什么。现在你要主动发一条消息给主人。"
)


@register(
    "astrbot_plugin_care_ping",
    "dafeiyu-local",
    "主动关心：冷场时低频发一条消息，带每日上限",
    "v1.0.0",
)
class CarePing(Star):
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
                logger.warning(f"[care_ping] 读取 config.json 失败: {e}")

        self.state = {
            "last_activity": 0.0,
            "last_text": "",
            "last_ping": 0.0,
            "day": "",
            "count": 0,
        }
        self._load_state()
        self._task: asyncio.Task | None = None
        self._stickers = self._load_stickers()

    # ---------- 状态 ----------
    def _load_state(self) -> None:
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.state.update({k: data[k] for k in data if k in self.state})
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"[care_ping] 状态保存失败: {e}")

    def _load_stickers(self) -> list[str]:
        out: list[str] = []
        try:
            with open(STICKER_MAP, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(":")
                    if len(parts) < 2:
                        continue
                    rel = parts[-1]
                    path = rel if os.path.isabs(rel) else os.path.join(STICKER_ROOT, rel)
                    if os.path.exists(path):
                        out.append(path)
        except OSError:
            pass
        return out

    # ---------- 生命周期 ----------
    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[care_ping] 已启动：主人={self.cfg['master_umo']}，"
            f"provider={self.cfg['provider_id'] or '默认'}，"
            f"冷场≥{self.cfg['min_idle_minutes']}分钟，"
            f"{self.cfg['start_hour']}:00-{self.cfg['end_hour']}:00，"
            f"每天≤{self.cfg['daily_cap']}条，表情包概率={self.cfg['sticker_probability']}，"
            f"可选表情包 {len(self._stickers)} 张"
        )

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    # ---------- 记录活跃 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _track(self, event: AstrMessageEvent):
        if event.unified_msg_origin != self.cfg["master_umo"]:
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        self.state["last_activity"] = datetime.now().timestamp()
        self.state["last_text"] = text[:120]
        self._save_state()

    # ---------- 定时检查 ----------
    async def _loop(self) -> None:
        while True:
            base = max(1, int(self.cfg["check_interval_minutes"]))
            await asyncio.sleep(base * 60 * random.uniform(0.8, 1.2))
            try:
                await self._maybe_ping()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[care_ping] 检查失败: {type(e).__name__}: {e}")

    def _roll_day(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state["day"] != today:
            self.state["day"] = today
            self.state["count"] = 0
            self._save_state()

    async def _maybe_ping(self) -> None:
        if not self.cfg["enabled"]:
            return
        now = datetime.now()
        self._roll_day()
        if now.hour >= NIGHT_START or now.hour < NIGHT_END:
            return
        if not (int(self.cfg["start_hour"]) <= now.hour < int(self.cfg["end_hour"])):
            return

        if int(self.state["count"]) >= int(self.cfg["daily_cap"]):
            return
        gap = int(self.cfg["min_idle_minutes"]) * 60
        # 阈值按时间戳抖动 0.7~1.3 倍：同一个冷场窗口内算出的阈值恒定，
        # 不同窗口不同，不会每次都卡在上次说话后整 3 小时。
        idle_gap = gap * random.Random(int(float(self.state["last_activity"]))).uniform(0.7, 1.3)
        if now.timestamp() - float(self.state["last_activity"]) < idle_gap:
            return
        ping_gap = gap * random.Random(int(float(self.state["last_ping"]))).uniform(0.7, 1.3)
        if now.timestamp() - float(self.state["last_ping"]) < ping_gap:
            return
        await self._send_ping()

    def _provider(self):
        pid = (self.cfg.get("provider_id") or "").strip()
        if pid:
            provider = self.context.get_provider_by_id(pid)
            if provider:
                return provider
            logger.warning(f"[care_ping] 找不到 provider {pid}，回退默认模型")
        return self.context.get_using_provider()

    async def _generate(self) -> str:
        now = datetime.now()
        idle_h = max(
            1, int((now.timestamp() - float(self.state["last_activity"])) // 3600)
        )
        last = (self.state["last_text"] or "").strip()
        prompt = (
            f"现在是 {now:%m月%d日 %H:%M}。主人已经 {idle_h} 小时没跟你说话了"
            + (f"，他最后说的是：「{last}」" if last else "")
            + "。用一句话（不超过25字）主动关心他，可以提吃饭、休息、天气或刚才聊的事。"
            "不要问「在吗」，不要解释你在做什么。"
        )
        provider = self._provider()
        if not provider:
            raise RuntimeError("没有可用的对话 provider")
        resp = await provider.text_chat(
            prompt=prompt,
            session_id=uuid.uuid4().hex,
            contexts=[],
            system_prompt=SYSTEM_PROMPT,
            persist=False,
        )
        text = (resp.completion_text or "").strip()
        limit = int(self.cfg["max_chars"])
        if len(text) > limit:
            text = text[:limit].rstrip("，,、 　")
        return text

    async def _send_ping(self) -> bool:
        now = datetime.now()
        if now.hour >= NIGHT_START or now.hour < NIGHT_END:
            logger.info("[care_ping] 夜间禁发主动消息，跳过")
            return False
        text = await self._generate()

        if not text:
            logger.warning("[care_ping] 生成为空，跳过")
            return False
        chain = [Plain(text)]
        if self._stickers and random.random() < float(self.cfg["sticker_probability"]):
            chain.append(Image.fromFileSystem(random.choice(self._stickers)))
        await self.context.send_message(self.cfg["master_umo"], MessageChain(chain))
        self.state["last_ping"] = datetime.now().timestamp()
        self.state["count"] = int(self.state["count"]) + 1
        self._save_state()
        logger.info(f"[care_ping] 已发送（今日第 {self.state['count']} 条）: {text[:40]}")
        return True

