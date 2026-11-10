"""命令类 handler 已经回复后，拦截默认 LLM，避免同一条命令被回两遍。

AstrBot 原生 ``ProcessStage`` 在命令 handler 执行完后仍可能继续走默认 LLM
（私聊 / 被 @ 时 ``is_at_or_wake_command=True``，且命令结果是用 yield 发的，
``_has_send_oper`` 仍为 False），用户看到的就是「命令回一遍、模型再回一遍」。

这里不再改 core：注册 ``on_waiting_llm_request`` 钩子，在默认 LLM 请求真正发出前
检查 ``activated_handlers``，发现命令类过滤器就 ``event.stop_event()``。
``call_event_hook`` 在事件被 stop 时返回 True，``InternalAgentSubStage``
（``internal.py`` L226-227）会直接 return，此时还没拿锁、没 build agent、
没调 provider；插件自己发起的 LLM 请求会带 ``provider_request`` 额外信息，这里放行。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_COMMAND_FILTERS = {"CommandFilter", "CommandGroupFilter", "RegexFilter"}


@register(
    "astrbot_plugin_command_guard",
    "dafeiyu-local",
    "命令类 handler 已回复时跳过默认 LLM，避免同一条命令回两遍",
    "v1.0.0",
)
class CommandGuardPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.on_waiting_llm_request()
    async def skip_llm_after_command(self, event: AstrMessageEvent) -> None:
        if event.get_extra("provider_request") is not None:
            # 插件主动发起的 LLM 请求，保持原行为
            return
        activated_handlers = event.get_extra("activated_handlers") or []
        handler_kinds = {
            type(event_filter).__name__
            for handler in activated_handlers
            for event_filter in handler.event_filters
        }
        if handler_kinds & _COMMAND_FILTERS:
            logger.debug("[command_guard] 命令已回复，跳过默认 LLM")
            event.stop_event()
