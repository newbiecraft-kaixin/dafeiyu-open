"""基础 LLM 工具集：时间、群成员、戳一戳、提醒、天气、查头像、定时任务。

这是「如何给大肥鱼加 tool」的参考模板：
- 每个工具 = 一个带 @filter.llm_tool 装饰器的异步方法
- docstring 就是给 LLM 看的说明书（第一行用途 + 何时用，Args 里写参数类型）
- 支持 string / number / boolean / object / array 五种参数类型（注意：函数注解只能写 Python 类型）
- 返回 str 会回传给 LLM 总结；return None 则静默

高频、参数简单、只有主人用的能力（看日志/跨群发消息）不放 LLM 工具，
改成 /日志、/发群、/私聊 指令，省 token 也避免模型乱调。
定时任务是例外：查看/设置/移除做成一个三合一工具 manage_schedule，AI 能直接接住
「每天几点提醒我」这类请求；设置和移除只认主人，查看谁都能用。
"""

from __future__ import annotations

import asyncio

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.active_event_registry import active_event_registry

from .basic_core.reminder import ReminderMixin
from .basic_core.schedule import ScheduleMixin
from .basic_core.super_faces import SuperFaceMixin
from .basic_core.tools import ToolsMixin
from .basic_core.messaging import MessagingMixin
from .basic_core.voice import VoiceMixin
from .basic_core.music import MusicMixin
from .basic_core.sing import SingMixin
from .basic_core.cover import CoverMixin
from .basic_core.avatar import AvatarMixin
from .basic_core.admin import AdminMixin
from .basic_core.compat import load_helpers
from .basic_core.kugou import (
    KUGOU_COOKIE_PATH,
    SHARE_DIR,
    _kugou_cookie,
    _load_kugou_login,
    _save_kugou_cookie,
    _search_songs,
)

# 常量与纯函数都在 helpers.py：按原名字注入本模块，保持旧的模块级可见性
_helpers = load_helpers()
globals().update({k: v for k, v in vars(_helpers).items() if not k.startswith("__")})


@register(
    "astrbot_plugin_basic_tools",
    "dafeiyu-local",
    "基础 LLM 工具：时间/群成员/戳一戳/提醒/天气/查头像，外加 /日志 /发群 /私聊 /定时任务 指令",
    "v1.4.0",
)
class BasicTools(
    ReminderMixin,
    ToolsMixin,
    MessagingMixin,
    SuperFaceMixin,
    VoiceMixin,
    MusicMixin,
    CoverMixin,
    SingMixin,
    AvatarMixin,
    ScheduleMixin,
    AdminMixin,
    Star,
):
    def __init__(self, context: Context):
        super().__init__(context)
        self._reminders: list[dict] = []
        self._load_reminders()
        self._sched: list[dict] = []
        self._load_sched()
        self._task: asyncio.Task | None = None
        self._sfaces: dict[str, dict] = {}
        self._load_super_faces()
        self._friend_cache: tuple[float, set[str]] = (0.0, set())
        # 工具执行中直发的消息先攒着，本轮历史落盘后再补写（否则会被整轮覆盖）
        self._memo: dict[str, list[tuple[float, str]]] = {}
