"""被动记录群聊消息的 CustomFilter。

返回 False，因此不会唤醒机器人，也不会触发本插件的 handler；
真正记录动作在 filter() 里完成（与 speaker_tag 的被动记录方式一致）。
"""
from __future__ import annotations

import weakref

from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import CustomFilter

_PLUGIN_REF: weakref.ReferenceType | None = None


def set_plugin_ref(plugin) -> None:
    global _PLUGIN_REF
    _PLUGIN_REF = weakref.ref(plugin)


class GroupWindowFilter(CustomFilter):
    """每条群消息都进动态窗口，永远返回 False（保持原唤醒逻辑）。"""

    def __init__(self, raise_error: bool = True, plugin=None, **kwargs) -> None:
        if not isinstance(raise_error, bool) and plugin is None:
            plugin = raise_error
            raise_error = False
        super().__init__(raise_error=raise_error, **kwargs)
        if plugin is not None:
            global _PLUGIN_REF
            _PLUGIN_REF = weakref.ref(plugin)

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        plugin = _PLUGIN_REF() if _PLUGIN_REF is not None else None
        if plugin is not None:
            plugin._capture(event)
        return False
