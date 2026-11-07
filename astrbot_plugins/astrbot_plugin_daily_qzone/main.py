"""每日一篇 QQ 空间动态。

- 每天按 6 / 12 / 18 点三档发说说（每档带随机分钟抖动，避免准点连发触发风控）
- 内容优先「当日群聊总结」：从 livingmemory 消息库里捞今天的聊天记录，提炼成大肥鱼口吻的总结
  （没有当天消息时退回主题池随机）
- 概率给说说配一张表情包图（本地表情库随机抽，gif/webp 自动转 PNG）
- 每档只发一篇，状态落盘，重启不重复发；错过超过 2 小时的档直接放弃，不集中补发
"""

from __future__ import annotations

import aiohttp
import asyncio
import base64
import io
import json
import os
import random
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))  # .../data
STATE_PATH = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_daily_qzone", "state.json"
)
LM_DB = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_livingmemory", "conversations.db"
)
STICKER_ROOT = os.path.join(DATA_ROOT, "stickers")
STICKER_MAP = os.path.join(STICKER_ROOT, "map.txt")

# 自赞走 Qzone 网页版 dolike 接口（和 qzone_channel 的点赞同源），凭据从 NapCat 拿
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
LIKE_URL = (
    "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com"
    "/cgi-bin/likes/internal_dolike_app"
)

try:  # gif/webp 转 PNG 用
    from PIL import Image as _PILImage
except ImportError:  # noqa: BLE001
    _PILImage = None

DEFAULTS = {
    "post_hours": [6, 12, 18],  # 每天这三档各发一条
    "jitter_minutes": 45,       # 每档在整点后 0~45 分钟内随机发，避免准点三连
    "catchup_minutes": 120,     # 错过超过这么久就放弃这一档，不集中补发
    "latest_hour": 23,          # 过点后到这个点之前仍可补发
    "check_interval_minutes": 10,
    "max_chars": 400,
    "prefer_provider_id": "deepseek/deepseek-flash",  # 写说说用官方 API，质量比本地小模型高
    "llm_retry": 2,             # LLM 返回空/异常时的重试次数
    "ugc_right": 1,  # 1=公开（NapCat 语义，私有改 4）
    "daily_summary": True,      # True=优先用当天群聊记录生成当日总结
    "summary_max_msgs": 160,    # 总结最多参考的当天消息条数
    "summary_max_chars": 4000,  # 喂给 LLM 的聊天记录总字数上限
    "sticker_probability": 0.8, # 说说配表情包图的概率
    "self_like": True,          # 发完说说隔一会儿给自己点个赞
    "self_like_delay": 60,      # 自赞延迟秒数（±50% 抖动，像顺手刷到自己这条）
    "themes": [
        "今天遇到的烦心事或开心事",
        "吐槽最近玩的游戏或看的番",
        "深夜感慨人生一言",
        "分享一个冷知识或吐槽天气",
        "随口一句牢骚，比如工作学习太累",
    ],
    "enabled": True,
}

SYSTEM_PROMPT = (
    "你是一个 QQ 群里的真人用户「大肥鱼」，现在要发一条 QQ 空间说说。"
    "要求：口语化、真实感、像随手打的，150~400 字，最多不超过 400 字。"
    "禁止：话题标签(#)、@任何人、颜文字堆砌、心灵鸡汤式套话、自我介绍。"
    "只输出说说正文本身，不要任何解释或引号。"
)

SUMMARY_PROMPT = (
    "你是一个 QQ 群里的真人用户「大肥鱼」，下面是你今天在群里看到的聊天记录。"
    "请基于这些内容写一条「今日总结」发到 QQ 空间：像真人随手写的复盘，可以吐槽、可以玩梗、"
    "可以只挑一两个最有意思的点，200~400 字，最多不超过 400 字。"
    "硬性要求：绝不出现任何人的昵称、QQ号、真实姓名；不逐条罗列；不出现「聊天记录」「群友说」这类字眼；"
    "禁止话题标签(#)、@任何人。"
    "只输出说说正文本身，不要任何解释或引号。"
)

