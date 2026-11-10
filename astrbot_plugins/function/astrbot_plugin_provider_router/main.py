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
    # 配置丢失时的兜底不能落到本地小模型：宁可走云端，也别静默降智
    "group_provider_id": "deepseek/deepseek-flash",
    "private_provider_id": "deepseek/deepseek-flash",
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
    # —— 按人 / 按群固定模型（主备两张表：主档探针失败自动切备用，没有其它兜底）——
    "user_provider_ids": {},          # {"QQ号": "主API"}
    "group_provider_ids": {},         # {"群号": "主API"}
    "user_provider_backup_ids": {},   # {"QQ号": "备用API"}
    "group_provider_backup_ids": {},  # {"群号": "备用API"}
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
        # 用户在会话里手动 /persona 指定过的会话；中枢映射不抢，
        # 但中枢再显式切换（映射值变了）会覆盖并解除标记
        self._manual_pinned: set[str] = set()
        self._load_state()

    # ---------- 主人身份动态注入 ----------
    # 人设文本里不写真实 QQ/微信，统一用占位符（主人 QQ=1000000000，微信=wxid_owner），
    # 每轮 LLM 请求前在这里替换成 cmd_config.json admins_id 里的真实身份。
    _OWNER_QQ_TOKEN = "1000000000"
    _OWNER_WX_TOKEN = "wxid_owner"

    def _admin_ids(self) -> tuple[list[str], list[str]]:
        """从 cmd_config.json 读管理员名单，拆成 QQ 号与微信 ID 两组。"""
        try:
            data_dir = os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))))
            with open(os.path.join(data_dir, "cmd_config.json"), encoding="utf-8-sig") as f:
                rows = [str(x).strip() for x in json.load(f).get("admins_id") or [] if str(x).strip()]
        except Exception:  # noqa: BLE001 - 读不到就保持占位符原样
            rows = []
        return ([r for r in rows if r.isdigit()],
                [r for r in rows if r.lower().startswith("wxid_")])

    @filter.on_llm_request()
    async def inject_owner_identity(self, event: AstrMessageEvent, req) -> None:
        """把人设/记忆注入文本里的主人占位符换成真实管理员身份（发请求前最后一跳）。"""
        prompt = getattr(req, "system_prompt", None)
        if not prompt or (self._OWNER_QQ_TOKEN not in prompt and self._OWNER_WX_TOKEN not in prompt):
            return
        qqs, wxs = self._admin_ids()
        if qqs:
            prompt = prompt.replace(self._OWNER_QQ_TOKEN, " 或 ".join(qqs))
        if wxs:
            prompt = prompt.replace(self._OWNER_WX_TOKEN, " 或 ".join(wxs))
        req.system_prompt = prompt

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

    def _pinned_pair(self, event: AstrMessageEvent) -> tuple[str, str]:
        """按人 / 按群固定的主备模型；(主, 备)，没配的返回空串。"""
        sender_id = str(event.get_sender_id() or "")
        group_id = str(event.get_group_id() or "")
        inst_map = getattr(self.context.provider_manager, "inst_map", {})
        if group_id:
            main = str((self.cfg.get("group_provider_ids") or {}).get(group_id) or "").strip()
            backup = str((self.cfg.get("group_provider_backup_ids") or {}).get(group_id) or "").strip()
            scope = f"群 {group_id}"
        else:
            main = str((self.cfg.get("user_provider_ids") or {}).get(sender_id) or "").strip()
            backup = str((self.cfg.get("user_provider_backup_ids") or {}).get(sender_id) or "").strip()
            scope = f"用户 {sender_id}"
        if main and main not in inst_map:
            logger.warning(f"[provider_router] {scope}主模型 {main} 不存在，忽略")
            main = ""
        if backup and backup not in inst_map:
            logger.warning(f"[provider_router] {scope}备用模型 {backup} 不存在，忽略")
            backup = ""
        return main, backup

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
        mp = data.get("manual_personas") if isinstance(data, dict) else None
        if isinstance(mp, list):
            self._manual_pinned = {str(x) for x in mp if x}

    def _save_state(self) -> None:
        try:
            tmp = f"{STATE_PATH}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "overrides": self._overrides,
                        "personas": self._persona_applied,
                        "manual_personas": sorted(self._manual_pinned),
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

    async def _priv_result(self, event: AstrMessageEvent, text: str, use_t2i: bool = False) -> None:
        """管理指令回执：私聊正常回；群聊私发给发言人，失败才退回群里 @ 一句。"""
        if not event.get_group_id():
            event.set_result(MessageEventResult().message(text).use_t2i(use_t2i))
            event.stop_event()
            return
        bot = getattr(event, "bot", None)
        if bot is not None and str(event.get_platform_name() or "").lower() == "aiocqhttp":
            try:
                await bot.call_action("send_private_msg", user_id=int(event.get_sender_id() or 0), message=text)
                event.stop_event()
                return
            except Exception:
                try:
                    await bot.call_action(
                        "send_group_msg", group_id=int(event.get_group_id() or 0),
                        message=[
                            {"type": "at", "data": {"qq": str(event.get_sender_id() or "")}},
                            {"type": "text", "data": {"text": " 回执私发失败，私聊我重发一遍指令可见"}},
                        ],
                    )
                except Exception as e2:  # noqa: BLE001
                    logger.warning(f"[provider_router] 回执私发失败: {type(e2).__name__}: {e2}")
            event.stop_event()
            return
        event.set_result(MessageEventResult().message(text))
        event.stop_event()

    @filter.command("模型")
    async def cmd_show_model(self, event: AstrMessageEvent, page: str = "1", extra: str = "") -> None:
        """/模型 [页码] —— 看当前用的模型和可用模型；/模型 切换 <编号|名字> 直接换"""
        self._maybe_reload()
        if self._block_reason(event):
            event.stop_event()
            return
        umo = event.unified_msg_origin
        sub = self._clean_arg(str(page or "")).lower()
        if sub in ("切换", "切", "换", "switch"):
            if not event.is_admin():
                await self._priv_result(event, "换模型仅主人。")
                return
            await self._do_switch_model(event, umo, self._clean_arg(str(extra or "")))
            return
        if sub in ("自动", "默认", "清除", "恢复", "重置", "reset", "auto"):
            if not event.is_admin():
                await self._priv_result(event, "换模型仅主人。")
                return
            await self._do_switch_model(event, umo, "自动")
            return
        # 直接算路由下一轮会用谁，而不是读 AstrBot 的会话模型存储——
        # 那个存储是路由事后写进去的，刚切完/刚改绑定的第一条消息会显示旧值
        override = self._overrides.get(umo)
        if override:
            cur_id = override
            head = f"现在用的是：{cur_id}（/转换 锁定）"
        else:
            try:
                cur_id = await self._target(event)
            except Exception:  # noqa: BLE001
                cur_id = ""
            if not cur_id:
                try:
                    current = self.context.get_using_provider(umo)
                    cur_id = current.meta().id if current else ""
                except Exception:  # noqa: BLE001
                    cur_id = ""
            head = f"现在用的是：{cur_id or '没取到'}"
            main, backup = self._pinned_pair(event)
            if cur_id and backup and cur_id == backup and main:
                head += f"（主 {main} 探针不可用，临时走备用）"
        try:
            p = max(1, int(str(page).strip() or 1))
        except ValueError:
            p = 1
        await self._priv_result(
            event,
            f"{head}\n可用模型：\n{self._provider_table(cur_id, p)}\n"
            f"换模型：/模型 切换 <编号或名字>（/模型 自动 恢复自动分配）",
        )

    @filter.command("转换")
    async def cmd_switch_model(self, event: AstrMessageEvent) -> None:
        """/转换 <编号|名字> —— 本会话换模型；/转换 自动 恢复自动分配（仅主人）。"""
        self._maybe_reload()
        if self._block_reason(event):
            event.stop_event()
            return
        umo = event.unified_msg_origin
        if not event.is_admin():
            await self._priv_result(event, "换模型仅主人。")
            return
        raw = self._command_args(event, "转换")
        await self._do_switch_model(event, umo, raw)

    async def _do_switch_model(self, event: AstrMessageEvent, umo: str, raw: str) -> None:
        """/转换 与 /模型 切换 共用的模型切换主体（已过主人校验）。"""
        norm = self._clean_arg(raw).lower()
        provs = self._providers()
        if not norm or norm in ("列表", "list", "?"):
            await self._priv_result(
                event,
                "用法：/转换 <编号或名字>（/转换 自动 恢复自动分配）\n"
                f"{self._provider_table(self._current_id(umo))}",
            )
            return
        if norm in ("自动", "默认", "清除", "恢复", "reset", "auto"):
            self._overrides.pop(umo, None)
            self._save_state()
            # 手动切换 = 控制台修改：把绑定的主模型也清掉，回到默认分配
            self._write_binding(event, self._binding_map_key(event, model=True), None)
            base = self._base_target(event) or ""
            if base:
                try:
                    await self.context.provider_manager.set_provider(
                        base, ProviderType.CHAT_COMPLETION, umo=umo
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[provider_router] 恢复自动分配失败: {type(e).__name__}: {e}")
            await self._priv_result(
                event,
                f"已恢复自动分配（{self._scope_label(event)}绑定已清除，现在走 {base or '默认'}）",
            )
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
                await self._priv_result(
                    event, "匹配到好几个：" + "、".join(hits) + "，用编号或者完整 id"
                )
                return
            target = hits[0] if hits else ""
        if not target:
            await self._priv_result(event, "没找到这个模型。/模型 看一下列表。")
            return
        try:
            await self.context.provider_manager.set_provider(
                target, ProviderType.CHAT_COMPLETION, umo=umo
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[provider_router] 切换模型失败: {type(e).__name__}: {e}")
            await self._priv_result(event, f"没切成（{type(e).__name__}），还是原来那个。")
            return
        # 手动切换 = 控制台修改：写进绑定表，谁后写谁生效（控制台再改绑定同样直接覆盖）
        self._write_binding(event, self._binding_map_key(event, model=True), target)
        await self._priv_result(
            event,
            f"{self._scope_label(event)}已切到 {target} 并写入控制台绑定。想回去发 /转换 自动。",
        )

    @filter.command("指令", alias={"帮助"})
    async def cmd_help_all(self, event: AstrMessageEvent) -> None:
        """/指令 —— 列出全部已注册指令（内置 + 插件，按插件分组，含权限说明）"""
        self._maybe_reload()
        if self._block_reason(event):
            event.stop_event()
            return
        from astrbot.core.star import command_management

        try:
            commands = await command_management.list_commands()
        except Exception as e:  # noqa: BLE001
            await self._priv_result(event, f"指令清单获取失败（{type(e).__name__}）")
            return
        grouped: dict[str, list[str]] = {}
        order: list[str] = []

        def label(item: dict, prefix: str = "") -> str:
            name = (item.get("effective_command")
                    or item.get("original_command") or item.get("handler_name") or "?")
            perm = "（仅主人）" if str(item.get("permission") or "") == "admin" else ""
            desc = str(item.get("description") or "").split("\n")[0].strip()
            tail = f" - {desc}" if desc else ""
            return f"{prefix}/{name}{perm}{tail}"

        def walk(items: list[dict]) -> None:
            for item in items:
                if not item.get("enabled") or not item.get("plugin_activated", True):
                    continue
                plugin = str(item.get("plugin") or "其它")
                if plugin not in grouped:
                    grouped[plugin] = []
                    order.append(plugin)
                grouped[plugin].append(label(item, "  " if item.get("parent_signature") else ""))
                for sub in (item.get("sub_commands") or []):
                    if sub.get("enabled"):
                        grouped[plugin].append(label(sub, "    "))

        walk(commands)
        if not grouped:
            event.set_result(MessageEventResult().message("（没有任何已注册指令）"))
            event.stop_event()
            return
        blocks = [
            f"【{name}】\n" + "\n".join(grouped[name])
            for name in order
        ]
        listing = "全部指令清单：\n" + "\n\n".join(blocks)
        # 群聊：清单私发给发言人，群里不刷屏；私发失败才回群里 @ 一句
        if event.get_group_id() \
                and str(event.get_platform_name() or "").lower() == "aiocqhttp" \
                and getattr(event, "bot", None) is not None:
            try:
                await event.bot.call_action(
                    "send_private_msg",
                    user_id=int(event.get_sender_id() or 0),
                    message=listing,
                )
            except Exception:
                try:
                    await event.bot.call_action(
                        "send_group_msg",
                        group_id=int(event.get_group_id() or 0),
                        message=[
                            {"type": "at", "data": {"qq": str(event.get_sender_id() or "")}},
                            {"type": "text",
                             "data": {"text": " 指令清单私发失败（可能不是好友/关了临时会话），私聊我发 /指令 可见"}},
                        ],
                    )
                except Exception as e2:  # noqa: BLE001
                    logger.warning(f"[provider_router] /指令 提示发送失败: {type(e2).__name__}: {e2}")
            event.stop_event()
            return
        event.set_result(
            MessageEventResult().message(listing).use_t2i(True)
        )
        event.stop_event()

    def _current_id(self, umo: str) -> str:
        try:
            current = self.context.get_using_provider(umo)
            return current.meta().id if current else ""
        except Exception:  # noqa: BLE001
            return ""

    def _write_binding(self, event: AstrMessageEvent, key: str, value: str | None) -> None:
        """把聊天指令里的手动切换写进 config.json 绑定表——和在中枢控制台里改完全等价。

        手动切换 = 控制台修改：谁后写谁生效。value=None 表示删除该群/好友的绑定，回默认。
        key 取 group_* / user_* 两族映射，按当前是群还是私聊自动选。
        """
        ent_id = str(event.get_group_id() or event.get_sender_id() or "")
        if not ent_id:
            return
        if key not in ("group_provider_ids", "user_provider_ids", "group_persona_ids",
                       "user_persona_ids", "group_provider_backup_ids", "user_provider_backup_ids"):
            logger.warning(f"[provider_router] 不支持的绑定键: {key}")
            return
        try:
            with open(self._cfg_path, encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            mapping = data.get(key)
            if not isinstance(mapping, dict):
                mapping = {}
            if value:
                mapping[ent_id] = value
            else:
                mapping.pop(ent_id, None)
            data[key] = mapping
            tmp = f"{self._cfg_path}.tmp"
            with open(tmp, "w", encoding="utf-8-sig") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(tmp, self._cfg_path)
            self.cfg[key] = mapping
            try:
                self._cfg_mtime = os.path.getmtime(self._cfg_path)
            except OSError:
                pass
            logger.info(f"[provider_router] 绑定已写入 {key}[{ent_id}] = {value or '（删除）'}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[provider_router] 写入绑定失败: {type(e).__name__}: {e}")

    def _binding_map_key(self, event: AstrMessageEvent, model: bool = True) -> str:
        """当前会话对应的绑定表键名：群还是私聊、模型还是人格。"""
        if model:
            return "group_provider_ids" if event.get_group_id() else "user_provider_ids"
        return "group_persona_ids" if event.get_group_id() else "user_persona_ids"

    def _scope_label(self, event: AstrMessageEvent) -> str:
        return "本群" if event.get_group_id() else "该好友会话"

    @staticmethod
    def _clean_arg(text: str) -> str:
        """去掉零宽等不可见字符和行首身份标签。

        从聊天窗口复制指令常带 U+200B/U+FEFF，会让参数匹配不上；另外 face_convert
        会在 message_str 前插入 "[主人(QQ)] " 这类身份标签，且它的 handler 可能
        先于本插件执行，不清掉标签命令名就匹配不到行首。
        """
        text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", text or "")
        text = re.sub(r"^(?:\s*\[[^\[\]]{0,80}\])+\s*", "", text)
        return text.strip()

    def _command_args(self, event: AstrMessageEvent, name: str) -> str:
        """取命令后面的参数：先清身份标签/零宽字符，再剥掉命令名本身。"""
        text = self._clean_arg(str(getattr(event, "message_str", "") or ""))
        return re.sub(rf"^[\s/！!]*{re.escape(name)}", "", text, flags=re.I).strip()


    # ---------- 角色切换（/角色） ----------
    def _persona_list(self) -> list[str]:
        """AstrBot 面板里建好的全部人格 id。"""
        try:
            return [str(p.persona_id) for p in (self.context.persona_manager.personas or [])]
        except Exception:  # noqa: BLE001
            return []

    async def _session_persona(self, umo: str) -> str:
        try:
            from astrbot.core import sp

            cfg = (
                await sp.get_async(
                    scope="umo", scope_id=umo, key="session_service_config", default={}
                )
                or {}
            )
            return str(cfg.get("persona_id") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _console_binding(self, event: AstrMessageEvent) -> str:
        """控制台（config.json）对这个群 / 好友绑的人格，没有则空串。"""
        group_id = str(event.get_group_id() or "")
        key = group_id or str(event.get_sender_id() or "")
        if not key:
            return ""
        mapping = (
            self.cfg.get("group_persona_ids") if group_id else self.cfg.get("user_persona_ids")
        ) or {}
        return str(mapping.get(key) or "") if isinstance(mapping, dict) else ""

    async def _reset_bridge_session(self, umo: str) -> None:
        """dsh 主脑模式下换角色：顺手重置桥接会话，避免旧人设的对话记忆串味。

        纯尽力而为：桥没起、token 变了都无所谓，角色本身下一轮照常生效。
        """
        try:
            import httpx

            token = ""
            try:
                with open(os.path.expanduser("~/.dsh/qq-bridge-token.txt"), encoding="utf-8") as f:
                    token = f.read().strip()
            except Exception:  # noqa: BLE001
                token = ""
            base = "http://127.0.0.1:18081"
            for p in self._providers():
                try:
                    if str(p.meta().id).lower().startswith("dsh"):
                        base = str(getattr(p, "api_base", "") or base).rstrip("/")
                        break
                except Exception:  # noqa: BLE001
                    continue
            headers = {"Authorization": "Bearer " + token} if token else {}
            async with httpx.AsyncClient(timeout=4.0) as client:
                await client.post(
                    base + "/reset", json={"conversationId": "provider:" + umo}, headers=headers
                )
            logger.info(f"[provider_router] 已重置 dsh 桥会话：provider:{umo}")
        except Exception as e:  # noqa: BLE001
            logger.info(f"[provider_router] 桥接会话重置跳过: {type(e).__name__}: {e}")

    @filter.command("角色")
    async def cmd_persona(self, event: AstrMessageEvent) -> None:
        """/角色 [编号|名字|重置] —— 看角色列表；换角色仅主人。"""
        self._maybe_reload()
        if self._block_reason(event):
            event.stop_event()
            return
        umo = event.unified_msg_origin
        raw = self._command_args(event, "角色")
        roles = self._persona_list()
        current = await self._session_persona(umo)
        if not raw or raw.lower() in ("列表", "list", "?"):
            lines = [f"当前角色：{current or '（默认）'}"]
            binding = self._console_binding(event)
            if binding:
                lines.append(f"控制台绑定：{binding}")
            if roles:
                lines.append("可用角色：\n" + "\n".join(
                    f"{i}. {name}" for i, name in enumerate(roles, 1)
                ))
            else:
                lines.append("（还没有建人格，去 AstrBot 面板 → 配置 → 人格情景新建）")
            lines.append("换角色：/角色 编号或名字（仅主人） · 回默认：/角色 重置")
            await self._priv_result(event, "\n".join(lines))
            return
        if not event.is_admin():
            await self._priv_result(event, "换角色仅主人。")
            return
        norm = raw.lower()
        if norm in ("重置", "解绑", "默认", "自动", "清除", "reset", "auto"):
            try:
                from astrbot.core import sp

                session_cfg = (
                    await sp.get_async(
                        scope="umo", scope_id=umo, key="session_service_config", default={}
                    )
                    or {}
                )
                session_cfg.pop("persona_id", None)
                await sp.put_async(
                    scope="umo", scope_id=umo,
                    key="session_service_config", value=session_cfg,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[provider_router] 解除角色失败: {type(e).__name__}: {e}")
            self._write_binding(event, self._binding_map_key(event, model=False), None)
            self._persona_applied.pop(umo, None)
            self._save_state()
            asyncio.create_task(self._reset_bridge_session(umo))
            await self._priv_result(
                event, f"角色已重置（{self._scope_label(event)}绑定已清除），按默认人格说话"
            )
            return
        target = ""
        if norm.isdigit():
            idx = int(norm)
            if 1 <= idx <= len(roles):
                target = roles[idx - 1]
        else:
            exact = [r for r in roles if r.lower() == norm]
            hits = [r for r in roles if norm in r.lower()]
            if len(exact) == 1:
                target = exact[0]
            elif len(hits) > 1:
                await self._priv_result(
                    event, "匹配到好几个：" + "、".join(hits) + "，用编号或完整名字"
                )
                return
            target = hits[0] if not target and len(hits) == 1 else target
        if not target:
            await self._priv_result(event, "没找到这个角色。/角色 看一下列表。")
            return
        if target == current:
            await self._priv_result(event, f"现在就已经是「{target}」。")
            return
        try:
            from astrbot.core import sp

            session_cfg = (
                await sp.get_async(
                    scope="umo", scope_id=umo, key="session_service_config", default={}
                )
                or {}
            )
            session_cfg["persona_id"] = target
            await sp.put_async(
                scope="umo", scope_id=umo, key="session_service_config", value=session_cfg
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[provider_router] 切换角色失败: {type(e).__name__}: {e}")
            await self._priv_result(event, f"没切成（{type(e).__name__}）。")
            return
        # 手动切换 = 控制台修改：写进绑定表，谁后写谁生效
        self._write_binding(event, self._binding_map_key(event, model=False), target)
        self._persona_applied[umo] = target
        self._save_state()
        asyncio.create_task(self._reset_bridge_session(umo))
        await self._priv_result(
            event,
            f"{self._scope_label(event)}角色已切到「{target}」并写入控制台绑定，下一条消息生效。想回去发 /角色 重置。",
        )


    # ---------- 主备模型探针降级 ----------
    def _probe_set(self) -> list[str]:
        """要探活的档位：所有绑定（主 + 备）里出现过的 provider id。"""
        ids: set[str] = set()
        for key in ("group_provider_ids", "user_provider_ids",
                    "group_provider_backup_ids", "user_provider_backup_ids"):
            mapping = self.cfg.get(key) or {}
            if isinstance(mapping, dict):
                ids |= {str(v).strip() for v in mapping.values() if str(v).strip()}
        return sorted(ids)

    def _usable(self, pid: str) -> bool:
        """没被探针判定坏掉 = 可用。坏掉的要等探针重新成功才恢复。"""
        return pid not in self._bad

    async def _probe_one(self, pid: str) -> None:
        provider = self.context.get_provider_by_id(pid)
        if not provider:
            self._bad.add(pid)
            return
        if str(pid).lower().startswith("dsh"):
            # dsh 桥后面是本地 harness，真实对话探针又慢又贵还容易误判：
            # 只看桥的 /health，ready 就算可用。
            alive = False
            try:
                import httpx

                base = str(getattr(provider, "api_base", "") or "http://127.0.0.1:18081").rstrip("/")
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(base + "/health")
                data = resp.json() if resp.status_code == 200 else {}
                alive = bool(data.get("ok", True)) and bool(data.get("ready", True))
            except Exception as e:  # noqa: BLE001
                logger.info(f"[provider_router] dsh 桥探针失败（{type(e).__name__}）")
            if alive:
                self._bad.discard(pid)
                self._next_probe[pid] = time.time() + float(self.cfg["health_probe_interval"])
            else:
                self._bad.add(pid)
                self._next_probe[pid] = time.time() + float(self.cfg["health_cooldown"])
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
            for pid in self._probe_set():
                if time.time() < self._next_probe.get(pid, 0.0):
                    continue  # 还在冷却，先不打扰它
                await self._probe_one(pid)
            self._probed_once.set()
            await asyncio.sleep(float(self.cfg["health_probe_interval"]))

    def _ensure_probe(self) -> None:
        if not self.cfg.get("health_probe_enabled", True) or not self._probe_set():
            self._probed_once.set()
            return
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop())

    async def initialize(self) -> None:
        """插件加载时就跑一轮探针，避免第一条群消息只能落到本地。"""
        if self.cfg.get("health_probe_enabled", True) and self._probe_set():
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
        """微信私聊锁定人格；微信群交给中枢的群映射，避免中枢解绑不掉。

        用户用 /persona 手动指定过的不覆盖。
        """
        persona_id = str(self.cfg.get("wechat_persona_id") or "").strip()
        if not persona_id or not self._is_wechat(event) or event.get_group_id():
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
                if wanted == current:
                    # 已经一致（可能是重启丢了 applied 记录）：补记录并解除手动标记
                    self._persona_applied[umo] = wanted
                    self._manual_pinned.discard(umo)
                    return
                if umo in self._manual_pinned and wanted == applied:
                    # 用户手动 /persona 过且中枢映射没变：尊重手动指定
                    return
                session_cfg["persona_id"] = wanted
                self._persona_applied[umo] = wanted
                self._manual_pinned.discard(umo)
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
            return str(self.cfg.get("group_provider_id") or "")
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
        # 按人 / 按群固定了主模型，就不再参与大小模型自动分流；
        # 主档被探针判坏时切这个会话的备用档，没有备用就仍然走主档
        main, backup = self._pinned_pair(event)
        if main:
            self._ensure_probe()
            if self._usable(main):
                return main
            if backup:
                logger.info(f"[provider_router] 主模型 {main} 探针不可用，本会话切备用 {backup}")
                return backup
            return main
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
        had_wake = False
        for wp in (wake_cfg.get("wake_prefix") or ["/"]):
            if wp and raw.startswith(wp):
                raw = raw[len(wp):].strip()
                had_wake = True
                break
        m = _DEAD_CMD_RE.match(raw)
        if m and m.group(1).lower() in _DEAD_CMDS:
            event.stop_event()
            return
        if had_wake and (raw == "persona" or raw.startswith("persona ")):
            # 用户手动 /persona：记下这个会话，控制台映射不再抢着覆盖；
            # 之后中枢在控制台里再显式切换（映射值变了）会覆盖并解除标记
            umo = event.unified_msg_origin
            if umo not in self._manual_pinned:
                self._manual_pinned.add(umo)
                self._save_state()
                logger.info(f"[provider_router] {umo} 手动 /persona，控制台映射暂停覆盖")
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
