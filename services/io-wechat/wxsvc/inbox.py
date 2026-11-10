"""来消息：wechatauto 回调 -> 归一化快照 -> 统一信封 -> 事件队列。"""

from __future__ import annotations

import html
import re
import time
from typing import Any, Dict, List

from infra import build_envelope

from . import pet_push, state
from .contacts import display_name
from .config import (
    AT_RE,
    GROUP_PREFIX_RE,
    NIGHT_REPLY_QUOTA,
    NIGHT_REPLY_WINDOW,
    STARTUP_GRACE,
)
from .util import log


def _xml_attrs(raw: str) -> Dict[str, str]:
    pairs = re.findall(r'(\w+)\s*=\s*"([^"]*)"', raw or "")
    return {key.lower(): html.unescape(value) for key, value in pairs}


def parse_content(content: str) -> tuple[str, List[Dict[str, Any]]]:
    """微信的图片/表情/视频消息给的是整段 XML，这里拆成可读正文 + 结构化媒体。"""
    text = str(content or "").strip()
    if "<msg" not in text:
        return text, []
    media: List[Dict[str, Any]] = []
    emoji = re.search(r"<emoji\b([^>]*)>", text, re.I)
    if emoji:
        attrs = _xml_attrs(emoji.group(1))
        media.append({
            "kind": "image",
            "url": attrs.get("cdnurl") or attrs.get("thumburl") or "",
            "name": "表情",
        })
        return "[表情]", media
    img = re.search(r"<img\b([^>]*)>", text, re.I)
    if img:
        attrs = _xml_attrs(img.group(1))
        media.append({
            "kind": "image",
            "url": attrs.get("cdnbigimgurl") or attrs.get("cdnmidimgurl") or attrs.get("cdnthumburl") or "",
            "name": "图片",
        })
        return "[图片]", media
    video = re.search(r"<video\b([^>]*)>", text, re.I)
    if video:
        attrs = _xml_attrs(video.group(1))
        media.append({
            "kind": "video",
            "url": attrs.get("cdnvideourl") or "",
            "name": "视频",
        })
        return "[视频]", media
    if re.search(r"<voicemsg\b", text, re.I):
        return "[语音]", media
    appmsg = re.search(r"<appmsg\b", text, re.I)
    if appmsg:
        title = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
        label = html.unescape(title.group(1).strip()) if title else ""
        return ("[链接] " + label if label else "[链接]"), media
    return "[消息]", media


def _create_time(msg, d: dict) -> int:
    """消息创建时间（秒）。个别版本给毫秒，统一成秒；取不到返回 0。"""
    try:
        ct = int(getattr(msg, "create_time", 0) or d.get("create_time") or 0)
    except (TypeError, ValueError):
        return 0
    return ct // 1000 if ct > 10 ** 12 else ct


def snapshot(msg, chat) -> dict:
    info = {}
    try:
        info = chat.ChatInfo() or {}
    except Exception:
        pass
    try:
        d = msg.to_dict() or {}
    except Exception:
        d = {}
    # AddListenAll 回调给的是 _AllMessageChat：没有 ChatInfo，
    # 但 .who 是会话 username（群聊以 @chatroom 结尾）、.nickname 是显示名。
    # 稳定标识只认 username，显示名只用于展示/日志，绝不再拿它当发送目标。
    who = str(getattr(chat, "who", "") or getattr(chat, "_wxid", "") or "")
    name = str(getattr(chat, "nickname", "") or "")
    chat_type = info.get("chat_type") or ("group" if who.endswith("@chatroom") else "friend")
    content = getattr(msg, "content", "") or d.get("content") or ""
    if chat_type == "group":
        content = GROUP_PREFIX_RE.sub("", content, count=1)
    content, media = parse_content(content)
    sender_uid = str(d.get("sender_wxid") or getattr(msg, "sender_wxid", "") or "")
    sender_name = str(d.get("sender") or getattr(msg, "sender", "") or "")
    # 自己发的消息：引擎的 is_self 依赖 sender_wxid 解析，老分片里 sender_id
    # 是数字、解析不出 wxid，就会漏判成对方消息，机器人读到自己的话再回一遍。
    is_self = bool(getattr(msg, "is_self", False))
    if not is_self and state.bot_wxid and sender_uid == state.bot_wxid:
        is_self = True
    if (not is_self and state.bot_name and sender_name == state.bot_name
            and (not sender_uid or sender_uid == state.bot_wxid)):
        is_self = True
    if not sender_uid and chat_type != "group":
        # 私聊里发消息的人就是会话本人，这条是结构事实而不是猜测
        sender_uid = who
    display = str(info.get("chat_name") or name or "")
    if not display or display == who:
        display = name or who
    return {
        "chat": display,
        "chat_uid": who,
        "chat_type": chat_type,
        "sender": sender_name,
        "sender_uid": sender_uid,
        "type": getattr(msg, "type", "") or d.get("type") or "",
        "content": content,
        "media": media,
        # 下游（AstrBot 桥接）要据此判断「有没有 @ 机器人」，
        # 因为微信正文里的 “@昵称” 只是一段普通文本，没有独立的消息段。
        "mentions": mentions(content),
        "is_self": is_self,
        "create_time": _create_time(msg, d),
        "id": str(getattr(msg, "id", "") or d.get("id") or ""),
    }


