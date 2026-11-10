# -*- coding: utf-8 -*-
"""提示词体积审计（只读）。

每次 LLM 请求把各段字符数写进 ``data/logs/prompt_audit.jsonl``，一行一条：
system_prompt / extra_user_content_parts / 历史 contexts / 工具 schema 序列化长度，
再加几个哈希用于判断「前缀是否稳定」（DeepSeek 前缀缓存命中依赖前缀不变）。

本插件**只统计，不改请求**。为了拿到「所有插件都改完之后」的最终请求，
它在 AstrBot 全部插件加载完成后把自己挂到 on_llm_request 队列末尾。
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.dirname(os.path.dirname(PLUGIN_DIR))  # .../data
LOG_PATH = os.path.join(DATA_DIR, "logs", "prompt_audit.jsonl")

# system_prompt 里各插件追加段的识别标记（顺序即拼接顺序）
SECTION_MARKS = (
    ("persona", "# Persona Instructions"),
    ("skills", "## Skills"),
    ("owner_guard", "【主人保护】"),
    ("small_model_guide", "【小模型工具用法】"),
    ("sticker", "【表情】"),
)


def _text_of(part) -> str:
    text = getattr(part, "text", None)
    if isinstance(text, str):
        return text
    return str(part or "")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]


def _sections(sp: str) -> dict[str, int]:
    """按标记切出 system_prompt 各段字符数（找不到的段记 0）。"""
    hits = []
    for name, mark in SECTION_MARKS:
        idx = sp.find(mark)
        if idx >= 0:
            hits.append((idx, name))
    hits.sort()
    out: dict[str, int] = {}
    for i, (start, name) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(sp)
        out[name] = end - start
    return out


def _tool_schema_bytes(func_tool) -> int:
    """OpenAI schema 序列化的字符数。"""
    if func_tool is None:
        return 0
    try:
        schema = func_tool.openai_schema(omit_empty_parameter_field=True)
    except Exception:  # noqa: BLE001
        schema = [
            {
                "type": "function",
                "function": {
                    "name": str(getattr(t, "name", "")),
                    "description": str(getattr(t, "description", "") or ""),
                    "parameters": getattr(t, "parameters", None),
                },
            }
            for t in (getattr(func_tool, "tools", []) or [])
        ]
    return len(json.dumps(schema, ensure_ascii=False, default=str))


def _tool_stats(req) -> tuple[int, int, int, str]:
    """返回 (工具个数, full schema 字符数, light schema 字符数, 工具名排序哈希)。"""
    func_tool = getattr(req, "func_tool", None)
    tools = list(getattr(func_tool, "tools", []) or []) if func_tool else []
    if not tools:
        return 0, 0, 0, ""
    full_chars = _tool_schema_bytes(func_tool)
    light_chars = 0
    try:
        light_chars = _tool_schema_bytes(func_tool.get_light_tool_set())
    except Exception:  # noqa: BLE001
        light_chars = 0
    names = ",".join(sorted(str(getattr(t, "name", "")) for t in tools))
    return len(tools), full_chars, light_chars, _sha(names)


@register(
    "astrbot_plugin_prompt_audit",
    "dafeiyu-local",
    "只读审计：记录每次 LLM 请求各段字符数与前缀哈希，用于前缀缓存优化",
    "v1.0.0",
)
class PromptAudit(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._installed = False

    @filter.on_astrbot_loaded()
    async def _install_last(self):
        """把自己的 on_llm_request 挪到注册表末尾，保证读到最终请求。"""
        if self._installed:
            return
        try:
            from astrbot.core.star.star_handler import (
                EventType,
                StarHandlerMetadata,
                star_handlers_registry,
            )

            full_name = f"{__name__}_audit_llm_request"
            old = star_handlers_registry.get_handler_by_full_name(full_name)
            if old is not None:
                star_handlers_registry.remove(old)
            star_handlers_registry.append(
                StarHandlerMetadata(
                    event_type=EventType.OnLLMRequestEvent,
                    handler_full_name=full_name,
                    handler_name="audit_llm_request",
                    handler_module_path=__name__,
                    handler=self.audit_llm_request,
                    event_filters=[],
                )
            )
            self._installed = True
            logger.info("[prompt_audit] 审计钩子已挂到 on_llm_request 末尾")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[prompt_audit] 挂载钩子失败: {type(e).__name__}: {e}")

    async def audit_llm_request(self, event: AstrMessageEvent, req) -> None:
        """只统计字符数并落盘，不修改 req。"""
        try:
            sp = getattr(req, "system_prompt", "") or ""
            if not isinstance(sp, str):
                sp = str(sp)

            parts = list(getattr(req, "extra_user_content_parts", []) or [])
            part_texts = [_text_of(p) for p in parts]
            parts_chars = sum(len(t) for t in part_texts)

            ctx = getattr(req, "contexts", []) or []
            if isinstance(ctx, str):
                ctx_chars = len(ctx)
                ctx_count = -1
            else:
                ctx_chars = len(json.dumps(ctx, ensure_ascii=False, default=str))
                ctx_count = len(ctx)

            tool_count, tool_chars, tool_chars_light, tool_names_hash = _tool_stats(req)

            prompt = getattr(req, "prompt", "") or ""
            if not isinstance(prompt, str):
                prompt = str(prompt)

            umo = str(getattr(event, "unified_msg_origin", "") or "")
            try:
                is_group = bool(event.get_group_id())
            except Exception:  # noqa: BLE001
                is_group = False

            record = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "umo": umo,
                "group": is_group,
                "model": str(getattr(req, "model", "") or ""),
                "chars": {
                    "system_prompt": len(sp),
                    "extra_parts": parts_chars,
                    "extra_parts_count": len(parts),
                    "contexts": ctx_chars,
                    "contexts_count": ctx_count,
                    "prompt": len(prompt),
                    "tool_schema": tool_chars,
                    "tool_schema_light": tool_chars_light,
                    "total": len(sp) + parts_chars + ctx_chars + len(prompt) + tool_chars,
                },
                "tools": tool_count,
                "sections": _sections(sp),
                "hashes": {
                    "system_prompt": _sha(sp),
                    "sys_head_4k": _sha(sp[:4000]),
                    "tool_names": tool_names_hash,
                    "extra_parts": _sha("".join(part_texts)),
                },
                "part_heads": [t[:40] for t in part_texts],
            }
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

            # 人工排查用：存在开关文件时把完整 system prompt 落到单独文件
            dump_flag = os.path.join(os.path.dirname(LOG_PATH), "prompt_audit_dump.on")
            if os.path.exists(dump_flag):
                with open(
                    os.path.join(os.path.dirname(LOG_PATH), "prompt_audit_sample.txt"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    f.write(sp)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[prompt_audit] 记录失败: {type(e).__name__}: {e}")
