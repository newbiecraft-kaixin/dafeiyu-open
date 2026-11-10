"""被动记录消息的 CustomFilter。"""

from __future__ import annotations

import weakref

from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import CustomFilter

_PLUGIN_REF: weakref.ReferenceType | None = None


def set_plugin_ref(plugin) -> None:
    global _PLUGIN_REF
    _PLUGIN_REF = weakref.ref(plugin)


class RecordUserMessageFilter(CustomFilter):
    """被动记录每条消息的说话人档案，永远返回 False（不唤醒机器人）

    注册时 AstrBot 会用 `cls(raise_error)` 实例化，插件实例通过模块级弱引用传递。
    """

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
            plugin._record(event)
        return False
