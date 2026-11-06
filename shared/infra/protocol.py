"""统一协议（Envelope）：所有平台、所有事件进管道前都变成这一个结构。

base 字段（任何事件都有）：
    platform  平台标识，开放字符串（qq / wechat / 未来插件平台），不写死枚举
    kind      事件类型：message | space | notice | result
    trace_id  链路 id
    ts        秒级时间戳
    chat      {id, type: group|private|channel, name}
    sender    {id, name, is_bot}
    text      文本
    media     [{kind: image|voice|video|file, path, url, name}]
    mentions  [{id, name, is_bot, is_all, ambiguous}]
    payload   kind 专属数据（message 为空对象）
    raw       平台原始数据，只给出站适配器排查/去重用，业务层不读

事件约定：
    message  普通聊天消息，用 text / media / mentions
    space    空间/动态：QQ 空间、微信朋友圈，payload = {action, post_id, url, author}
    notice   系统通知：入群、退群、被踢、登录失效等
    result   平台动作回执：撤回、禁言等执行结果

平台差异（@、去重、媒体路径）只允许留在平台 IO 服务内，出了 IO 层只剩本结构。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

# 已知平台，仅用于工具与文档展示；校验不限制，方便新平台以插件形式接入
KNOWN_PLATFORMS = ("qq", "wechat")

EVENT_KINDS = ("message", "space", "notice", "result")
CHAT_TYPES = ("group", "private", "channel")
MEDIA_KINDS = ("image", "voice", "video", "file")
SPACE_ACTIONS = ("post", "like", "comment", "reply")

AT_ALL_ID = "all"
_PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_trace_lock = threading.Lock()
_trace_seq = 0


class EnvelopeError(ValueError):
    """协议结构非法。"""


def make_trace_id(platform: str) -> str:
    """生成可读且不重复的链路 id：平台前缀 + 时间 + 自增 + 随机。"""
    global _trace_seq
    with _trace_lock:
        _trace_seq = (_trace_seq + 1) & 0xFFFF
        seq = _trace_seq
    return f"{platform}-{int(time.time() * 1000):x}{seq:04x}{secrets.token_hex(2)}"


def normalize_text(text: Any) -> str:
    """只做保守处理：转字符串、去首尾空白、压缩连续空格，保留换行。"""
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def _norm_mention(item: Any, *, bot_ids: Iterable[str] = (), bot_names: Iterable[str] = ()) -> Dict[str, Any]:
    """归一单条 @。字符串输入视为只有昵称，天然 ambiguous。"""
    bot_ids = {str(x) for x in bot_ids if x}
    bot_names = {str(x) for x in bot_names if x}

    if isinstance(item, str):
        name = item.strip().lstrip("@").strip()
        return {
            "id": "",
            "name": name,
            "is_bot": name in bot_names,
            "is_all": False,
            "ambiguous": True,
        }

    if not isinstance(item, dict):
        raise EnvelopeError(f"mentions 条目类型不支持: {type(item).__name__}")

    mid = str(item.get("id") or item.get("qq") or "").strip()
    name = str(item.get("name") or item.get("nickname") or "").strip().lstrip("@")
    is_all = bool(item.get("is_all")) or mid == AT_ALL_ID or name in ("所有人", "全体成员", "all")
    if is_all:
        mid = AT_ALL_ID
    ambiguous = bool(item.get("ambiguous", not mid))
    return {
        "id": "" if is_all else mid,
        "name": name,
        "is_bot": bool(item.get("is_bot")) or mid in bot_ids or name in bot_names,
        "is_all": is_all,
        "ambiguous": ambiguous,
    }


def _norm_media(item: Any) -> Dict[str, Any]:
    if isinstance(item, str):
        item = {"path": item}
    if not isinstance(item, dict):
        raise EnvelopeError(f"media 条目类型不支持: {type(item).__name__}")
    kind = str(item.get("kind") or "file").lower()
    if kind not in MEDIA_KINDS:
        kind = "file"
    return {
        "kind": kind,
        "path": str(item.get("path") or ""),
        "url": str(item.get("url") or ""),
        "name": str(item.get("name") or ""),
    }


def build_envelope(
    platform: str,
    *,
    kind: str = "message",
    chat_id: Any = "",
    chat_type: str = "group",
    chat_name: str = "",
    sender_id: Any = "",
    sender_name: str = "",
    sender_is_bot: bool = False,
    mentions: Optional[Iterable[Any]] = None,
    text: Any = "",
    media: Optional[Iterable[Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    raw: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
    ts: Optional[float] = None,
    bot_ids: Iterable[str] = (),
    bot_names: Iterable[str] = (),
) -> Dict[str, Any]:
    """构造协议对象并校验；非法直接抛 EnvelopeError。"""
    env: Dict[str, Any] = {
        "platform": str(platform or "").lower(),
        "kind": str(kind or "message").lower(),
        "trace_id": trace_id or make_trace_id(str(platform or "?")),
        "ts": float(ts if ts is not None else time.time()),
        "chat": {
            "id": str(chat_id or ""),
            "type": str(chat_type or "group").lower(),
            "name": str(chat_name or ""),
        },
        "sender": {
            "id": str(sender_id or ""),
            "name": str(sender_name or ""),
            "is_bot": bool(sender_is_bot),
        },
        "mentions": [_norm_mention(m, bot_ids=bot_ids, bot_names=bot_names) for m in (mentions or [])],
        "text": normalize_text(text),
        "media": [_norm_media(m) for m in (media or [])],
        "payload": dict(payload or {}),
        "raw": dict(raw or {}),
    }
    errors = validate(env)
    if errors:
        raise EnvelopeError("; ".join(errors))
    return env


def build_space_event(
    platform: str,
    *,
    container_id: str,
    container_name: str = "",
    action: str = "post",
    post_id: Any = "",
    author_id: Any = "",
    author_name: str = "",
    text: Any = "",
    media: Optional[Iterable[Any]] = None,
    url: str = "",
    raw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """空间/动态事件：QQ 空间与朋友圈都走这里，payload 里区分动作。"""
    return build_envelope(
        platform,
        kind="space",
        chat_id=container_id,
        chat_type="channel",
        chat_name=container_name,
        sender_id=author_id,
        sender_name=author_name,
        text=text,
        media=media,
        payload={
            "action": str(action or "post").lower(),
            "post_id": str(post_id or ""),
            "url": str(url or ""),
        },
        raw=raw,
    )


def validate(env: Any) -> List[str]:
    """返回错误列表，空列表表示合法。不修改输入。"""
    errors: List[str] = []
    if not isinstance(env, dict):
        return ["信封必须是 dict"]

    platform = env.get("platform")
    if not isinstance(platform, str) or not _PLATFORM_RE.match(platform):
        errors.append(f"platform 非法: {platform!r}")

    kind = env.get("kind")
    if kind not in EVENT_KINDS:
        errors.append(f"kind 非法: {kind!r}")

    trace = env.get("trace_id")
    if not isinstance(trace, str) or not trace.strip():
        errors.append("trace_id 为空")

    ts = env.get("ts")
    if not isinstance(ts, (int, float)) or ts <= 0:
        errors.append("ts 必须是正数时间戳")

    chat = env.get("chat")
    if not isinstance(chat, dict):
        errors.append("chat 缺失")
    else:
        if not str(chat.get("id") or "").strip():
            errors.append("chat.id 为空")
        if chat.get("type") not in CHAT_TYPES:
            errors.append(f"chat.type 非法: {chat.get('type')!r}")

    sender = env.get("sender")
    if not isinstance(sender, dict):
        errors.append("sender 缺失")
    elif not str(sender.get("id") or "").strip() and not str(sender.get("name") or "").strip():
        errors.append("sender.id 与 sender.name 同时为空")

    mentions = env.get("mentions")
    if not isinstance(mentions, list):
        errors.append("mentions 必须是 list")
    else:
        for i, m in enumerate(mentions):
            if not isinstance(m, dict):
                errors.append(f"mentions[{i}] 不是 dict")
                continue
            if not str(m.get("id") or "").strip() and not str(m.get("name") or "").strip():
                errors.append(f"mentions[{i}] 的 id 与 name 同时为空")
            if not isinstance(m.get("is_all"), bool):
                errors.append(f"mentions[{i}].is_all 不是 bool")
            if not isinstance(m.get("ambiguous"), bool):
                errors.append(f"mentions[{i}].ambiguous 不是 bool")

    text = env.get("text")
    if not isinstance(text, str):
        errors.append("text 必须是字符串")

    media = env.get("media")
    if not isinstance(media, list):
        errors.append("media 必须是 list")
    else:
        for i, m in enumerate(media):
            if not isinstance(m, dict):
                errors.append(f"media[{i}] 不是 dict")

    payload = env.get("payload")
    if not isinstance(payload, dict):
        errors.append("payload 必须是 dict")
    elif kind != "message":
        if kind == "space" and payload.get("action") not in SPACE_ACTIONS:
            errors.append(f"space.action 非法: {payload.get('action')!r}")

    if not isinstance(env.get("raw"), dict):
        errors.append("raw 必须是 dict")

    return errors


def is_valid(env: Any) -> bool:
    return not validate(env)


def is_space(env: Dict[str, Any]) -> bool:
    return env.get("kind") == "space"


def bot_mentioned(env: Dict[str, Any]) -> bool:
    return any(m.get("is_bot") for m in env.get("mentions") or [])


def at_all(env: Dict[str, Any]) -> bool:
    return any(m.get("is_all") for m in env.get("mentions") or [])


def mention_names(env: Dict[str, Any]) -> List[str]:
    return [m["name"] for m in env.get("mentions") or [] if m.get("name")]


def clean_text_for_bot(env: Dict[str, Any]) -> str:
    """去掉正文里的 @昵称，保留其余文字与换行。"""
    text = str(env.get("text") or "")
    for name in sorted(mention_names(env), key=len, reverse=True):
        text = text.replace("@" + name, " ").replace("＠" + name, " ")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def to_json(env: Dict[str, Any]) -> str:
    return json.dumps(env, ensure_ascii=False, separators=(",", ":"))


def from_json(payload: str | bytes) -> Dict[str, Any]:
    env = json.loads(payload)
    errors = validate(env)
    if errors:
        raise EnvelopeError("; ".join(errors))
    return env
