"""管理指令/日志统计 mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import json
import os
import re
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.core.utils.active_event_registry import active_event_registry

from . import blocklist
from .compat import enter_plugin_main, exit_plugin_main, install_helpers

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())


class AdminMixin:
    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            with open(os.path.join(DATA_ROOT, "cmd_config.json"), encoding="utf-8-sig") as f:
                admins = [str(a).strip() for a in (json.load(f).get("admins_id") or [])]
        except Exception:  # noqa: BLE001
            return False
        return str(event.get_sender_id() or "").strip() in admins

    @filter.command("停")
    async def stop_cn(self, event: AstrMessageEvent):
        """/停 —— 停掉本会话正在跑的回复，以及后台翻唱（仅主人）"""
        if not self._is_admin(event):
            event.stop_event()
            return
        umo = event.unified_msg_origin
        stopped = active_event_registry.request_agent_stop_all(umo, exclude=event)
        killed = 0
        for t in list(_COVER_TASKS):
            if not t.done():
                t.cancel()
                killed += 1
        if not stopped and not killed:
            yield event.plain_result("这会儿没有在跑的任务。")
            return
        parts = []
        if stopped:
            parts.append(f"{stopped} 个回复任务")
        if killed:
            parts.append(f"{killed} 个翻唱")
        yield event.plain_result("停了：" + "、".join(parts) + "。")

    @filter.command("拉黑")
    async def blocklist_cmd(self, event: AstrMessageEvent):
        """/拉黑 @某人 分钟数 —— 拉黑某人 N 分钟（仅主人）；不带参数列出当前黑名单；/拉黑 @某人 0 解除"""
        if not self._is_admin(event):
            event.stop_event()
            return
        rest = re.sub(r"^/?(拉黑)\s*", "", str(event.message_str or "").strip())
        uid, disp = "", ""
        for comp in getattr(getattr(event, "message_obj", None), "message", None) or []:
            if isinstance(comp, At):
                uid = str(getattr(comp, "qq", "") or "").strip()
                disp = f"@{uid}"
                break
        if not uid:
            m = re.search(r"\b(\d{5,11})\b", rest)
            if m:
                uid, disp = m.group(1), m.group(1)
        if not uid:
            items = blocklist.listing()
            if not items:
                yield event.plain_result("黑名单是空的。用法：/拉黑 @某人 分钟数")
            else:
                now = time.time()
                lines = [
                    f"{u}：还剩 {max(1, int((float(e.get('until') or 0) - now) // 60) + 1)} 分钟"
                    f"（{e.get('reason') or '没写原因'}，{'AI 自己拉的' if e.get('by') == 'ai' else '主人拉的'}）"
                    for u, e in items
                ]
                yield event.plain_result("当前黑名单：\n" + "\n".join(lines))
            return
        mins = 30
        m = re.search(r"(\d{1,4})", rest)
        if m:
            mins = int(m.group(1))
        if mins <= 0:
            removed = blocklist.clear(uid)
            yield event.plain_result(
                f"已解除 {disp} 的拉黑。" if removed else f"{disp} 本来就不在黑名单里。"
            )
            return
        mins = blocklist.set_block(uid, mins, reason="主人指令", by="owner")
        yield event.plain_result(f"好，{disp} 被拉黑 {mins} 分钟，期间TA说什么都当没看见。到期自动恢复。")


exit_plugin_main(globals(), _REAL_MODULE_NAME)
