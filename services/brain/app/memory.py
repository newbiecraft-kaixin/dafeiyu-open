"""会话指针与统一记忆库之间的读写辅助。"""
from __future__ import annotations

from typing import Any, Dict, List

from .state import harness, store


def _hydrate_session(key: str) -> None:
    """内存里没有会话指针时，从统一 store 恢复，保证重启后仍能续聊。"""
    if harness.sessions.get(key):
        return
    saved = store.get_session(key)
    sid = str((saved or {}).get("session_id") or "")
    if sid:
        harness.sessions.set(key, sid)


def _persist_session(key: str, role: Any, result: Any, parts: Dict[str, str]) -> None:
    """会话指针与消息一起落进统一记忆库。"""
    if not result.session_id:
        return
    store.set_session(
        key,
        result.session_id,
        role=role.name,
        platform=parts.get("platform", ""),
        chat_id=parts.get("chat_id", ""),
        chat_type=parts.get("chat_type", ""),
        provider=result.provider,
        model=result.model,
    )


def _memory_context(scope: str, has_session: bool) -> str:
    """长期事实 + （新会话时）最近对话，拼成本轮任务的前缀。

    事实按「角色级」与「当前会话级」两层召回：角色级跨平台共享，会话级只在本会话生效。
    """
    parts: List[str] = []
    facts: List[Dict[str, Any]] = []
    seen: set = set()
    for item_scope in (scope.split("|", 1)[0], scope):
        if not item_scope or item_scope in seen:
            continue
        seen.add(item_scope)
        for fact in store.recall(item_scope, limit=8):
            facts.append(fact)
    if facts:
        lines = [f"- {f['key']}：{f['value']}" for f in facts]
        parts.append("[长期记忆]\n" + "\n".join(lines))
    if not has_session:
        recent = store.recent_messages(scope, 6)
        if recent:
            lines = []
            for item in recent:
                who = "你" if item.get("kind") == "assistant" else (item.get("sender_name") or "对方")
                lines.append(f"{who}：{str(item.get('text') or '')[:200]}")
            parts.append("[最近对话]\n" + "\n".join(lines))
    return "\n".join(parts)


def _record(
    scope: str,
    kind: str,
    text: str,
    role: Any,
    parts: Dict[str, str],
    trace_id: str = "",
) -> None:
    store.append_message(
        scope,
        kind,
        text,
        role=role.name,
        platform=parts.get("platform", ""),
        chat_id=parts.get("chat_id", ""),
        chat_type=parts.get("chat_type", ""),
        sender_id=parts.get("user_id", "") if kind == "user" else "",
        sender_name=parts.get("name", "") if kind == "user" else "",
        trace_id=trace_id,
    )
