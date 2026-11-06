"""HTTP 路由：管道接口、旧接口兼容与能力声明。"""

from __future__ import annotations

import time
from typing import Any

from infra import fail, ok

from . import state
from .actions import handle_send
from .contacts import friend_rows, group_rows
from .config import NIGHT_REPLY_WINDOW, QUIET_END, QUIET_START, START_AT
from .ui import handle_ui


def legacy_health(req) -> Any:
    return 200, {
        "ok": state.wx is not None,
        "service": "io-wechat",
        "uptime": int(time.time() - START_AT),
        "queued": len(state.raw_messages),
        "stats": dict(state.stats),
        "bot": state.bot_name or "",
        "event_queue": state.queue.stats(),
    }


def legacy_sessions(req) -> Any:
    if state.wx is None:
        return fail("微信未附着", 503)
    try:
        with state.wx_lock:
            items = state.wx.GetSession()
        names = [getattr(x, "name", None) or str(x) for x in items]
        return ok({"sessions": names})
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 503)


def handle_groups(req) -> Any:
    """群列表（微信侧），中枢的群管控把 QQ 群和这些合并显示。"""
    if state.wx is None:
        return fail("微信未附着", 503)
    try:
        with state.wx_lock:
            rows = group_rows()
        return ok({"groups": rows})
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 503)


def handle_friends(req) -> Any:
    """好友列表（微信侧），中枢的好友管控把 QQ 好友和这些合并显示。"""
    if state.wx is None:
        return fail("微信未附着", 503)
    try:
        with state.wx_lock:
            rows = friend_rows()
        return ok({"friends": rows})
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 503)


def legacy_messages(req) -> Any:
    since = int(req.q("since", "0") or 0)
    wait = min(int(req.q("wait", "0") or 0), 30000) / 1000
    deadline = time.time() + wait
    with state.lock:
        while state.raw_seq <= since and wait > 0 and time.time() < deadline:
            state.lock.wait(min(1.0, deadline - time.time()))
        items = [m for m in state.raw_messages if m["seq"] > since]
        return 200, {"ok": True, "seq": state.raw_seq, "messages": items}


def legacy_mood(req) -> Any:
    with state.lock:
        return 200, {"ok": True, "stats": dict(state.stats), "queued": len(state.raw_messages)}


def handle_capabilities(req) -> Any:
    return ok({
        "platform": "wechat",
        "message": True,
        "send": True,
        "text": True,
        "media": {
            "images": False,
            "files": False,
            "audio_file": False,
            "voice_bubble": False,
            "reason": "微信侧只保留文字收发；图片/文件/语音依赖窗口点击或转文件发送，已移除",
        },
        "pat": False,
        "recall_last": False,
        "space": {"fetch": False, "like": False, "comment": False, "reply": False, "post": False},
        "contacts": {"groups": True, "friends": True},
        "night_quiet": {
            "start_hour": QUIET_START,
            "end_hour": QUIET_END,
            "policy": f"夜间禁止主动消息；仅放行收到消息后 {NIGHT_REPLY_WINDOW} 秒内的即时回复",
        },
        "reason": "朋友圈、拍一拍、撤回、引用、@、文件/语音等需要额外窗口点击的功能已移除；"
                  "禁言为机器人级软禁言（不回复、不进管道）",
    })


routes = {
    "GET /": handle_ui,
    "GET /ui": handle_ui,
    "GET /health": legacy_health,
    "GET /events": lambda req: ok({
        "seq": state.queue.seq,
        "events": state.queue.since(int(req.q("since", "0") or 0),
                                    min(int(req.q("wait", "0") or 0), 30000))[1],
        **state.queue.stats(),
    }),
    "GET /sessions": legacy_sessions,
    "GET /groups": handle_groups,
    "GET /friends": handle_friends,
    "GET /messages": legacy_messages,
    "GET /mood": legacy_mood,
    "GET /capabilities": handle_capabilities,
    "POST /send": handle_send,
}
