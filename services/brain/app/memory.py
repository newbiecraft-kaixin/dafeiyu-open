"""会话指针与统一记忆库之间的读写辅助。"""
from __future__ import annotations

from typing import Any, Dict, List

from .state import config, harness, store

# 每轮注入记忆的默认上限；roles.json 的 harness.memory 可覆盖，缺省即这些值。
MEMORY_DEFAULTS = {
    "facts_per_scope": 8,   # 每个 scope（角色级 + 会话级）召回条数
    "fact_chars": 120,      # 单条事实 value 截断
    "total_chars": 1200,    # [长期记忆] + [最近对话] 合计字符上限
    "recent_messages": 6,   # 新会话注入的最近消息条数
    "recent_chars": 160,    # 单条最近消息截断
}


def _limits() -> Dict[str, int]:
    raw = config.harness_setting("memory", {}) or {}
    out = dict(MEMORY_DEFAULTS)
    if isinstance(raw, dict):
        for key, default in MEMORY_DEFAULTS.items():
            try:
                value = raw.get(key)
                if value is not None:
                    out[key] = max(0, int(value))
            except (TypeError, ValueError):
                out[key] = default
    return out


def _clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    if limit > 0 and len(value) > limit:
        return value[:limit] + "…"
    return value


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
    条数、单条长度和总字符都有上限（harness.memory，默认见 MEMORY_DEFAULTS），
    超出部分直接丢弃而不是整段拼进去。
    """
    limits = _limits()
    budget = limits["total_chars"]
    parts: List[str] = []
    facts: List[Dict[str, Any]] = []
    seen_keys: set = set()
    seen_scopes: set = set()
    for item_scope in (scope.split("|", 1)[0], scope):
        if not item_scope or item_scope in seen_scopes or limits["facts_per_scope"] <= 0:
            continue
        seen_scopes.add(item_scope)
        for fact in store.recall(item_scope, limit=limits["facts_per_scope"]):
            key = str(fact.get("key") or "").strip()
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            facts.append(fact)

    if facts and budget > 0:
        lines: List[str] = []
        used = len("[长期记忆]\n")
        for fact in facts:
            line = f"- {fact['key']}：{_clip(fact.get('value'), limits['fact_chars'])}"
            if used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1
        if lines:
            parts.append("[长期记忆]\n" + "\n".join(lines))
            budget -= used

    if not has_session and limits["recent_messages"] > 0 and budget > 0:
        recent = store.recent_messages(scope, limits["recent_messages"])
        lines = []
        used = len("[最近对话]\n")
        for item in recent:
            who = "你" if item.get("kind") == "assistant" else (item.get("sender_name") or "对方")
            line = f"{who}：{_clip(item.get('text'), limits['recent_chars'])}"
            if used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1
        if lines:
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
