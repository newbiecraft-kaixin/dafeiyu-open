"""统一调度入口：按 provider 链依次尝试。"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from .errors import ModelError
from .harness import _run_harness
from .openai import _openai
from .route import Route
from .router import Router


def complete(payload: Dict[str, Any], *, router: Router) -> Dict[str, Any]:
    role = str(payload.get("role") or "").strip()
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ModelError("messages 或 text 不能为空")
        messages = [{"role": "user", "content": text}]
    norm: List[Dict[str, str]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if content:
            norm.append({"role": str(item.get("role") or "user"), "content": content})
    if not norm:
        raise ModelError("messages 里没有有效内容")

    profile = str(payload.get("profile") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    scene = str(payload.get("scene") or "").strip()
    system = str(payload.get("system") or "").strip()

    # 显式指定 provider（如兜底 local-ollama）时，型号应取该 provider 自己的，而不是角色的主模型。
    explicit_provider = bool(str(payload.get("provider") or "").strip())
    primary = router.resolve(
        role,
        provider=str(payload.get("provider") or ""),
        model=str(payload.get("model") or ""),
        prefer_provider_model=explicit_provider,
    )
    chain: List[Route] = [primary]
    for name in router.fallbacks:
        if name and name != primary.provider and name in router.providers:
            try:
                chain.append(router.resolve(role, provider=name, prefer_provider_model=True))
            except ModelError:
                continue

    started = time.perf_counter()
    errors: List[str] = []
    for route in chain:
        try:
            if route.kind == "harness":
                task = f"{scene}\n{norm[-1]['content']}" if scene else norm[-1]["content"]
                reply, sid = _run_harness(
                    route, profile=profile, task=task, session_id=session_id, dotenv=router.dotenv
                )
            else:
                reply, sid = _openai(route, norm, system, router.dotenv), ""
            return {
                "reply": reply,
                "role": primary.role,
                "provider": route.provider,
                "type": route.kind,
                "model": route.model,
                "session_id": sid,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        except ModelError as exc:
            errors.append(f"{route.provider}: {exc}")
    raise ModelError("；".join(errors) or "没有可用 provider")