_PLACEHOLDER_RE = re.compile(r":{0,1}[:：][^:：\n]{1,12}[:：]\.{0,1}|\[[^\[\]\n]{1,12}\]")


@register(
    "astrbot_plugin_daily_qzone",
    "dafeiyu-local",
    "每日自动发一篇 QQ 空间动态（LLM 生成 + NapCat send_qzone_msg）",
    "v1.0.0",
)
class DailyQzone(Star):
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
                logger.warning(f"[daily_qzone] 读取 config.json 失败: {e}")

        self._bot = None  # aiocqhttp CQHttp 客户端，从任意消息事件里抓
        self.state: dict = {"day": "", "tid": "", "posted_slots": {}, "tids": []}
        self._load_state()
        # 兼容老 state：只有最近一条 tid 时补进历史
        if self.state.get("tid") and not self.state.get("tids"):
            self.state["tids"] = [{
                "tid": self.state["tid"],
                "ts": 0,
                "text": str(self.state.get("last_text") or "")[:60],
                "image": bool(self.state.get("last_with_image")),
            }]
        self._task: asyncio.Task | None = None
        self._bg_tasks: set = set()  # 自赞等后台任务强引用，避免被 GC 打断
        self._lock = asyncio.Lock()  # 发帖串行化：巡查循环 / 手动命令 / 补发互斥
        self._hours = self._parse_hours()

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
            logger.warning(f"[daily_qzone] 状态保存失败: {e}")

    def _parse_hours(self) -> list[int]:
        """解析发帖档位；兼容老配置里的单个 post_hour。"""
        raw = self.cfg.get("post_hours")
        if not raw and self.cfg.get("post_hour") is not None:
            raw = [self.cfg.get("post_hour")]
        hours: list[int] = []
        for h in raw or []:
            try:
                n = int(h)
            except (TypeError, ValueError):
                continue
            if 0 <= n <= 23 and n not in hours:
                hours.append(n)
        return sorted(hours) or [6, 12, 18]

    def _posted_slots(self, day: str | None = None) -> list[int]:
        """某天已经发过的档位（记录在 state["posted_slots"]）。"""
        day = day or datetime.now().strftime("%Y-%m-%d")
        slots = self.state.setdefault("posted_slots", {})
        got = slots.get(day) or []
        return [int(h) for h in got if str(h).isdigit()]

    def _mark_slot(self, hour: int | None, day: str | None = None) -> None:
        if hour is None:
            return
        day = day or datetime.now().strftime("%Y-%m-%d")
        got = self.state.setdefault("posted_slots", {}).setdefault(day, [])
        if hour not in got:
            got.append(int(hour))
        # 只留最近两天的记录，别让 state 无限长
        for k in list(self.state["posted_slots"].keys()):
            if k != day and len(self.state["posted_slots"]) > 2:
                self.state["posted_slots"].pop(k, None)

    def _slot_time(self, day: str, hour: int) -> datetime:
        """这一档实际发帖时刻：整点 + 当天固定随机抖动（同一天同一档只抖一次）。"""
        jitter = random.Random(f"{day}-{hour}").uniform(
            0, max(0, float(self.cfg.get("jitter_minutes", 45)))
        )
        return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour) + timedelta(
            minutes=jitter
        )

    # ---------- 生命周期 ----------
    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())
        hours_txt = " / ".join(str(h) for h in self._hours)
        logger.info(
            f"[daily_qzone] 已启动：每天 {hours_txt} 点档各发一条空间动态"
            f"（每档整点后 0~{self.cfg.get('jitter_minutes', 45)} 分钟随机发，"
            f"错过超过 {self.cfg.get('catchup_minutes', 120)} 分钟直接跳过），"
            f"内容={'当日群聊总结' if self.cfg.get('daily_summary') else '主题池'}，"
            f"配图概率={self.cfg.get('sticker_probability', 0.8)}，"
            f"生成模型={self.cfg.get('prefer_provider_id') or '默认'}，"
            f"今日已发档位={self._posted_slots() or '无'}"
        )

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    # ---------- 抓 bot 客户端 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _grab_bot(self, event: AstrMessageEvent):
        if self._bot is None:
            bot = getattr(event, "bot", None)
            if bot is not None:
                self._bot = bot
                logger.info("[daily_qzone] 已捕获 aiocqhttp bot 客户端")

    # ---------- 定时循环 ----------
    async def _loop(self) -> None:
        base = max(2, int(self.cfg["check_interval_minutes"]))
        # 首轮不要等满一个周期：进程往往在临近窗口时才起来（今天就重启了 4 次），
        # 每次都从 sleep 开头排，等于永远等不到 tick。
        await asyncio.sleep(20)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[daily_qzone] 巡查失败: {type(e).__name__}: {e}")
            await asyncio.sleep(base * 60 * random.uniform(0.8, 1.2))

    async def _tick(self) -> None:
        if not self.cfg["enabled"]:
            return
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        latest = int(self.cfg.get("latest_hour", 23))
        if now.hour >= latest:
            return
        posted = self._posted_slots(day)
        catchup = timedelta(
            minutes=max(0, float(self.cfg.get("catchup_minutes", 120)))
        )
        for h in self._hours:
            if h in posted:
                continue
            target = self._slot_time(day, h)
            if now < target:
                continue  # 还没到这一档，后面的档更晚，也不用看了
            if now >= target + catchup:
                # 错过太多就不补了，免得几篇挤在一起看起来像机器人
                self._mark_slot(h, day)
                self._save_state()
                logger.info(
                    f"[daily_qzone] {h} 点档错过 "
                    f"{int((now - target).total_seconds() // 60)} 分钟，放弃这一档"
                )
                continue
            if self._bot is None:
                logger.info("[daily_qzone] 到点但还没拿到 bot 客户端，等下一轮")
                return
            await self.post_now(f"{h} 点档", slot=h)
            return  # 一轮巡查最多发一篇，其余档留给后续 tick

    # ---------- 当日总结素材 ----------
    def _collect_today_digest(self) -> str:
        """从 livingmemory 消息库捞今天的群聊记录，压成 LLM 能吃的摘要文本。"""
        if not os.path.isfile(LM_DB):
            return ""
        day_start = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        limit = max(20, int(self.cfg.get("summary_max_msgs", 160)))
        budget = max(500, int(self.cfg.get("summary_max_chars", 4000)))
        try:
            db = sqlite3.connect(f"file:{LM_DB}?mode=ro", uri=True)
            rows = db.execute(
                "SELECT sender_name, content FROM messages "
                "WHERE timestamp >= ? AND session_id LIKE '%GroupMessage%' "
                "ORDER BY timestamp ASC LIMIT ?",
                (day_start, limit * 3),
            ).fetchall()
            db.close()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[daily_qzone] 读消息库失败: {type(e).__name__}: {e}")
            return ""
        lines: list[str] = []
        total = 0
        for name, content in rows:
            text = re.sub(r"\s+", " ", str(content or "")).strip()
            # 压掉占位符/渲染痕迹，过滤太长或无信息量的行
            text = _PLACEHOLDER_RE.sub("", text).strip()
            if len(text) < 2 or len(text) > 120:
                continue
            who = str(name or "").strip() or "?"
            line = f"{who}：{text}"
            if total + len(line) > budget or len(lines) >= limit:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)

    # ---------- 表情包配图 ----------
    def _pick_sticker_b64(self) -> str | None:
        """从本地表情库随机抽一张。gif 原样上传（不取首帧），保证发到空间还在动；
        webp 动图转成多帧 gif，静图才转 PNG。"""
        try:
            paths = []
            with open(STICKER_MAP, encoding="utf-8") as f:
                for raw in f:
                    seg = raw.strip().split(":")
                    if len(seg) == 3 and seg[2]:
                        paths.append(seg[2])
            random.shuffle(paths)
            for rel in paths[:15]:  # 最多试 15 张，找不到能用的就放弃
                path = os.path.join(STICKER_ROOT, rel)
                if not os.path.isfile(path):
                    continue
                ext = os.path.splitext(path)[1].lower()
                try:
                    if ext == ".gif":
                        with open(path, "rb") as f:
                            data = f.read()  # 动图整帧带上，别 seek(0)
                    elif ext == ".webp":
                        if _PILImage is None:
                            continue
                        img = _PILImage.open(path)
                        frames, durations = [], []
                        try:
                            while True:
                                frames.append(img.convert("RGBA").copy())
                                durations.append(int(img.info.get("duration", 60) or 60))
                                img.seek(img.tell() + 1)
                        except EOFError:
                            pass
                        buf = io.BytesIO()
                        if len(frames) > 1:
                            frames[0].save(buf, "GIF", save_all=True,
                                           append_images=frames[1:],
                                           duration=durations, loop=0, disposal=2)
                        else:
                            frames[0].save(buf, "PNG")
                        data = buf.getvalue()
                    elif ext in (".png", ".jpg", ".jpeg"):
                        with open(path, "rb") as f:
                            data = f.read()
                    else:
                        continue
                    return base64.b64encode(data).decode()
                except Exception:  # noqa: BLE001
                    continue
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[daily_qzone] 挑表情包失败: {type(e).__name__}: {e}")
        return None

    # ---------- 发帖 ----------
    async def post_now(
        self, reason: str, force: bool = False, slot: int | None = None
    ) -> bool:
        """生成并发布一篇说说。成功返回 True。

        幂等闸门：真正的「这一档今天发过没」判断放在锁里做，而不是只靠调用方。
        巡查循环、手动命令、重启后的补发可能同时进来，只查一次必然重复发。
        """
        async with self._lock:
            if not force:
                if slot is not None:
                    if slot in self._posted_slots():
                        logger.info(f"[daily_qzone] {slot} 点档今天已发过，跳过（幂等闸门）")
                        return False
                elif len(self._posted_slots()) >= len(self._hours):
                    logger.info("[daily_qzone] 今天的档位都发完了，跳过（幂等闸门）")
                    return False
            if self._bot is None:
                logger.warning("[daily_qzone] 还没拿到 bot 客户端（未登录？），发不了")
                return False
            # 失败冷却：生成失败/发送失败后 30 分钟内不再重试，免得把模型额度烧光
            last_try = float(self.state.get("last_attempt_ts", 0) or 0)
            if not force and time.time() - last_try < 1800:
                logger.info("[daily_qzone] 30 分钟内刚失败过，等冷却结束再试")
                return False
            self.state["last_attempt_ts"] = time.time()
            self._save_state()
            return await self._post_locked(reason, slot)

    async def _post_locked(self, reason: str, slot: int | None = None) -> bool:
        text = await self._generate()
        if not text:
            logger.warning("[daily_qzone] LLM 没吐出有效内容，本次放弃")
            return False
        images: list[str] = []
        if random.random() < float(self.cfg.get("sticker_probability", 0.95)):
            b64 = self._pick_sticker_b64()
            if b64:
                images = [f"base64://{b64}"]
            else:
                logger.info("[daily_qzone] 掷中配图但没挑到能用的表情包，本条不带图")
        else:
            logger.info("[daily_qzone] 配图概率掷骰未中，本条纯文字")
        try:
            resp = await self._bot.api.call_action(
                "send_qzone_msg",
                content=text,
                images=images,
                ugc_right=int(self.cfg["ugc_right"]),
            )
            # OneBot 失败也可能以 retcode!=0 返回而不抛异常，不查就成了「假装发成功」
            code = (resp or {}).get("retcode", 0) if isinstance(resp, dict) else 0
            if code not in (0, None):
                msg = (resp or {}).get("message") or (resp or {}).get("wording") or ""
                logger.error(f"[daily_qzone] send_qzone_msg 被拒: retcode={code} {msg}")
                self.state["last_fail"] = f"{datetime.now():%H:%M:%S} retcode={code} {msg}"
                self._save_state()
                return False
            tid = str((resp or {}).get("tid") or (resp or {}).get("data", {}).get("tid") or "")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[daily_qzone] send_qzone_msg 失败: {type(e).__name__}: {e}")
            self.state["last_fail"] = f"{datetime.now():%H:%M:%S} {type(e).__name__}: {e}"
            self._save_state()
            return False
        self.state["day"] = datetime.now().strftime("%Y-%m-%d")
        self.state["tid"] = tid
        self.state["last_text"] = text
        self.state["last_with_image"] = bool(images)
        if tid:
            hist = self.state.setdefault("tids", [])
            hist.insert(0, {
                "tid": tid,
                "ts": time.time(),
                "text": text[:60],
                "image": bool(images),
            })
            del hist[20:]
        self.state.pop("last_fail", None)
        self._mark_slot(slot)
        self._save_state()
        logger.info(
            f"[daily_qzone] 空间动态已发布（{reason}，配图={bool(images)}），"
            f"tid={tid[:12]}…: {text[:50]}"
        )
        if tid and self.cfg.get("self_like", True):
            task = asyncio.create_task(self._self_like(tid))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        return True

    # ---------- 发完给自己点赞 ----------
    def _platform_bot(self):
        """兜底取 aiocqhttp 客户端：刚热重载还没收到消息时 _bot 是空的，
        光等消息事件会让自赞静默跳过。"""
        try:
            for inst in self.context.platform_manager.get_insts():
                try:
                    meta = inst.meta()
                except Exception:  # noqa: BLE001
                    meta = None
                if getattr(meta, "name", "") not in ("aiocqhttp", ""):
                    continue
                bot = getattr(inst, "bot", None)
                if bot is not None and hasattr(bot, "call_action"):
                    self._bot = bot
                    return bot
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[daily_qzone] 从平台管理器取 bot 失败: {type(e).__name__}: {e}")
        return None

    async def _get_auth(self):
        """拿 qzone cookie + bkn + 自己的 uin（多域名合并，残缺 cookie 会整块罢工）。"""
        merged: dict[str, str] = {}
        bkn = ""
        for domain in (
            "qzone.qq.com",
            "user.qzone.qq.com",
            "h5.qzone.qq.com",
            "mobile.qzone.qq.com",
            "qq.com",
        ):
            try:
                resp = await self._bot.api.call_action("get_cookies", domain=domain) or {}
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[daily_qzone] get_cookies({domain}) 失败: {type(e).__name__}: {e}")
                continue
            if not bkn:
                bkn = str(resp.get("bkn") or "")
            for part in str(resp.get("cookies") or "").split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    merged.setdefault(k.strip(), v.strip())
            if (merged.get("p_skey") or merged.get("skey")) and (
                merged.get("uin") or merged.get("p_uin")
            ):
                break
        if not merged:
            raise RuntimeError("get_cookies 返回空 cookie")
        if not bkn:
            raise RuntimeError("get_cookies 没返回 bkn")
        cookies = "; ".join(f"{k}={v}" for k, v in merged.items())
        uin = ""
        for key in ("uin", "p_uin", "o_uin", "luin"):
            m = re.match(r"o?(\d{5,12})", str(merged.get(key) or ""))
            if m:
                uin = m.group(1)
                break
        if not uin:
            try:
                info = await self._bot.api.call_action("get_login_info") or {}
                data = info.get("data") if isinstance(info.get("data"), dict) else info
                uin = str(data.get("user_id") or "")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[daily_qzone] get_login_info 失败: {type(e).__name__}: {e}")
        if not uin:
            raise RuntimeError("拿不到自己的 uin")
        return cookies, bkn, uin

    async def _self_like(self, tid: str) -> None:
        """发完说说给自己点个赞：延迟抖动一下，像顺手刷到自己这条。"""
        if self._bot is None:
            self._platform_bot()
        if self._bot is None:
            logger.warning("[daily_qzone] 自赞跳过：还没拿到 aiocqhttp 客户端")
            return
        delay = float(self.cfg.get("self_like_delay", 60))
        await asyncio.sleep(max(5.0, delay) * random.uniform(0.5, 1.5))
        try:
            cookies, bkn, uin = await self._get_auth()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[daily_qzone] 自赞取凭据失败: {type(e).__name__}: {e}")
            return
        unikey = f"http://user.qzone.qq.com/{uin}/mood/{tid}.1"
        form = {
            "qzreferrer": f"https://user.qzone.qq.com/{uin}",
            "opuin": uin,
            "unikey": unikey,
            "curkey": unikey,
            "fid": tid,
            "appid": "311",
            "from": "1",
            "type": "0",  # 0=点赞，1=取消
            "abstime": str(int(time.time())),
            "fupdate": "1",
        }
        headers = {
            "Cookie": cookies,
            "Referer": f"https://user.qzone.qq.com/{uin}",
            "User-Agent": UA,
        }
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(
                    LIKE_URL, params={"g_tk": bkn}, data=form, headers=headers
                ) as r:
                    raw = await r.text()
            j = None
            # Qzone 回的是 HTML 包着的 frameElement.callback({...})：前面的 try{ 也是花括号，
            # 所以从 "code" 往前找对象的起始 {，再用 raw_decode 只吃这一个对象
            key_at = raw.find('"code"')
            if key_at >= 0:
                obj_at = raw.rfind("{", 0, key_at)
                if obj_at >= 0:
                    try:
                        j, _ = json.JSONDecoder().raw_decode(raw[obj_at:])
                    except ValueError:
                        j = None
            if not isinstance(j, dict):
                logger.warning(f"[daily_qzone] 自赞返回无法解析: {raw[:80]}")
                return
            code = j.get("code", j.get("ret", 0))
            if code in (0, None):
                self.state["self_liked"] = {"tid": tid, "ts": time.time()}
                self._save_state()
                logger.info(f"[daily_qzone] 已给自己刚发的说说点赞 tid={tid[:12]}…")
            else:
                logger.warning(
                    f"[daily_qzone] 自赞被拒 code={code} {j.get('message') or j.get('msg') or ''}"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[daily_qzone] 自赞失败: {type(e).__name__}: {e}")

    def _provider_chain(self) -> list:
        """写说说优先用官方 API（DeepSeek），拿不到再退回默认 provider。"""
        chain = []
        want = str(self.cfg.get("prefer_provider_id") or "").strip()
        if want:
            try:
                p = self.context.get_provider_by_id(want)
                if p is not None:
                    chain.append(p)
            except Exception:  # noqa: BLE001
                pass
        try:
            p = self.context.get_using_provider()
            if p is not None:
                chain.append(p)
        except Exception:  # noqa: BLE001
            pass
        return chain

    async def _generate(self) -> str:
        # 优先：当日群聊总结
        prompt, sys = "", SYSTEM_PROMPT
        if self.cfg.get("daily_summary", True):
            digest = self._collect_today_digest()
            if digest:
                prompt = f"今天的群聊记录：\n{digest}\n\n写一条今日总结说说。"
                sys = SUMMARY_PROMPT
        used_digest = bool(prompt)
        if not prompt:  # 兜底：主题池
            theme = random.choice(self.cfg["themes"])
            prompt = f"今天的说说主题方向：{theme}。写一条。"
            used_digest = False

        chain = self._provider_chain()
        if not chain:
            logger.warning("[daily_qzone] 没有可用 provider")
            return ""
        retry = max(1, int(self.cfg.get("llm_retry", 2)))
        for provider in chain:
            try:
                pid = provider.meta().id
            except Exception:  # noqa: BLE001
                pid = "?"
            for attempt in range(1, retry + 1):
                try:
                    resp = await provider.text_chat(
                        prompt=prompt,
                        session_id=uuid.uuid4().hex,
                        contexts=[],
                        system_prompt=sys,
                        persist=False,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[daily_qzone] LLM {pid} 第 {attempt} 次失败: {type(e).__name__}: {e}"
                    )
                    continue
                text = (resp.completion_text or "").strip().strip("“”\"")
                text = _PLACEHOLDER_RE.sub("", text).strip()
                if len(text) < 6:
                    logger.warning(
                        f"[daily_qzone] LLM {pid} 第 {attempt} 次返回空/过短"
                        f"（{len(text)} 字），重试"
                    )
                    continue
                limit = int(self.cfg["max_chars"])
                if len(text) > limit:
                    text = text[:limit].rstrip("，,、 　。")
                logger.info(
                    f"[daily_qzone] 说说生成完成（{pid}，"
                    f"{'当日总结' if used_digest else '主题池'}，{len(text)} 字）"
                )
                return text
        logger.error("[daily_qzone] 所有 provider 都没吐出有效内容，本次放弃")
        return ""
