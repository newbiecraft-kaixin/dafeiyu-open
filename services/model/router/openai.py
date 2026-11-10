"""OpenAI 兼容端点调用。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from infra import keepalive

from .dotenv import resolve_key
from .errors import ModelError
from .route import Route


def _openai(route: Route, messages: List[Dict[str, str]], system: str, dotenv: List[Path]) -> str:
    base = route.base_url.rstrip("/")
    if not base:
        raise ModelError(f"provider {route.provider} 缺少 base_url")
    body: Dict[str, Any] = {"model": route.model, "messages": [], "stream": False}
    if system:
        body["messages"].append({"role": "system", "content": system})
    body["messages"].extend(messages)

    key = resolve_key(route.api_key_env, dotenv)
    if route.api_key_env and not key:
        raise ModelError(f"provider {route.provider} 需要环境变量 {route.api_key_env}，但未读到值")
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        # 复用同一条长连接，省掉每条消息重新握手（HTTPS 尤其明显）
        data = keepalive.request_json(
            "POST",
            base + "/chat/completions",
            body,
            headers=headers,
            timeout=route.timeout_s,
        )
    except keepalive.KeepAliveError as exc:
        detail = exc.body.decode("utf-8", "replace")[:300]
        raise ModelError(f"{route.provider} HTTP {exc.status}: {detail}") from exc
    except (OSError, TimeoutError, ValueError) as exc:
        raise ModelError(f"{route.provider} 请求失败: {exc}") from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelError(f"{route.provider} 返回结构异常: {str(data)[:200]}") from exc
    text = str(content or "").strip()
    if not text:
        raise ModelError(f"{route.provider} 返回空回复")
    return text
