"""对话与统一信封入口。"""
from __future__ import annotations

import time
from typing import Any

import roles as roles_mod
from harness import HarnessError, build_key
from infra import clean_text_for_bot, fail, ok, validate

from .common import _parts, _resolve_route, _role_override
from .memory import _hydrate_session, _memory_context, _persist_session, _record
from .state import LOG, config, counts, harness


def handle_chat(req) -> Any:  # noqa: ANN001
    payload = req.payload if isinstance(req.payload, dict) else {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return fail("text 不能为空", 400)

    try:
        forced = _role_override(payload)
        ident_note = ""
        if forced is not None:
            parts = _parts(payload)
            role = forced
            reason, is_admin = "role_override", False
        else:
            route, parts = _resolve_route(payload)
            role, reason, is_admin = route.role, route.reason, route.is_admin
            if route.identity is not None:
                ident_note = route.identity.note.split("/")[0].strip()
    except (ValueError, roles_mod.RolesError) as exc:
        return fail(str(exc), 400)

    if not parts["platform"]:
        return fail("缺少 platform", 400)

    key = build_key(role, parts["platform"], parts["chat_id"], parts["chat_type"], parts["user_id"])
    scene = (
        f"[场景] 平台={parts['platform']}"
        f" 会话={parts['chat_type']}:{parts['chat_name'] or parts['chat_id'] or '-'}"
        f" 发言人={parts['name'] or parts['user_id'] or '-'}"
        + ("（管理员）" if is_admin else "")
        + (f" 身份={ident_note}" if ident_note else "")
    )

    _hydrate_session(key)
    started = time.perf_counter()
    context = _memory_context(key, bool(harness.sessions.get(key)))
    _record(key, "user", text, role, parts)
    try:
        result = harness.chat(role, key, text, scene=scene, context=context)
    except HarnessError as exc:
        counts.inc("chat_failed")
        LOG.warning("chat failed (%s): %s", role.name, exc)
        return fail(str(exc), 502)
    _record(key, "assistant", result.reply, role, parts)
    _persist_session(key, role, result, parts)

    counts.inc("chat")
    counts.inc(f"chat {role.name}")
    return ok(
        {
            "role": result.role,
            "display": role.display,
            "profile": result.profile,
            "provider": result.provider,
            "model": result.model,
            "reply": result.reply,
            "session_id": result.session_id,
            "latency_ms": result.latency_ms,
            "reason": reason,
            "is_admin": is_admin,
            "platform": parts["platform"],
            "chat_id": parts["chat_id"],
            "scope": key,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    )


def handle_event(req) -> Any:  # noqa: ANN001
    payload = req.payload if isinstance(req.payload, dict) else {}
    env = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    errors = validate(env)
    if errors:
        return fail("信封非法: " + "; ".join(errors), 400)
    if env.get("kind") != "message":
        return fail(f"只处理 message 事件，收到 {env.get('kind')}", 400)

    sender = env.get("sender") or {}
    chat = env.get("chat") or {}
    text = clean_text_for_bot(env) or str(env.get("text") or "").strip()
    if not text:
        return fail("消息没有文本", 400)

    role = None
    try:
        route = config.resolve(
            platform=str(env.get("platform") or ""),
            user_id=sender.get("id"),
            name=sender.get("name"),
            chat_id=chat.get("id"),
        )
        role = route.role
    except roles_mod.RolesError as exc:
        return fail(str(exc), 400)

    key = build_key(
        role,
        str(env.get("platform") or ""),
        str(chat.get("id") or ""),
        str(chat.get("type") or "group"),
        str(sender.get("id") or ""),
    )
    parts = {
        "platform": str(env.get("platform") or ""),
        "user_id": str(sender.get("id") or ""),
        "name": str(sender.get("name") or ""),
        "chat_id": str(chat.get("id") or ""),
        "chat_type": str(chat.get("type") or "group"),
    }
    scene = (
        f"[场景] 平台={env.get('platform')}"
        f" 会话={chat.get('type')}:{chat.get('name') or chat.get('id') or '-'}"
        f" 发言人={sender.get('name') or sender.get('id') or '-'}"
        + ("（管理员）" if route.is_admin else "")
        + (
            f" 身份={route.identity.note.split('/')[0].strip()}"
            if route.identity is not None and route.identity.note
            else ""
        )
    )
    _hydrate_session(key)
    context = _memory_context(key, bool(harness.sessions.get(key)))
    _record(key, "user", text, role, parts, str(env.get("trace_id") or ""))
    try:
        result = harness.chat(role, key, text, scene=scene, context=context)
    except HarnessError as exc:
        counts.inc("event_failed")
        LOG.warning("event chat failed (%s): %s", role.name, exc)
        return fail(str(exc), 502)
    _record(key, "assistant", result.reply, role, parts, str(env.get("trace_id") or ""))
    _persist_session(key, role, result, parts)

    counts.inc("event_message")
    return ok(
        {
            "trace_id": env.get("trace_id"),
            "platform": env.get("platform"),
            "chat_id": chat.get("id"),
            "role": result.role,
            "display": role.display,
            "provider": result.provider,
            "model": result.model,
            "reply": result.reply,
            "session_id": result.session_id,
            "latency_ms": result.latency_ms,
            "reason": route.reason,
            "is_admin": route.is_admin,
            "scope": key,
            "suggest_only": True,
        }
    )
