"""管理指令/日志统计 mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import json
import os


from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.utils.active_event_registry import active_event_registry

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


exit_plugin_main(globals(), _REAL_MODULE_NAME)
