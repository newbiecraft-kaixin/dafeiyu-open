"""发送：夜间风控、重复/频率拦截。所有窗口操作串行在 wx_lock。

只保留纯文字发送；引用、@、图片/文件/语音等依赖额外窗口点击的能力已移除。
"""

from __future__ import annotations

import time

from . import state, window
from .config import NIGHT_REPLY_QUOTA, NIGHT_REPLY_WINDOW, QUIET_END, QUIET_START
from .contacts import chat, display_name
from .util import log


def in_quiet() -> bool:
    hour = time.localtime().tm_hour
    if QUIET_START == QUIET_END:
        return False
    if QUIET_START < QUIET_END:
        return QUIET_START <= hour < QUIET_END
    return hour >= QUIET_START or hour < QUIET_END


def night_reply_allowed(uid: str, proactive: bool) -> bool:
    """夜间只放行「刚收到消息后的即时回复」，且消耗回复额度。"""
    if proactive:
        return False
    now = time.time()
    with state.lock:
        win = state.reply_windows.get(uid)
        if not win or win[0] < now or win[1] <= 0:
            state.reply_windows.pop(uid, None)
            return False
        win[1] -= 1
    return True


def send_gate(uid: str, text: str) -> str:
    """风控：同会话重复内容 / 短时高频直接拦下，避免重试风暴刷屏。"""
    now = time.time()
    with state.send_gate_lock:
        while state.recent_sends and now - state.recent_sends[0][0] > 60:
            state.recent_sends.popleft()
        for ts, who, last in state.recent_sends:
            if who == uid and last == (text or "") and now - ts <= 90:
                return "duplicate"
        if sum(1 for _ts, who, _last in state.recent_sends if who == uid) >= 8:
            return "rate"
        state.recent_sends.append((now, uid, text or ""))
    return ""


def gate_rollback(uid: str, text: str) -> None:
    """发送失败时撤销风控登记。

    风控是在「尝试发送之前」登记的；如果这次压根没发出去（例如剪贴板被别的
    程序占住、内容没落进输入框），登记留着会把 90 秒内的正常重发判成重复消息，
    表现就是「偶尔发一条就发不出去了」。
    """
    now = time.time()
    with state.send_gate_lock:
        for i in range(len(state.recent_sends) - 1, -1, -1):
            ts, who, last = state.recent_sends[i]
            if who == uid and last == (text or "") and now - ts <= 60:
                del state.recent_sends[i]
                break


def send_uid(uid: str, text: str, exact: bool = False) -> str:
    verdict = send_gate(uid, text)
    if verdict:
        state.counts.inc(f"send_blocked_{verdict}")
        raise RuntimeError("重复消息已拦截（90 秒内同一会话同文）" if verdict == "duplicate"
                           else "发送过于频繁（同一会话每分钟上限 8 条）")
    resp = None
    try:
        # 输入框没接住内容（剪贴板被抢 / 焦点被切走）时 wechatauto 会拒绝回车，
        # 这种失败什么都没发出去，可以安全地再来一次；已经按过回车的不重试。
        # 用界面显示名当发送目标：wechatauto 判断「这个会话是否已经开着」时
        # 比对的是聊天窗口标题（显示名），传 wxid 永远对不上，于是每条消息
        # 都要重新搜索、点开一次会话。名称解析不到时才退回 uid。
        for attempt in range(2):
            with state.wx_lock:
                prev = window.begin_window_op()
                try:
                    resp = chat(uid).SendMsg(
                        text, who=display_name(uid) or uid, exact=exact
                    )
                finally:
                    window.end_window_op(prev)
            status = getattr(resp, "status", None)
            if status is not False:
                with state.stats_lock:
                    state.stats["sent"] += 1
                state.counts.inc("sent")
                return str(status if status is not None else resp)
            gui = window.gui()
            if getattr(gui, "_send_attempted", True):
                break  # 回车可能已投递，重发有重复风险
            if attempt == 0:
                log("发送未落进输入框，1 秒后重试:", getattr(resp, "message", ""))
                try:
                    window.ensure_visible()
                except Exception as exc:  # noqa: BLE001
                    log("重试前置前失败:", exc)
                time.sleep(1.0)
        raise RuntimeError(str(getattr(resp, "message", None) or "发送失败"))
    except Exception:
        gate_rollback(uid, text)
        raise
