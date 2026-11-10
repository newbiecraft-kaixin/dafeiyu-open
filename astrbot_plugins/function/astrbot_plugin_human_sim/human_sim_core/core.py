"""核心运行时：初始化、状态落盘、频控/共享闸门、成员与身份等公共设施。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from astrbot.api import logger
from astrbot.api.star import Context, Star, register

from .config import DATA_ROOT, DEFAULTS, PLUGIN_DIR, STATE_PATH, STICKER_MAP, STICKER_ROOT
from .runtime import send_gate

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"

# 主人账号（admins_id）缓存：按 mtime 失效，改配置不用重启
_OWNER_CFG_CACHE = {"mtime": None, "uids": ()}


class CoreMixin:
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = dict(DEFAULTS)
        cfg_path = os.path.join(PLUGIN_DIR, "config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8-sig") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    # 不再按 DEFAULTS 白名单过滤：以前不在默认表里的键会被静默丢弃，
                    # 表现是「config.json 写了但完全不生效」（如 greeting_slots、send_min_gap_seconds）。
                    # 这里全量合并，并把拼错的键打到日志里，便于发现。
                    unknown = sorted(set(loaded) - set(DEFAULTS))
                    if unknown:
                        logger.warning(
                            f"[human_sim] config.json 有 {len(unknown)} 个键不在 DEFAULTS 内，"
                            f"已照样生效：{unknown}"
                        )
                    self.cfg.update(loaded)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[human_sim] 读取 config.json 失败: {e}")

        # 兜底模型列表内核只在启动时读一次；插件热重载时顺手把磁盘上的值同步进内存，
        # 改主配置就不用重启 AstrBot（重启后本来也会读到同样的值）
        try:
            with open(
                os.path.join(DATA_ROOT, "cmd_config.json"), encoding="utf-8-sig"
            ) as f:
                fb = (
                    (json.load(f).get("agent_runner") or {})
                    .get("config", {})
                    .get("model", {})
                    .get("fallback_provider_ids", [])
                )
            context.get_config()["agent_runner"]["config"]["model"][
                "fallback_provider_ids"
            ] = list(fb)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 同步兜底模型配置失败: {e}")

        self._silence_llm_error_chat()

        self._history: dict[str, deque] = {}
        self._msg_ts: dict[str, deque] = {}  # 每群消息时间戳（防刷屏决策限流用）
        # group_id -> (查询时间, 群人数)：按人数缩放自主发言概率，10 分钟缓存避免每条消息都调接口
        self._gsize_cache: dict[str, tuple[float, int]] = {}
        self._super_faces_cache: dict | None = None
        self._face_pool_cache: list[int] | None = None
        self._face_cfg_ts: float = 0.0  # 上次读 face_config.json 的时间（一周一刷）
        self._bots: dict[str, object] = {}  # umo -> aiocqhttp CQHttp 客户端（调群成员列表等 API 用）
        self._gate = send_gate  # 出站节流：同群两条消息至少隔 1.6~3 秒
        self._face_task = None  # 启动后补学群历史表情的一次性任务
        self._roster: dict[str, list[dict]] = {}  # umo -> 群成员列表缓存（6 小时刷新）
        self.state: dict = {"day": "", "interject": {}, "idle_topic": {}}
        self._last_save: float = 0.0
        self._load_state()
        self._stickers = self._load_stickers()
        self._map_faces = self._load_map_faces()
        self._task: asyncio.Task | None = None
        self._deciding: set[str] = set()  # 正在跑决策调用的群（防请求堆积）
        self._grudge: dict[str, float] = {}  # umo|骂人者 -> 被骂时刻（气头上不接活）
        self._rate: dict[str, list[float]] = {}  # 每分钟滑动窗口（key -> 时间戳列表，内存态）
        self._mon_task: asyncio.Task | None = None
        # 限额度量：pass/block=通过/撞顶次数，cool=冷却拦截次数（监测周期内）
        self._rate_stats: dict[str, dict[str, int]] = {"pass": {}, "block": {}, "cool": {}}
        self._poke_last: dict[str, float] = {}  # umo -> 上次被戳回应时间（防戳一戳对刷）
        # 被拍台词防复读：umo -> 最近用过的几句；umo|戳人 -> 连拍序号
        self._poke_used: dict[str, deque] = {}
        self._poke_seq: dict[str, dict] = {}
        # 本进程收到的拍一拍事件数（不管是不是拍我）——真机排查入站链路用，
        # /拍一拍状态 里能看到：数字不动 = NapCat 压根没上报，不是插件的问题
        self._poke_seen: int = 0
        self._poke_seen_at: float = 0.0
        # 主动拍一拍的诊断计数：掷了几次骰子 / 被每群冷却拦了几次 / 成功 / 失败
        self._poke_rolls: int = 0
        self._poke_cools: int = 0
        self._poke_hits: int = 0
        self._poke_fails: int = 0
        self._react_last: dict[str, float] = {}  # umo -> 上次表情回应时间（同群防刷）
        # 输入层 +1：umo|消息id -> (上次贴的表情, 时间)；同一条只跟贴一次
        self._react_chain: dict[str, tuple[list[str], float]] = {}
        self._react_join_at: dict[str, float] = {}
        # umo -> 最近贴过的表情 ID：随机抽容易连着同一张，这里做轮换冷却
        self._react_recent: dict[str, list[int]] = {}
        # umo -> 机器人自己发出过的 message_id 集合（表情回应通知不带发送者，靠这个判定是不是 AI 的消息）
        self._own_msg_ids: dict[str, set] = {}
        self._reactions_seen: int = 0  # 累计收到的表情回应数（诊断用）
        # —— 输入层 / 决策层 / 复读 ——
        # 复读缓冲：umo -> deque[(归一化文本, 发送者, 时间戳, 图片URL)]
        self._repeat_buf: dict[str, deque] = {}
        # 上一条「接龙素材」的指纹：umo -> (key, 发送者, 原文)，用来判断已经有人 +1 了
        self._plus1_prev: dict[str, tuple] = {}
        # 各群上一条「接龙素材」的完整组件链：umo -> (key, [Plain/Face/Image])
        # +1 时整条原样重发，保证回的和被 +1 的那条是同一类东西
        # （只存表情/图会把「文字+表情」跟成只剩表情，看着文不对图）
        self._plus1_material: dict[str, tuple] = {}
        # 各群最近一次「跟过」的指纹：umo -> (key, 时间)
        # 复读和 +1 两条路都往这里记账，同一句话说两遍只跟一次，防连发两条一样的
        self._last_echo: dict[str, tuple[str, float]] = {}
        # 好友列表缓存：私聊对象不是好友时（群里点的临时会话）改走群临时会话
        self._friend_cache: dict = {}
        # 本地探测结果缓存：umo -> (时间戳, 提示词片段)，同一目标 2 分钟内复用
        self._probe_cache: dict[str, tuple[float, str]] = {}
        # 决策层计数（/决策状态 里看命中率）
        self._verdict_stats: dict[str, int] = {"reply": 0, "silent": 0, "ask": 0, "repeat": 0}

    # ---------- 拍一拍：被拍必回 / 主动拍人（纯规则，不进 AI） ----------
    async def _member_name(self, umo: str, uid: str) -> str:
        """群成员名字：群名片 > 昵称 > QQ号；机器人自己返回自己的名字。"""
        uid = str(uid or "").strip()
        me = str(self.state.get(f"self_id:{umo}", "") or "")
        if uid == me:
            names = self.cfg.get("bot_names") or ["大肥鱼"]
            return str(names[0]) if names else "大肥鱼"
        try:
            for m in await self._get_members(umo):
                if str(m.get("user_id", "")) == uid:
                    return (m.get("card") or m.get("nickname") or uid)[:12]
        except Exception:  # noqa: BLE001
            pass
        return uid or "?"

    def _note_act(self, umo: str, desc: str, spoken: bool = False) -> None:
        """把本地动作 / 自主发言写回上下文，让模型下一轮知道自己刚做过什么。

        spoken=True 记成「大肥鱼：内容」（真的说了这句话），
        否则记成「大肥鱼：[简短动作]」（发了表情/贴了表情/拍了一下这类纯动作）。
        """
        if not umo:
            return
        try:
            text = re.sub(r"[\r\n]", " ", str(desc or "")).strip()
            if not text:
                return
            names = self.cfg.get("bot_names") or ["大肥鱼"]
            me = str(names[0]) if names else "大肥鱼"
            line = f"{me}：{text[:80]}" if spoken else f"{me}：[{text[:50]}]"
            self._history.setdefault(umo, deque(maxlen=30)).append(line)
            logger.info(f"[human_sim] 动作写回上下文（{umo.rsplit(':', 1)[-1]}）：{line}")
        except Exception:  # noqa: BLE001
            pass

    def _stat(self, kind: str, k: str) -> None:
        try:
            self._rate_stats[kind][k] = self._rate_stats[kind].get(k, 0) + 1
        except Exception:  # noqa: BLE001
            pass

    # ---------- 每分钟限流（取代旧「今日额度」） ----------
    def _unlimited(self, umo: str) -> bool:
        """无限制群名单（如走官方 API 的群）：只绕过每分钟限额，冷却仍要遵守。"""
        return umo in set(self.cfg.get("unlimited_groups", []) or [])

    def _rate_ok(self, key: str, limit: int, umo: str = "") -> bool:
        """60 秒滑动窗口限额；limit<=0 视为不限额。占位成功返回 True。"""
        if limit <= 0:
            return True
        k = f"{key}:{umo}" if umo else key
        now = datetime.now().timestamp()
        window = self._rate.setdefault(k, [])
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.pop(0)
        if len(window) >= limit:
            self._stat("block", k)
            return False
        window.append(now)
        self._stat("pass", k)
        return True

    def _rate_release(self, key: str, umo: str = "") -> None:
        """发送失败/闸门拦截时退回占的额度"""
        k = f"{key}:{umo}" if umo else key
        window = self._rate.get(k)
        if window:
            try:
                window.pop()
            except IndexError:
                pass

    # ---------- 状态 ----------
    def _load_state(self) -> None:
        restored = None
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.state.update(data)
                # 滑动窗口跟着状态一起落盘，重启后接着用；内存里另存一份，不留在 state 里反复重写
                restored = self.state.pop("chat_history_window", None)
        except (OSError, ValueError):
            pass
        if isinstance(restored, dict):
            for umo, lines in restored.items():
                if not isinstance(umo, str) or not isinstance(lines, list):
                    continue
                kept = [str(x) for x in lines if isinstance(x, str)][-30:]
                if kept:
                    self._history[umo] = deque(kept, maxlen=30)
        try:
            self._state_mtime = os.path.getmtime(STATE_PATH)
        except OSError:
            self._state_mtime = 0.0

    def _sync_mutes(self) -> None:
        """中枢面板直接改 state.json 管理禁言，这里按 mtime 热同步 mute 键。

        状态平时全在内存里、防抖落盘，外部改文件内存是感知不到的；
        每次查禁言前 stat 一下（很便宜），文件变新了就把 mute_until 键并进来。
        """
        try:
            mtime = os.path.getmtime(STATE_PATH)
        except OSError:
            return
        if mtime <= getattr(self, "_state_mtime", 0.0):
            return
        self._state_mtime = mtime
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        file_mutes = {
            key: value for key, value in data.items() if key.startswith("mute_until:")
        }
        # 双向同步：文件里已经没了的 mute 键说明外部（qq_mcp / 中枢面板）解除了，
        # 内存里也要跟着删；只合并不删的话，解禁后机器人会继续以为自己闭麦。
        for key in [k for k in self.state if k.startswith("mute_until:")]:
            if key not in file_mutes:
                self.state.pop(key, None)
        self.state.update(file_mutes)

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            # 点赞记账按天分键（like_done:YYYY-MM-DD:uid），过期的直接清掉，
            # 否则 state.json 会一天多出几十个键，越跑越胖
            today = datetime.now().strftime("%Y-%m-%d")
            for key in [k for k in self.state if k.startswith("like_done:")]:
                if not key.startswith(f"like_done:{today}:"):
                    self.state.pop(key, None)
            # 戳戳的「今日」键留当天和昨天（统计要看两日），更早的清掉
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            for key in [k for k in self.state if k.startswith("poke_stat:today:")]:
                if not key.startswith(
                    (f"poke_stat:today:{today}:", f"poke_stat:today:{yesterday}:")
                ):
                    self.state.pop(key, None)
            for key in [k for k in self.state if k.startswith("poke_stat:ptoday:")]:
                if not key.startswith(
                    (f"poke_stat:ptoday:{today}:", f"poke_stat:ptoday:{yesterday}:")
                ):
                    self.state.pop(key, None)
            # 旧版的历史累计键已不再使用，顺手清掉
            for key in [
                k for k in self.state
                if k.startswith(("poke_stat:total:", "poke_stat:ptotal:"))
            ]:
                self.state.pop(key, None)
            # 限流/冷却按「群+人」分键，过期后键本身还留着，不清就会一天天变胖
            now_ts = time.time()
            for key in [k for k in self.state if k.startswith("dir_ts:")]:
                ts = [
                    t for t in (self.state.get(key) or [])
                    if isinstance(t, (int, float)) and now_ts - float(t) <= 60
                ]
                if ts:
                    self.state[key] = ts
                else:
                    self.state.pop(key, None)
            for key in [k for k in self.state if k.startswith("dir_cool:")]:
                if now_ts > float(self.state.get(key, 0) or 0):
                    self.state.pop(key, None)
            for key in [k for k in self.state if k.startswith("like_cool:")]:
                if now_ts - float(self.state.get(key, 0) or 0) > 600:
                    self.state.pop(key, None)
            # 其余「最后时间」类缓存统一 7 天过期：last_msg / 复读 / +1 / 表情回击 /
            # 余额空消息 / 临时禁言。它们只服务冷却判断，放久了没有任何价值，
            # 但键是按 group+user 分的，不清会随群数和人数一直长。
            ts_prefixes = (
                "last_msg:", "last_repeat:", "last_plus1:", "last_sticker_back:",
                "balance_empty_ts:", "mute_until:",
            )
            for key in [k for k in self.state if k.startswith(ts_prefixes)]:
                try:
                    ts = float(self.state.get(key) or 0)
                except (TypeError, ValueError):
                    ts = 0
                if ts and now_ts - ts > 7 * 86400:
                    self.state.pop(key, None)
            # 追问 / 接龙是带时间戳的 dict，窗口过后就该丢，不然每聊一次多一个键
            for key in [k for k in self.state if k.startswith("followup:")]:
                val = self.state.get(key)
                until = float(val.get("until") or 0) if isinstance(val, dict) else 0
                if until and now_ts - until > 86400:
                    self.state.pop(key, None)
            for key in [k for k in self.state if k.startswith("plus1:")]:
                val = self.state.get(key)
                ts = float(val.get("ts") or 0) if isinstance(val, dict) else 0
                if ts and now_ts - ts > 86400:
                    self.state.pop(key, None)
            # 滑动窗口（每个会话最近 30 条）跟着状态落盘：重启后新消息进来时上下文还在，
            # 不会被当成「这人第一次说话」。只留最近 120 个会话，防止 state.json 无限膨胀。
            window = {
                umo: list(lines)[-30:]
                for umo, lines in list(self._history.items())[-120:]
                if lines
            }
            if window:
                self.state["chat_history_window"] = window
            else:
                self.state.pop("chat_history_window", None)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"[human_sim] 状态保存失败: {e}")

    def _save_state_soon(self, delay: float = 10.0) -> None:
        """防抖落盘：高频路径（私聊记账等）先标脏，合并成一次写盘。

        以前每条私聊消息都同步全量写 state.json，连发多条就是连发多次 I/O。
        最多延迟 delay 秒落盘，丢的只是这几秒内的计数/时间戳，无伤大雅。
        """
        self._state_dirty = True
        loop = asyncio.get_running_loop()
        handle = getattr(self, "_state_save_handle", None)
        if handle is not None and not handle.done():
            return
        self._state_save_handle = loop.call_later(delay, self._flush_state_if_dirty)

    def _flush_state_if_dirty(self) -> None:
        if getattr(self, "_state_dirty", False):
            self._state_dirty = False
            # 写盘前先吸收外部改动，否则会把 qq_mcp 刚写进去的「解禁」覆盖回去
            self._sync_mutes()
            self._save_state()

    def _load_map_faces(self) -> list[int]:
        """从 map.txt 读所有 face 类型条目（183 个官方黄脸），随机冒泡用"""
        out: list[int] = []
        try:
            with open(STICKER_MAP, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(":")
                    if len(parts) >= 4 and parts[2] == "face":
                        try:
                            out.append(int(parts[3]))
                        except ValueError:
                            pass
        except OSError:
            pass
        return out

    def _load_stickers(self) -> list[str]:
        out: list[str] = []
        self._sticker_meta: list[tuple[str, str, str]] = []  # (path, name, meaning)
        # 名称 -> QQ 黄脸 id：:西瓜: 这种占位符要换成真表情，而不是被抹成空气
        self._face_by_name: dict[str, int] = {}
        try:
            with open(STICKER_MAP, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(":")
                    if len(parts) >= 4 and parts[2] == "face":
                        try:
                            self._face_by_name.setdefault(parts[0], int(parts[3]))
                        except ValueError:
                            pass
                        continue
                    rel = parts[-1]
                    if rel.startswith("face:"):
                        continue
                    path = os.path.join(STICKER_ROOT, rel)
                    if os.path.exists(path):
                        out.append(path)
                        name = parts[0]
                        meaning = parts[1] if len(parts) >= 3 else name
                        self._sticker_meta.append((path, name, meaning))
        except OSError:
            pass
        return out

    # ---------- 人群系数：人数 × 近 5 分钟活跃度 ----------

    @staticmethod
    def _gid_of_umo(umo: str) -> str:
        """从 aiocqhttp:GroupMessage:<群号>:<机器人号> 里抠出群号，非群会话返回空。"""
        parts = (umo or "").split(":")
        if len(parts) >= 3 and parts[1] == "GroupMessage":
            return parts[2]
        return ""

    # ---------- 工具 ----------
    async def _get_members(self, umo: str) -> list[dict]:
        """拿群成员列表（含 QQ 号/昵称/群名片/role），6 小时缓存"""
        cached = self._roster.get(umo)
        ts = float(self.state.get(f"roster_ts:{umo}", 0) or 0)
        if cached and time.time() - ts < 6 * 3600:
            return cached
        bot = self._bots.get(umo)
        if bot is None:
            return cached or []
        try:
            members = await bot.get_group_member_list(
                group_id=int(umo.rsplit(":", 1)[-1])
            )
            self._roster[umo] = members
            self.state[f"roster_ts:{umo}"] = time.time()
            self._save_state()
            logger.info(f"[human_sim] 名册已刷新：群 {umo.rsplit(':', 1)[-1]} 共 {len(members)} 人")
            return members
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 拉取群成员列表失败: {e}")
            return cached or []

    async def _roster_brief(self, umo: str) -> str:
        """生成给 LLM 看的名册摘要：群主/管理员优先，其余随机抽样（不含机器人自己）"""
        members = await self._get_members(umo)
        if not members:
            return ""
        # 名册里不能出现机器人自己：模型会把「大肥鱼」当成另一个群友，
        # 于是写出「大肥鱼又在偷吃零食了」这种第三人称胡话
        self_id = str(self.state.get(f"self_id:{umo}") or "")
        own = set(self._bot_names())
        members = [
            m for m in members
            if (not self_id or str(m.get("user_id")) != self_id)
            and (m.get("card") or m.get("nickname") or "") not in own
        ]
        if not members:
            return ""
        def fmt(m: dict) -> str:
            role = {"owner": "群主", "admin": "管理员"}.get(m.get("role", ""), "")
            name = m.get("card") or m.get("nickname") or str(m.get("user_id"))
            tag = f"，{role}" if role else ""
            return f"{name}(QQ:{m.get('user_id')}{tag})"
        owners = [m for m in members if m.get("role") == "owner"]
        admins = [m for m in members if m.get("role") == "admin"]
        others = [m for m in members if m.get("role") not in ("owner", "admin")]
        random.shuffle(others)
        return "、".join(fmt(m) for m in owners + admins + others[:20])

    async def _pick_random_member(self, umo: str) -> str | None:
        """从群里随机抽一位活人（排除自己）"""
        members = await self._get_members(umo)
        self_id = str(self.state.get(f"self_id:{umo}") or "")
        candidates = [
            m for m in members
            if str(m.get("user_id")) != self_id and m.get("role") != "owner"
        ]
        return str(random.choice(candidates)["user_id"]) if candidates else None

    # ---------- 跨进程分钟限流（多机器人共用一份额度） ----------
    def _shared_db(self) -> str:
        p = str(self.cfg.get("shared_rate_db") or "").strip()
        if p:
            return p
        return os.path.join(DATA_ROOT, "plugin_data", "fy_rate_shared.db")

    def _shared_cap(self, umo: str) -> int:
        if self._is_private(umo) or self._unlimited(umo):
            return 0
        try:
            return int(self.cfg.get("shared_minute_cap", 20) or 0)
        except (TypeError, ValueError):
            return 20

    def _shared_key(self, umo: str) -> str:
        return f"grp:{umo.rsplit(':', 1)[-1]}"

    def _shared_left(self, umo: str) -> bool:
        """这一分钟还有没有额度（只看不占）。"""
        cap = self._shared_cap(umo)
        fn = getattr(send_gate, "peek_shared", None)
        if cap <= 0 or fn is None:
            return True
        return fn(self._shared_db(), self._shared_key(umo)) < cap

    def _shared_claim(self, umo: str) -> bool:
        """占一条额度；跨进程原子，多机器人抢同一条不会超发。"""
        cap = self._shared_cap(umo)
        fn = getattr(send_gate, "claim_shared", None)
        if cap <= 0 or fn is None:
            return True
        return fn(self._shared_db(), self._shared_key(umo), cap)

    @staticmethod
    def _is_private(umo: str) -> bool:
        return ":GroupMessage:" not in umo

    def _global_gate(self, umo: str) -> bool:
        """防封号总闸：全局最小间隔 + 每分钟总量，所有自主发言先过这道门"""
        now = datetime.now()
        if self._unlimited(umo):
            return True  # 无限制群：跳过全局闸
        cap = int(self.cfg["global_per_minute"])
        if cap > 0 and not self._rate_ok("global", cap):
            logger.info(f"[human_sim] 防封闸：本分钟全局发言已达 {cap} 条上限")
            return False
        last = float(self.state.get("last_global_send", 0) or 0)
        gap = random.uniform(float(self.cfg["global_gap_min"]), float(self.cfg["global_gap_max"]))
        if now.timestamp() - last < gap:
            logger.info("[human_sim] 防封闸：全局间隔冷却中")
            self._stat("cool", "global_gap")
            self._rate_release("global")  # 间隔没过，退回刚占的全局额度
            return False
        self.state["last_global_send"] = now.timestamp()
        self._save_state()
        return True

    def _wants_long(self, text: str) -> bool:
        """对方是不是在要「内容」（故事/文/详细解释），而不是随口聊两句。"""
        t = (text or "").strip()
        if not t:
            return False
        hints = self.cfg.get("long_form_hints") or []
        if not any(h and h in t for h in hints):
            return False
        # 光说「故事」两个字可能是「别讲故事了」这种否定，简单挡一下
        return not any(x in t for x in ("别讲", "不要讲", "别编", "不用讲", "别写"))

    # ---------- 认人：谁是谁 ----------
    def _owner_uids(self) -> tuple[str, ...]:
        """所有「自己人」账号：cfg.owner_id + AstrBot 主配置 admins_id 里的纯数字号。

        管理员（含金主/爸爸）和主人按同一档处理：认人、关系、发脾气豁免都一致。
        插件不写配置文件时 owner_id 恒为空，兜底认主配置里的管理员，避免见谁都喊主人。
        """
        uids: list[str] = []
        uid = str(self.cfg.get("owner_id") or "").strip()
        if uid:
            uids.append(uid)
        path = os.path.join(DATA_ROOT, "cmd_config.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return tuple(uids)
        if _OWNER_CFG_CACHE["mtime"] != mtime:
            found: list[str] = []
            try:
                with open(path, encoding="utf-8-sig") as f:
                    admins = [
                        str(a).strip() for a in (json.load(f).get("admins_id") or [])
                    ]
                found = [a for a in admins if a.isdigit()]
            except Exception:  # noqa: BLE001
                found = []
            _OWNER_CFG_CACHE.update(mtime=mtime, uids=tuple(found))
        return tuple(dict.fromkeys(uids + list(_OWNER_CFG_CACHE["uids"])))

    def _owner_uid(self) -> str:
        """第一个主人 QQ（兼容只需要单个账号的老逻辑）。"""
        uids = self._owner_uids()
        return uids[0] if uids else ""

    def _bot_names(self) -> tuple[str, ...]:
        """机器人自己的名字（cfg bot_names + QQ 昵称），判断引用段是不是在引用自己。"""
        names = [str(n).strip() for n in (self.cfg.get("bot_names") or []) if str(n).strip()]
        return tuple(dict.fromkeys(names + ["大肥鱼", "开心大肥鱼"]))