def mentions(content: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for name in AT_RE.findall(content or ""):
        if name in ("所有人", "全体成员", "all"):
            out.append({"id": "all", "name": name, "is_all": True})
            continue
        is_bot = bool(state.bot_name) and name == state.bot_name
        out.append({"id": "", "name": name, "is_bot": is_bot, "ambiguous": True})
    return out


def mute_key(chat: str, sender: str) -> str:
    return f"{chat}\x1f{sender}"


def muted(chat: str, sender: str) -> bool:
    key = mute_key(chat, sender)
    with state.mute_lock:
        until = state.mutes.get(key)
        if not until:
            return False
        if until <= time.time():
            state.mutes.pop(key, None)
            return False
        return True


def _echo_of_own(chat_uid: str, text: str) -> bool:
    """自己的发送被数据库回读：sender 解析不出来时，用最近发送内容对账兜底。"""
    if not chat_uid or not text:
        return False
    now = time.time()
    with state.send_gate_lock:
        for ts, who, last in state.recent_sends:
            if who == chat_uid and last == text and 0 <= now - ts <= 120:
                return True
    return False


def on_message(msg, chat) -> None:
    try:
        item = snapshot(msg, chat)
    except Exception as exc:  # noqa: BLE001
        log("snapshot failed:", exc)
        return
    if item["is_self"] or item["type"] == "system":
        if item["is_self"]:
            state.counts.inc("event_self")
        return

    # 重启后微信库会把停机期间的积压补发一遍：按消息创建时间丢掉旧消息
    ct = int(item.get("create_time") or 0)
    if ct and state.attach_time and ct < state.attach_time - STARTUP_GRACE:
        state.counts.inc("event_stale")
        return

    # 引擎解析不出显示名（sender 为空或就是裸 id）时，从联系人库兜底备注/昵称，
    # 桌宠气泡和管道记忆才不会显示 wxid/openim id 这种「没有昵称」的样子
    if not item["sender"] or item["sender"] == item["sender_uid"]:
        nick = display_name(item["sender_uid"])
        if nick:
            item["sender"] = nick

    # 自己刚发的消息被回读，但引擎没判出 is_self 时，用发送内容对账兜底
    if (not item["sender"] or item["sender"] == state.bot_name) and _echo_of_own(
        item["chat_uid"], item["content"]
    ):
        state.counts.inc("event_echo")
        return

    if muted(item["chat"], item["sender"]):
        with state.stats_lock:
            state.stats["received"] += 1
            state.stats["muted"] = state.stats.get("muted", 0) + 1
        state.counts.inc("event_muted")
        return

    chat_type = "group" if item["chat_type"] == "group" else "private"
    chat_uid = item["chat_uid"] or ""
    # 群消息没有群 id 时不能退回 sender_uid：那会把回复发给群成员的私聊。
    if chat_type == "group" and not chat_uid.endswith("@chatroom"):
        state.counts.inc("event_drop_no_group")
        log("群消息缺少群 id，丢弃:", item.get("sender"), (item.get("content") or "")[:20])
        return
    try:
        env = build_envelope(
            "wechat",
            kind="message",
            chat_id=chat_uid or item["sender_uid"] or item["chat"] or item["sender"],
            chat_type=chat_type,
            chat_name=item["chat"],
            sender_id=item["sender_uid"] or (chat_uid if chat_type == "private" else "") or item["sender"],
            sender_name=item["sender"],
            sender_is_bot=bool(state.bot_name) and item["sender"] == state.bot_name,
            mentions=item.get("mentions") or [],
            text=item["content"],
            media=item.get("media") or [],
            raw=item,
            bot_names=[state.bot_name] if state.bot_name else [],
        )
    except Exception as exc:  # noqa: BLE001
        log("envelope failed:", exc)
        return

    with state.lock:
        state.raw_seq += 1
        item["seq"] = state.raw_seq
        item["ts"] = int(time.time())
        state.raw_messages.append(item)
        if chat_uid:
            now = time.time()
            win = state.reply_windows.get(chat_uid)
            if not win or win[0] < now:
                state.reply_windows[chat_uid] = [now + NIGHT_REPLY_WINDOW, float(NIGHT_REPLY_QUOTA)]
            else:
                win[1] = float(NIGHT_REPLY_QUOTA)
            if len(state.reply_windows) > 2000:
                cutoff = now - 600
                stale = [k for k, v in state.reply_windows.items() if v[0] < cutoff]
                for k in stale:
                    state.reply_windows.pop(k, None)
        state.stats["received"] += 1
        if chat_type == "group":
            state.stats["groups"] += 1
        state.lock.notify_all()

    if state.dedupe.seen(env):
        with state.stats_lock:
            state.stats["duplicate"] += 1
        state.counts.inc("event_duplicate")
        return

    state.queue.push(env)
    state.counts.inc("event_in")
    state.counts.inc("event message")
    # 图片/表情/GIF 不进 AstrBot 消息链（桥接只认文字），在这里直推桌宠
    try:
        pet_push.push_image_message(item)
    except Exception as exc:  # noqa: BLE001
        log("pet push error:", exc)
