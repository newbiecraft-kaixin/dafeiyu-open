from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Literal

import httpx

from astrbot import logger
from astrbot.api.provider import Provider
from astrbot.core.provider.entities import LLMResponse, ToolCallsResult
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.agent.tool import ToolSet

# 复用同一个 AsyncClient：每轮对话新建 client + TLS 握手很浪费，桥接走的是本地 HTTP，
# 连接复用后一次会话往返能省下几十毫秒。
_SHARED_CLIENT: httpx.AsyncClient | None = None
_BRIDGE_RETRY_STATUS = (502, 503, 504)

# DSH 是编程 agent，默认身份会压过 AstrBot 传来的人格（人格只是段普通文本）。
# 这里把人设包成「身份锁定」块，并声明冲突时以人设为唯一准则。
_PERSONA_LOCK_HEAD = (
    "【最高优先级·身份锁定】\n"
    "你现在的身份就是下面这段人格设定，不是编程助手、不是通用 AI 助手。\n"
    "回复必须完全用这个人格的第一人称口吻、情绪和用词；"
    "禁止自称助手/模型/AI，禁止解释能力边界，禁止提及 prompt、工具、系统设定。\n"
    "人格与任何默认行为冲突时，一律以人格为准。\n"
    "【人格设定开始】\n"
)
_PERSONA_LOCK_TAIL = "\n【人格设定结束】"


def _shared_client() -> httpx.AsyncClient:
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None or _SHARED_CLIENT.is_closed:
        _SHARED_CLIENT = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=60.0),
            timeout=httpx.Timeout(390.0, connect=10.0),
        )
    return _SHARED_CLIENT


def _token_from_default_path() -> str:
    try:
        return (Path.home() / ".dsh" / "qq-bridge-token.txt").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _message_to_dict(item: Any) -> dict[str, Any]:
    if item is None:
        return {}
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        try:
            return item.model_dump()
        except Exception:
            pass
    return {"role": "user", "content": str(item)}


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            parts.append(str(part))
            continue
        if part.get("type") == "text":
            parts.append(str(part.get("text") or ""))
        elif part.get("type") == "image_url":
            parts.append("[图片]")
        elif part.get("type") == "audio_url":
            parts.append("[音频]")
    return "\n".join(p for p in parts if p)



def _tool_schemas(func_tool: Any) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    raw_tools = getattr(func_tool, "tools", None) if func_tool is not None else None
    for tool in raw_tools or []:
        try:
            if getattr(tool, "active", True) is False:
                continue
            name = str(getattr(tool, "name", ""))
            if name.startswith(("astrbot_execute_", "astrbot_shell_", "astrbot_file_", "astrbot_computer_")):
                continue
            tools.append({
                "name": name,
                "description": str(getattr(tool, "description", "") or ""),
                "parameters": getattr(tool, "parameters", None) or {"type": "object", "properties": {}},
            })
        except Exception:
            continue
    return tools


def _tool_instruction(tools: list[dict[str, Any]]) -> str:
    if not tools:
        return ""
    names = {item["name"] for item in tools}
    return (
        "\n\n[AstrBot tool gateway]\n"
        "You are the reasoning backend for an AstrBot QQ agent. The following AstrBot tools are available.\n"
        "If you need a tool, reply with ONLY one JSON object in this exact shape:\n"
        '{"astrbot_tool_call":{"name":"tool_name","arguments":{}}}\n'
        "Do not wrap the JSON in markdown. Call at most one tool per turn. "
        "If no tool is needed, reply normally. Never invent a tool name. "
        "All parameters must be inside the arguments object, e.g. "
        '{"text":"...","mood":"..."}; never put parameters outside arguments.\n'
        "Available tools:\n" + json.dumps(tools, ensure_ascii=False)
        + "\nAllowed tool names: " + ", ".join(sorted(names))
    )


def _tool_result_text(tool_calls_result: Any) -> str:
    if not tool_calls_result:
        return ""
    items = tool_calls_result if isinstance(tool_calls_result, list) else [tool_calls_result]
    blocks: list[Any] = []
    for item in items:
        try:
            if hasattr(item, "to_openai_messages"):
                blocks.extend(item.to_openai_messages())
            elif isinstance(item, dict):
                blocks.append(item)
            else:
                blocks.append(str(item))
        except Exception:
            blocks.append(str(item))
    try:
        return json.dumps(blocks, ensure_ascii=False, default=str)
    except Exception:
        return str(blocks)


