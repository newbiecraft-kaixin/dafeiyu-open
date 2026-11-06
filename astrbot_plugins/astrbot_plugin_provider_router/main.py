"""按会话类型 + 消息复杂度分流对话模型（大小模型切换，小模型当子代理）。

三层分流：
1. 群聊（非名单群）-> 本地模型，零 token 费用
2. 名单群（走官方 API 的群）-> 按消息复杂度再分：
   - 规则直接判「强」（长文本/代码/任务词）-> 云端强模型
   - 规则直接判「弱」（超短闲聊）-> 本地小模型
   - 中间地带 -> 本地小模型当分类子代理（免费）判断强弱，失败回落强模型
3. 私聊 -> 好友走云端模型；没加好友的临时会话/陌生人走本地，零费用

目标 provider 不存在时自动跳过，回落到全局默认。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderType

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))  # .../data
STATE_PATH = os.path.join(PLUGIN_DIR, "state.json")

# 内置指令已全部下线：这些旧英文指令不能在聊天里被当成普通消息转给 LLM
_DEAD_CMD_RE = re.compile(r"^/(\w+)(?:\s|$)")
_DEAD_CMDS = {
    "sid", "name", "reset", "stop", "new", "stats", "provider",
    "dashboard_update", "set", "unset", "help",
}

DEFAULTS = {
    "enabled": True,
    "deepseek_group_ids": [],
    "deepseek_provider_id": "deepseek/deepseek-flash",
    "group_provider_id": "ollama-chat/qwen3.5-9b",
    "private_provider_id": "ollama-chat/qwen3.5-9b",
    # 私聊里没加好友的人（群里点的临时会话 / 陌生人）：一律本地，不烧云端 token
    "stranger_private_provider_id": "ollama-chat/qwen3.5-9b",
    # 微信（weixin_oc/wechatauto）单独走一个模型，与 QQ 区分；留空则按私聊规则
    "wechat_provider_id": "deepseek/deepseek-flash",
    # 微信会话锁定的人格；"[%None]" = 明确不使用系统提示词；留空则不改
    "wechat_persona_id": "dafeiyu-wechat",
    # 按群锁定人格：{"群号": "persona_id"}，控制台角色层写入
    "group_persona_ids": {},
    # 按好友锁定人格：{"QQ号": "persona_id"}，控制台好友卡片写入
    "user_persona_ids": {},
    # 普通群的免费模型链：按顺序取第一个可用的，全挂才回落到最后一个（本地）
    "group_provider_chain": [],
    "health_probe_enabled": True,
    "health_probe_interval": 300,  # 重新探测间隔（秒）
    "health_cooldown": 600,        # 某档失败后冷却多久再试（秒）
    "health_probe_timeout": 8,     # 单次探针超时（秒）
    # —— 大小模型智能切换（仅对 deepseek_group_ids 生效）——
    "smart_route_enabled": True,
    "use_classifier": False,      # 规则判不准的中间带：False=直接走强模型（零延迟，效果优先）
    "simple_provider_id": "ollama-chat/qwen3.5-9b",  # 简单消息走的小模型
    "strong_len_threshold": 40,   # 超过这个字数直接判强
    "short_len_threshold": 8,     # 不超过这个字数且无问号直接判弱
    "task_words": ["帮我", "写一", "写个", "查一", "查下", "查查", "翻译", "总结",
                   "解释", "分析", "代码", "报错", "教程", "怎么做", "怎么写",
                   "为什么", "原理", "联网", "搜一下", "推荐一下", "对比", "区别"],
    "classify_timeout": 2.5,      # 小模型分类器超时（秒），超时按强处理
    # —— 消息管控（控制台可改，改完热加载，无需重启）——
    "block_users": [],            # 不监听这些 QQ 号（私聊和群里都不理）
    "block_groups": [],           # 不监听这些群
    "listen_groups": [],          # 非空时只监听这些群；私聊不受影响
    # —— 按人 / 按群固定模型（优先级高于默认分流）——
    "user_provider_ids": {},      # {"QQ号": "deepseek/deepseek-flash"}
    "group_provider_ids": {},     # {"群号": "zhipu/glm-4-flash"}
}

CODE_PAT = re.compile(r"```|def |import |class |func |https?://|SELECT .+ FROM|select .+ from")


@register(
    "astrbot_plugin_provider_router",
    "dafeiyu-local",
    "按群聊/私聊+消息复杂度自动分流对话模型（小模型当分类子代理）",
    "v2.1.0",
)
class ProviderRouter(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = dict(DEFAULTS)
        self._cfg_path = os.path.join(PLUGIN_DIR, "config.json")
        self._cfg_mtime = 0.0
        if os.path.exists(self._cfg_path):
            try:
                with open(self._cfg_path, encoding="utf-8-sig") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.cfg.update({k: loaded[k] for k in loaded if k in self.cfg})
                self._cfg_mtime = os.path.getmtime(self._cfg_path)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[provider_router] 读取 config.json 失败: {e}")
        # 云端档位健康表：
        #   _bad = 探针确认不可用的档位（探针成功前不再用于真实对话，避免用户干等）
        #   _next_probe = 下次允许探测的时间戳
        self._bad: set[str] = set()
        self._next_probe: dict[str, float] = {}
        self._probe_task: asyncio.Task | None = None
        self._probed_once = asyncio.Event()
        # 本会话手动指定的模型：umo -> provider_id。重启后仍生效，/转换 自动 才清掉
        self._overrides: dict[str, str] = {}
        # 控制台按群写入的人格：umo -> persona_id，用于解绑时精确回退
        self._persona_applied: dict[str, str] = {}
        self._load_state()

    # ---------- 配置热加载 + 消息管控 ----------
    def _maybe_reload(self) -> None:
        """控制台改 config.json 后，下一条消息就生效，不用重启 AstrBot。"""
        try:
            mtime = os.path.getmtime(self._cfg_path)
        except OSError:
            return
        if mtime <= self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        try:
            with open(self._cfg_path, encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for key in self.cfg:
                    if key in loaded:
                        self.cfg[key] = loaded[key]
                logger.info("[provider_router] config.json 已热加载")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[provider_router] 热加载 config.json 失败: {e}")

    @staticmethod
    def _ids(raw) -> set[str]:
        return {str(x).strip() for x in (raw or []) if str(x).strip()}

    def _block_reason(self, event: AstrMessageEvent) -> str:
        """命中黑名单或不在监听范围时返回原因，否则空串。"""
        sender_id = str(event.get_sender_id() or "")
        group_id = str(event.get_group_id() or "")
        if sender_id and sender_id in self._ids(self.cfg.get("block_users")):
            return f"用户 {sender_id} 在黑名单"
        if group_id and group_id in self._ids(self.cfg.get("block_groups")):
            return f"群 {group_id} 在黑名单"
        listen = self._ids(self.cfg.get("listen_groups"))
        if group_id and listen and group_id not in listen:
            return f"群 {group_id} 不在监听范围"
        return ""

    def _pinned_provider_id(self, event: AstrMessageEvent) -> str:
        """按人 / 按群固定的模型；不存在或没配返回空串。"""
        sender_id = str(event.get_sender_id() or "")
        group_id = str(event.get_group_id() or "")
        user_map = self.cfg.get("user_provider_ids") or {}
        group_map = self.cfg.get("group_provider_ids") or {}
        inst_map = getattr(self.context.provider_manager, "inst_map", {})
        for source, pid in (("用户", user_map.get(sender_id)), ("群", group_map.get(group_id))):
            pid = str(pid or "").strip()
            if not pid:
                continue
            if pid in inst_map:
                return pid
            logger.warning(f"[provider_router] {source}固定模型 {pid} 不存在，忽略")
        return ""

    # ---------- 手动指定模型（/模型、/转换） ----------
    def _load_state(self) -> None:
        try:
            with open(STATE_PATH, encoding="utf-8-sig") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[provider_router] 读取 state.json 失败: {e}")
            return
        ov = data.get("overrides") if isinstance(data, dict) else None
        if isinstance(ov, dict):
            self._overrides = {str(k): str(v) for k, v in ov.items() if v}
        ps = data.get("personas") if isinstance(data, dict) else None
        if isinstance(ps, dict):
            self._persona_applied = {str(k): str(v) for k, v in ps.items() if v}

    def _save_state(self) -> None:
        try:
            tmp = f"{STATE_PATH}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "overrides": self._overrides,
                        "personas": self._persona_applied,
                    },
                    f, ensure_ascii=False, indent=2
                )
            os.replace(tmp, STATE_PATH)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[provider_router] 写入 state.json 失败: {e}")

    def _providers(self) -> list:
        try:
            return list(self.context.get_all_providers() or [])
        except Exception:  # noqa: BLE001
            return []

    def _provider_table(self, current_id: str, page: int = 1, size: int = 10) -> str:
        lines = []
        for i, p in enumerate(self._providers(), 1):
            try:
                meta = p.meta()
            except Exception:  # noqa: BLE001
                continue
            mark = "  ← 现在用的就是这个" if meta.id == current_id else ""
            lines.append(f"{i}. {meta.model}（{meta.id}）{mark}")
        if not lines:
            return "（没有配置可用的对话模型）"
        total = (len(lines) + size - 1) // size
        page = max(1, min(page, total))
        body = "\n".join(lines[(page - 1) * size: page * size])
        tail = f"第 {page}/{total} 页"
        if page < total:
            tail += f" · 下一页 /模型 {page + 1}"
        return body + "\n" + tail

    @filter.command("模型")
    async def cmd_show_model(self, event: AstrMessageEvent, page: str = "1") -> None:
        """/模型 [页码] —— 看当前用的模型和可用模型"""
        self._maybe_reload()
        if self._block_reason(event):
            event.stop_event()
            return
        umo = event.unified_msg_origin
        cur_id = ""
        try:
            current = self.context.get_using_provider(umo)
            cur_id = current.meta().id if current else ""
        except Exception:  # noqa: BLE001
            cur_id = ""
        head = f"现在用的是：{cur_id or '没取到'}"
        if self._overrides.get(umo):
            head += "（已锁定）"
        try:
            p = max(1, int(str(page).strip() or 1))
        except ValueError:
            p = 1
        event.set_result(
            MessageEventResult().message(
                f"{head}\n可用模型：\n{self._provider_table(cur_id, p)}"
            )
        )
        event.stop_event()

    @filter.command("转换")
    async def cmd_switch_model(self, event: AstrMessageEvent) -> None:
        """/转换 <编号|名字> —— 本会话换模型；/转换 自动 恢复自动分配（仅主人）。"""
        self._maybe_reload()
        if self._block_reason(event):
            event.stop_event()
            return
        umo = event.unified_msg_origin
        if not event.is_admin():
            event.set_result(
                MessageEventResult().message("换模型仅主人。")
            )
            event.stop_event()
            return
        raw = re.sub(
            r"^[\s/！!]*转换", "", (event.message_str or "").strip(), flags=re.I
        ).strip()
        norm = raw.lower()
        provs = self._providers()
        if not norm or norm in ("列表", "list", "?"):
            event.set_result(
                MessageEventResult().message(
                    "用法：/转换 <编号或名字>（/转换 自动 恢复自动分配）\n"
                    f"{self._provider_table(self._current_id(umo))}"
                )
            )
            event.stop_event()
            return
        if norm in ("自动", "默认", "清除", "恢复", "reset", "auto"):
            self._overrides.pop(umo, None)
            self._save_state()
            base = self._base_target(event) or ""
            if base:
                try:
                    await self.context.provider_manager.set_provider(
                        base, ProviderType.CHAT_COMPLETION, umo=umo
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[provider_router] 恢复自动分配失败: {type(e).__name__}: {e}")
            event.set_result(
                MessageEventResult().message(f"已恢复自动分配（现在走 {base or '默认'}）")
            )
            event.stop_event()
            return
        target = ""
        if norm.isdigit():
            idx = int(norm)
            if 1 <= idx <= len(provs):
                try:
                    target = provs[idx - 1].meta().id
                except Exception:  # noqa: BLE001
                    target = ""
        else:
            hits = []
            for p in provs:
                try:
                    meta = p.meta()
                except Exception:  # noqa: BLE001
                    continue
                pid = (meta.id or "").lower()
                model = (meta.model or "").lower()
                if norm == pid or norm in model or norm in pid:
                    hits.append(meta.id)
            if len(hits) > 1:
                event.set_result(
                    MessageEventResult().message(
                        "匹配到好几个：" + "、".join(hits) + "，用编号或者完整 id"
                    )
                )
                event.stop_event()
                return
            target = hits[0] if hits else ""
        if not target:
            event.set_result(
                MessageEventResult().message("没找到这个模型。/模型 看一下列表。")
            )
            event.stop_event()
            return
        try:
            await self.context.provider_manager.set_provider(
                target, ProviderType.CHAT_COMPLETION, umo=umo
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[provider_router] 切换模型失败: {type(e).__name__}: {e}")
            event.set_result(
                MessageEventResult().message(f"没切成（{type(e).__name__}），还是原来那个。")
            )
            event.stop_event()
            return
        self._overrides[umo] = target
        self._save_state()
        event.set_result(
            MessageEventResult().message(
                f"本会话已切到 {target}。想回去发 /转换 自动。"
            )
        )
        event.stop_event()

    def _current_id(self, umo: str) -> str:
        try:
            current = self.context.get_using_provider(umo)
            return current.meta().id if current else ""
        except Exception:  # noqa: BLE001
            return ""


    # ---------- 免费模型链 + 健康降级 ----------
    def _chain(self) -> list[str]:
        """普通群要依次尝试的 provider 列表。"""
        chain = [str(x) for x in (self.cfg.get("group_provider_chain") or []) if x]
        if not chain:
            base = str(self.cfg.get("group_provider_id") or "")
            chain = [base] if base else []
        return chain

    def _usable(self, pid: str) -> bool:
        """没被探针判定坏掉 = 可用。坏掉的要等探针重新成功才恢复。"""
        return pid not in self._bad

    def _pick_chain(self) -> str:
        """取链上第一个健康且真实存在的 provider；全挂则用最后一项（本地）兜底。"""
        chain = self._chain()
        if not chain:
            return ""
        inst_map = getattr(self.context.provider_manager, "inst_map", {})
        for pid in chain:
            if pid not in inst_map:
                continue
            if self._usable(pid):
                return pid
        return chain[-1]

    async def _probe_one(self, pid: str) -> None:
        provider = self.context.get_provider_by_id(pid)
        if not provider:
            self._bad.add(pid)
            return
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt="ping",
                    session_id=uuid.uuid4().hex,
                    contexts=[],
                    system_prompt="只回复：OK",
                    persist=False,
                ),
                timeout=float(self.cfg["health_probe_timeout"]),
            )
            if (resp.completion_text or "").strip():
                self._bad.discard(pid)  # 恢复正常
                self._next_probe[pid] = time.time() + float(self.cfg["health_probe_interval"])
                return
            raise RuntimeError("空回复")
        except Exception as e:  # noqa: BLE001
            logger.info(f"[provider_router] 探针 {pid} 不可用（{type(e).__name__}），冷却 {self.cfg['health_cooldown']}s")
            self._bad.add(pid)
            self._next_probe[pid] = time.time() + float(self.cfg["health_cooldown"])

    async def _probe_loop(self) -> None:
        while True:
            chain = self._chain()
            # 只探云端档位（本地最后一个永远可用，不必探）
            for pid in chain[:-1]:
                if time.time() < self._next_probe.get(pid, 0.0):
                    continue  # 还在冷却，先不打扰它
                await self._probe_one(pid)
            self._probed_once.set()
            await asyncio.sleep(float(self.cfg["health_probe_interval"]))

    def _ensure_probe(self) -> None:
        if not self.cfg.get("health_probe_enabled", True) or len(self._chain()) < 2:
            self._probed_once.set()
            return
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop())

    async def initialize(self) -> None:
        """插件加载时就跑一轮探针，避免第一条群消息只能落到本地。"""
        if self.cfg.get("health_probe_enabled", True) and len(self._chain()) >= 2:
            self._probe_task = asyncio.create_task(self._probe_loop())

    async def terminate(self) -> None:
        """插件被重载/禁用时停掉后台探针，避免旧循环继续空跑。"""
        if self._probe_task and not self._probe_task.done():
            self._probe_task.cancel()

    @staticmethod
    def _private_is_stranger(event: AstrMessageEvent) -> bool:
        """私聊对象是不是没加好友的人（群里点的临时会话 / 陌生人）。

        OneBot v11 私聊消息的 raw sub_type：friend = 好友，group = 群临时会话，
        other = 其他陌生人。取不到就当好友，避免误伤。
        """
        if event.get_group_id() or not event.is_private_chat():
            return False
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        try:
            sub = str(raw.get("sub_type") or "").strip().lower() if raw else ""
        except Exception:  # noqa: BLE001
            sub = ""
        return bool(sub) and sub != "friend"

    @staticmethod
    def _is_wechat(event: AstrMessageEvent) -> bool:
        # weixin_oc = 官方微信通道；wechatauto = 本机 RPA 微信桥
        return str(event.get_platform_name() or "").lower() in ("weixin_oc", "wechatauto")

    async def _apply_wechat_persona(self, event: AstrMessageEvent) -> None:
        """微信会话锁定人格；用户用 /persona 手动指定过的不覆盖。"""
        persona_id = str(self.cfg.get("wechat_persona_id") or "").strip()
        if not persona_id or not self._is_wechat(event):
            return
        umo = event.unified_msg_origin
        try:
            from astrbot.core import sp

            session_cfg = (
                await sp.get_async(
                    scope="umo",
                    scope_id=umo,
                    key="session_service_config",
                    default={},
                )
                or {}
            )
            if str(session_cfg.get("persona_id") or "").strip():
                return
            session_cfg["persona_id"] = persona_id
            await sp.put_async(
                scope="umo",
                scope_id=umo,
                key="session_service_config",
                value=session_cfg,
            )
            logger.info(f"[provider_router] {umo} 人格锁定为 {persona_id}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[provider_router] 设置微信人格失败: {type(e).__name__}: {e}")

    async def _apply_persona(self, event: AstrMessageEvent) -> None:
        """按群 / 按好友锁定控制台选的人格；解绑时只回退自己写过的值。"""
        group_id = str(event.get_group_id() or "")
        if group_id:
            mapping = self.cfg.get("group_persona_ids") or {}
            key_id, scope = group_id, "群"
        else:
            if not event.is_private_chat() or self._private_is_stranger(event):
                return
            mapping = self.cfg.get("user_persona_ids") or {}
            key_id, scope = str(event.get_sender_id() or ""), "好友"
        if not key_id:
            return
        wanted = str(mapping.get(key_id) or "").strip() if isinstance(mapping, dict) else ""
        umo = event.unified_msg_origin
        applied = self._persona_applied.get(umo)
        if not wanted and not applied:
            return
        if wanted == applied:
            return
        try:
            from astrbot.core import sp

            session_cfg = (
                await sp.get_async(
                    scope="umo",
                    scope_id=umo,
                    key="session_service_config",
                    default={},
                )
                or {}
            )
            current = str(session_cfg.get("persona_id") or "").strip()
            if wanted:
                if current and current != applied:
                    # 用户手动 `/persona` 指定过，不覆盖
                    return
                session_cfg["persona_id"] = wanted
                self._persona_applied[umo] = wanted
                logger.info(f"[provider_router] {scope} {key_id} 人格锁定为 {wanted}")
            else:
                if current == applied:
                    session_cfg.pop("persona_id", None)
                    logger.info(f"[provider_router] {scope} {key_id} 人格已解绑")
                self._persona_applied.pop(umo, None)
            await sp.put_async(
                scope="umo",
                scope_id=umo,
                key="session_service_config",
                value=session_cfg,
            )
            self._save_state()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[provider_router] 设置{scope}人格失败: {type(e).__name__}: {e}")

    def _base_target(self, event: AstrMessageEvent) -> str:
        # 微信单独一路上游模型，不受 QQ 群/私聊规则影响
        if self._is_wechat(event):
            wechat_target = str(self.cfg.get("wechat_provider_id") or "").strip()
            if wechat_target:
                return wechat_target
        group_id = str(event.get_group_id() or "")
        if group_id and group_id in {str(g) for g in self.cfg["deepseek_group_ids"]}:
            return str(self.cfg["deepseek_provider_id"] or "")
        if group_id:
            self._ensure_probe()
            # 首轮探针没跑完时先走本地，避免把消息砸到一个已知 429 的档位
            if not self._probed_once.is_set():
                return self._chain()[-1] if self._chain() else ""
            return self._pick_chain()
        if self._private_is_stranger(event):
            return str(
                self.cfg.get("stranger_private_provider_id")
                or self.cfg.get("group_provider_id")
                or ""
            )
        return str(self.cfg["private_provider_id"] or "")

    # ---------- 大小模型智能切换 ----------
    def _is_complex_by_rule(self, text: str) -> bool | None:
        """规则判复杂度：True=强，False=弱，None=交给小模型分类器"""
        text = (text or "").strip()
        if not text:
            return False
        if len(text) > int(self.cfg["strong_len_threshold"]):
            return True
        if CODE_PAT.search(text):
            return True
        if any(w in text for w in self.cfg["task_words"]):
            return True
        if len(text) <= int(self.cfg["short_len_threshold"]) and "？" not in text and "?" not in text:
            return False
        return None

    async def _classify_by_small_model(self, text: str) -> bool:
        """本地小模型当子代理判断复杂度；True=强（走云端）。失败回落强。"""
        provider = self.context.get_provider_by_id(str(self.cfg["simple_provider_id"]))
        if not provider:
            return True
        prompt = (
            "判断这条 QQ 群消息需不需要高质量大模型回复。\n"
            "需要写代码、查资料、翻译、解释原理、认真回答问题 → 强\n"
            "纯闲聊、接梗、情绪表达、打招呼 → 弱\n"
            "只回答一个字：强 或 弱\n\n消息：" + text[:100]
        )
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    session_id=uuid.uuid4().hex,
                    contexts=[],
                    system_prompt="你是路由分类器。只输出：强 或 弱",
                    persist=False,
                ),
                timeout=float(self.cfg["classify_timeout"]),
            )
            answer = (resp.completion_text or "").strip()
            return "弱" not in answer  # 答非所问一律按强处理
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[provider_router] 分类器超时/失败（{type(e).__name__}），按强处理")
            return True

    async def _target(self, event: AstrMessageEvent) -> str:
        # 按人 / 按群固定了模型，就不再参与大小模型自动分流
        pinned = self._pinned_provider_id(event)
        if pinned:
            return pinned
        base = self._base_target(event)
        group_id = str(event.get_group_id() or "")
        smart_on = (
            self.cfg.get("smart_route_enabled", True)
            and group_id in {str(g) for g in self.cfg["deepseek_group_ids"]}
            and base  # 有云端模型才谈切换
        )
        if not smart_on:
            return base
        text = (event.message_str or "").strip()
        rule = self._is_complex_by_rule(text)
        if rule is True:
            return base  # 强模型
        if rule is False:
            return str(self.cfg["simple_provider_id"] or "") or base
        # 中间带：默认直接走强模型（分类器有 2.5s 延迟，效果优先时不启用）
        if self.cfg.get("use_classifier", False):
            complex_ = await self._classify_by_small_model(text)
            return base if complex_ else str(self.cfg["simple_provider_id"] or "") or base
        return base

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def route(self, event: AstrMessageEvent):
        self._maybe_reload()
        blocked = self._block_reason(event)
        if blocked:
            logger.debug(f"[provider_router] 忽略消息：{blocked}")
            event.stop_event()
            return
        # 内置指令已下线：旧英文指令别当聊天内容送进 LLM（中文指令不在此列）
        # 注意 event.message_str 里的唤醒前缀已被 pipeline 剥掉，要看原始文本
        raw = str(getattr(event.message_obj, "message_str", "") or "").strip()
        wake_cfg = self.context.get_config(umo=event.unified_msg_origin)
        for wp in (wake_cfg.get("wake_prefix") or ["/"]):
            if wp and raw.startswith(wp):
                raw = raw[len(wp):].strip()
                break
        m = _DEAD_CMD_RE.match(raw)
        if m and m.group(1).lower() in _DEAD_CMDS:
            event.stop_event()
            return
        if not self.cfg["enabled"]:
            return
        await self._apply_wechat_persona(event)
        await self._apply_persona(event)
        umo = event.unified_msg_origin
        # 手动 /转换 过的会话，路由不再插手，直到 /转换 自动
        override = self._overrides.get(umo)
        if override and override not in getattr(self.context.provider_manager, "inst_map", {}):
            self._overrides.pop(umo, None)
            self._save_state()
            override = None
        if override:
            target = override
        else:
            try:
                target = await self._target(event)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[provider_router] 路由计算失败: {type(e).__name__}: {e}")
                return
        if not target:
            return
        try:
            current = self.context.get_using_provider(umo)
            if current and current.meta().id == target:
                return
            manager = self.context.provider_manager
            if target not in manager.inst_map:
                logger.warning(f"[provider_router] 目标 provider 不存在: {target}")
                return
            await manager.set_provider(target, ProviderType.CHAT_COMPLETION, umo=umo)
            logger.info(
                f"[provider_router] {umo} -> {target}"
                f"（{'群聊' if event.get_group_id() else '私聊'}）"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[provider_router] 分流失败: {type(e).__name__}: {e}")
