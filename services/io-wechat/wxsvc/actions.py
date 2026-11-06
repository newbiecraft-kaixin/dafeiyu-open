"""HTTP 发送：/send。微信侧只保留文字收发。

朋友圈、拍一拍、撤回、引用、@、图片/文件/语音等需要额外窗口点击或转文件发送的
功能都已移除，不再提供 /action 与 /space。
"""

from __future__ import annotations

from typing import Any

from infra import fail, ok

from . import state
from .config import NIGHT_REPLY_WINDOW, QUIET_END, QUIET_START
from .contacts import resolve_target
from .sending import in_quiet, night_reply_allowed, send_uid
from .util import log


def handle_send(req) -> Any:
    payload = req.payload if isinstance(req.payload, dict) else {}
    to = str(payload.get("to") or "").strip()
    text = str(payload.get("text") or "")
    if not to or not text.strip():
        return fail("to/text required（微信侧只支持文字发送）", 400)
    if state.wx is None:
        return fail("微信未附着（请确认微信已登录且窗口可见）", 503)
    try:
        uid = resolve_target(to)
    except ValueError as exc:
        return fail(str(exc), 400)

    proactive = bool(payload.get("proactive"))
    if in_quiet() and not night_reply_allowed(uid, proactive):
        with state.stats_lock:
            state.stats["blocked_night"] += 1
        state.counts.inc("send_blocked_night")
        log("夜间拦截主动消息:", uid, "proactive=", proactive)
        return fail(
            f"夜间 {QUIET_START:02d}:00-{QUIET_END:02d}:00 禁止主动消息；"
            f"只在收到消息后 {NIGHT_REPLY_WINDOW} 秒内允许即时回复",
            429,
        )

    exact = bool(payload.get("exact", False))
    try:
        return ok({"to": uid, "status": send_uid(uid, text, exact=exact)})
    except Exception as exc:  # noqa: BLE001
        state.counts.inc("send_failed")
        return fail(f"{type(exc).__name__}: {exc}", 503)