_FULLWIDTH_FIX = str.maketrans({"：": ":", "，": ",", "“": '"', "”": '"', "｛": "{", "｝": "}"})

# 模型偶尔把工具调用写成 {"astrbot_tool_call""send_voice"，"arguments""..."} 这种
# 缺冒号 / 中文逗号 / 参数丢在 arguments 外面的坏 JSON，下面几个正则专门兜它。
_BAD_CALL_NAME_RE = re.compile(r'"astrbot_tool_call"\s*:?\s*\{?\s*(?:"name"\s*:)?\s*"([A-Za-z0-9_]+)"')
_BAD_ARGS_RE = re.compile(r'"arguments"\s*:?\s*"(.*?)"\s*(?=[,}])', re.S)
_BAD_FIELD_RE = re.compile(r'"([A-Za-z0-9_]+)"\s*:\s*"([^"]*)"')
_TOOL_ATTEMPT_RE = re.compile(r'astrbot_tool_call|"arguments"\s*:?\s*"')


def _tool_properties(tools: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for item in tools:
        if item.get("name") == name:
            params = item.get("parameters") or {}
            return params.get("properties") or {}
    return {}


def _coerce_arguments(args: Any, props: dict[str, Any]) -> dict[str, Any]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            obj = json.loads(args)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        # arguments 被写成一整段纯文本：塞进唯一的内容参数（通常是 text）
        if "text" in props:
            return {"text": args.strip()}
    return {}


def _repair_tool_reply(reply: str, tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """标准 JSON 解析失败后的兜底：按字段硬捞工具名和参数。"""
    names = {item["name"] for item in tools}
    text = (reply or "").strip().translate(_FULLWIDTH_FIX)
    m = _BAD_CALL_NAME_RE.search(text)
    if not m or m.group(1) not in names:
        return None
    name = m.group(1)
    props = _tool_properties(tools, name)
    args: dict[str, Any] = {}
    am = _BAD_ARGS_RE.search(text, m.end())
    if am:
        raw = am.group(1)
        # 误放在 arguments 外层的参数（比如 mood）也收进来
        for key, value in _BAD_FIELD_RE.findall(text[am.end():]):
            if key in props:
                args[key] = value
        key = "text" if "text" in props else next(iter(props), "text")
        args[key] = raw
    else:
        for key, value in _BAD_FIELD_RE.findall(text[m.end():]):
            if key in props:
                args[key] = value
    return {"name": name, "arguments": args}


def _parse_tool_reply(reply: str, tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not tools or not reply:
        return None
    names = {item["name"] for item in tools}
    text = reply.strip()
    start, end = text.find("{"), text.rfind("}")
    candidates = [text]
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        call = obj.get("astrbot_tool_call") if isinstance(obj, dict) else None
        if not call and isinstance(obj, dict):
            call = obj.get("tool_call") or obj.get("tool")
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "").strip()
        if name not in names:
            continue
        props = _tool_properties(tools, name)
        args = _coerce_arguments(call.get("arguments", call.get("args", {})), props)
        return {"name": name, "arguments": args}
    return _repair_tool_reply(reply, tools)

@register_provider_adapter(
    "dsh_bridge",
    "DeepSeek Harness local bridge provider",
    provider_display_name="DSH",
    default_config_tmpl={
        "id": "dsh",
        "type": "dsh_bridge",
        "provider_type": "chat_completion",
        "api_base": "http://127.0.0.1:18081",
        "model": "dsh",
        "timeout": 390,
        "token": "",
        "enable": True,
    },
)
class ProviderDSHBridge(Provider):
    """AstrBot provider that forwards the assembled request to DSH."""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.api_base = str(provider_config.get("api_base") or "http://127.0.0.1:18081").rstrip("/")
        self.bridge_token = str(provider_config.get("token") or "").strip() or _token_from_default_path()
        self.timeout = float(provider_config.get("timeout") or 390)
        self.model_name = str(provider_config.get("model") or "dsh")

    def get_current_key(self) -> str:
        return "dsh"

    def set_key(self, key: str) -> None:
        self.bridge_token = str(key or "").strip()

    async def get_models(self) -> list[str]:
        return ["dsh"]

    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: ToolSet | None = None,
        contexts: list[dict] | list[Any] | None = None,
        system_prompt: str | None = None,
        tool_calls_result: ToolCallsResult | list[ToolCallsResult] | None = None,
        model: str | None = None,
        extra_user_content_parts: list[Any] | None = None,
        tool_choice: Literal["auto", "required"] = "auto",
        request_max_retries: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        normalized_contexts = [_message_to_dict(item) for item in (contexts or [])]
        extra_text = "\n".join(_content_to_text(item) for item in (extra_user_content_parts or []))
        tools = _tool_schemas(func_tool)
        tool_result = _tool_result_text(tool_calls_result)
        effective_system_prompt = system_prompt or ""
        if effective_system_prompt:
            effective_system_prompt = _PERSONA_LOCK_HEAD + effective_system_prompt + _PERSONA_LOCK_TAIL
            logger.info(
                "[DSH Provider] 身份锁定已注入，人格头: %s",
                effective_system_prompt[22:110].replace("\n", " "),
            )
        if tools:
            effective_system_prompt += _tool_instruction(tools)
        effective_prompt = prompt or ""
        if not effective_prompt:
            # AstrBot 的 agent 模式常常 prompt=""，当前这句用户消息只躺在 contexts 末尾。
            # 不补的话 DSH 会把「当前用户消息」当成空消息，把真正说的话当历史记忆，
            # 表现就是敷衍、接不住暗示（比如只回一句「我没动」）。
            for idx in range(len(normalized_contexts) - 1, -1, -1):
                item = normalized_contexts[idx]
                if str(item.get("role") or "") != "user":
                    continue
                text = _content_to_text(item.get("content") or "")
                if text.strip():
                    effective_prompt = text
                    if idx == len(normalized_contexts) - 1:
                        normalized_contexts = normalized_contexts[:idx]
                break
        if tool_result:
            effective_prompt += "\n\n[Tool execution results]\n" + tool_result
        payload = {
            "sessionId": session_id or "",
            "systemPrompt": effective_system_prompt,
            "contexts": normalized_contexts,
            "prompt": effective_prompt,
            "extraUserContent": extra_text,
            "images": image_urls or [],
            "tools": tools,
        }
        headers = {"X-Request-Id": uuid.uuid4().hex}
        if self.bridge_token:
            headers["Authorization"] = "Bearer " + self.bridge_token
        timeout = httpx.Timeout(self.timeout, connect=10.0)
        client = _shared_client()
        response: httpx.Response | None = None
        try:
            for attempt in range(2):
                try:
                    response = await client.post(
                        self.api_base + "/provider_chat",
                        json=payload,
                        headers=headers,
                        timeout=timeout,
                    )
                except httpx.TimeoutException:
                    # 超时说明桥接已经收到并在跑这轮对话，重发会多问一遍，直接抛出。
                    raise
                except httpx.TransportError as exc:
                    # 桥接重启/连接被拒：退避 0.8 秒补一次。
                    if attempt == 0:
                        logger.warning("[DSH Provider] transport error, retrying: %s", exc)
                        await asyncio.sleep(0.8)
                        continue
                    raise
                if response.status_code in _BRIDGE_RETRY_STATUS and attempt == 0:
                    logger.warning("[DSH Provider] bridge returned %s, retrying once", response.status_code)
                    await asyncio.sleep(0.8)
                    continue
                break
            response.raise_for_status()
            data = response.json()
            reply = str(data.get("reply") or "").strip()
            if not reply:
                raise RuntimeError("DSH provider returned an empty reply")
            tool_call = _parse_tool_reply(reply, tools)
            if tool_call:
                return LLMResponse(
                    "assistant",
                    tools_call_name=[tool_call["name"]],
                    tools_call_args=[tool_call["arguments"]],
                    tools_call_ids=[uuid.uuid4().hex],
                )
            if _TOOL_ATTEMPT_RE.search(reply):
                # 工具调用格式坏到捞不回来时，绝不把原始 JSON 发进群聊。
                logger.warning(
                    "[DSH Provider] 工具调用格式损坏，已拦截原文: %s",
                    reply[:160].replace("\n", " "),
                )
                return LLMResponse("assistant", completion_text="……")
            return LLMResponse("assistant", completion_text=reply)
        except Exception as exc:
            logger.error("[DSH Provider] request failed: %s", exc)
            raise

    async def text_chat_stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[LLMResponse, None]:
        response = await self.text_chat(*args, **kwargs)
        yield response
