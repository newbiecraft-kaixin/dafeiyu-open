# -*- coding: utf-8 -*-
"""提示词前缀优化（只动 system prompt 的冗余段，不改功能）。

做两件事：

1. **删掉用不上的技能清单**
   AstrBot 在 ``computer_use_runtime`` 非 local/sandbox 时，仍会把内置技能
   清单（documents / pdf / skill-creator / spreadsheets，约 3.4k 字符）塞进
   system prompt，紧接着又写明「User has not enabled the Computer Use
   feature」，即这些技能根本无法调用。QQ 聊天场景下这部分纯属浪费，
   本插件在它出现时整段移除；一旦开启 computer use（runtime=local/sandbox）
   则原样保留，技能功能不受影响。

2. **顺序守卫**
   DeepSeek 前缀缓存要求请求前缀稳定。理想顺序：
   静态前缀（安全声明/人设）→ 半静态（技能说明/工具约定）→ 动态尾部
   （主人保护、当前时间、群窗口、记忆召回）。
   AstrBot 的动态内容本来就都追加在末尾（或 extra_user_content_parts），
   这里只做兜底检查：一旦发现动态段跑到静态段之前就搬到末尾，并记日志。

本插件自身不产生新的动态内容，也不改任何回复语义。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# 这些运行时下技能真正可用，此时不裁剪
SKILL_RUNTIMES = {"local", "sandbox"}

# QQ 聊天里用不到的内置技能清单，整段标记
SKILLS_BLOCK_START = "## Skills"
SKILLS_BLOCK_END = "User has not enabled the Computer Use feature."
SKILLS_BLOCK_TAIL = "WebUI -> Config."

# 动态段标记（出现即视为应位于 system prompt 尾部）
DYNAMIC_MARKS = (
    "【主人保护】",
    "【小模型工具用法】",
    "【表情】",
    "【群聊动态窗口】",
    "【你记得的这个人】",
)

STATIC_ANCHOR = "# Persona Instructions"


def _get_runtime(config: dict) -> str:
    ar = (config or {}).get("agent_runner") or {}
    inner = ar.get("config") or {}
    return str(inner.get("computer_use_runtime", "none") or "none").lower()


def _strip_skill_block(sp: str) -> tuple[str, int]:
    """移除「## Skills ... WebUI -> Config.」整段，返回 (新文本, 省下的字符数)。"""
    start = sp.find("\n" + SKILLS_BLOCK_START)
    if start < 0:
        start = sp.find(SKILLS_BLOCK_START)
    if start < 0:
        return sp, 0
    end_mark = sp.find(SKILLS_BLOCK_END, start)
    if end_mark < 0:
        return sp, 0
    tail = sp.find(SKILLS_BLOCK_TAIL, end_mark)
    end = tail + len(SKILLS_BLOCK_TAIL) if tail >= 0 else end_mark + len(SKILLS_BLOCK_END)
    head = sp[:start].rstrip("\n")
    rest = sp[end:].lstrip("\n")
    new_sp = head + ("\n\n" + rest if rest else "")
    return new_sp, len(sp) - len(new_sp)


def _dynamic_segment_range(sp: str, mark_index: int) -> int:
    """动态段结束位置：下一个已知标记或文本末尾。"""
    end = len(sp)
    for other in DYNAMIC_MARKS:
        pos = sp.find(other, mark_index + 1)
        if pos > mark_index:
            end = min(end, pos)
    return end


def _enforce_dynamic_tail(sp: str) -> tuple[str, bool]:
    """若动态段出现在静态锚点之前，则整段搬到末尾。返回 (新文本, 是否改动)。"""
    anchor = sp.find(STATIC_ANCHOR)
    if anchor < 0:
        return sp, False
    moved = []
    changed = False
    for mark in DYNAMIC_MARKS:
        pos = sp.find(mark)
        if pos < 0 or pos > anchor:
            continue
        end = _dynamic_segment_range(sp, pos)
        if end >= len(sp) - 1:
            # 该段一直延续到结尾，无法安全搬移，跳过
            continue
        moved.append(sp[pos:end])
        sp = sp[:pos] + sp[end:]
        changed = True
    if moved:
        sp = sp.rstrip("\n") + "\n\n" + "\n".join(m.rstrip("\n") for m in moved)
    return sp, changed


@register(
    "astrbot_plugin_prompt_cache",
    "dafeiyu-local",
    "裁剪用不上的技能清单并保证 system prompt 动态段位于尾部，稳定 DeepSeek 前缀缓存",
    "v1.0.0",
)
class PromptCache(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._installed = False
        self._logged_strip = False

    @filter.on_astrbot_loaded()
    async def _install_last(self):
        """把钩子挂到 on_llm_request 队列末尾（审计插件再排它后面）。"""
        if self._installed:
            return
        try:
            from astrbot.core.star.star_handler import (
                EventType,
                StarHandlerMetadata,
                star_handlers_registry,
            )

            registry = star_handlers_registry
            audit_md = None
            for md in list(getattr(registry, "_handlers", []) or []):
                if getattr(md, "handler_name", "") == "audit_llm_request":
                    audit_md = md
                    break

            full_name = f"{__name__}_optimize_system_prompt"
            old = registry.get_handler_by_full_name(full_name)
            if old is not None:
                registry.remove(old)
            registry.append(
                StarHandlerMetadata(
                    event_type=EventType.OnLLMRequestEvent,
                    handler_full_name=full_name,
                    handler_name="optimize_system_prompt",
                    handler_module_path=__name__,
                    handler=self.optimize_system_prompt,
                    event_filters=[],
                )
            )
            # 审计插件负责「最终值」，必须排在本插件之后
            if audit_md is not None:
                registry.remove(audit_md)
                registry.append(audit_md)
            self._installed = True
            logger.info("[prompt_cache] 钩子已挂载（裁剪技能清单 + 动态段顺序守卫）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[prompt_cache] 挂载钩子失败: {type(e).__name__}: {e}")

    async def optimize_system_prompt(self, event: AstrMessageEvent, req) -> None:
        try:
            sp = getattr(req, "system_prompt", "") or ""
            if not isinstance(sp, str) or not sp:
                return

            cfg = {}
            try:
                cfg = self.context.get_config(umo=event.unified_msg_origin)
            except TypeError:
                cfg = self.context.get_config()
            except Exception:  # noqa: BLE001
                cfg = {}

            runtime = _get_runtime(cfg)
            if runtime not in SKILL_RUNTIMES:
                sp, saved = _strip_skill_block(sp)
                if saved and not self._logged_strip:
                    logger.info(f"[prompt_cache] 移除不可用技能清单，省 {saved} 字符")
                    self._logged_strip = True

            sp, moved = _enforce_dynamic_tail(sp)
            if moved:
                logger.warning("[prompt_cache] 检测到动态段位于静态段之前，已搬到末尾")

            req.system_prompt = sp
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[prompt_cache] 处理失败: {type(e).__name__}: {e}")
