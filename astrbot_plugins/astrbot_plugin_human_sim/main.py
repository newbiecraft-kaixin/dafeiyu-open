"""模拟真人：潜水、插话、冷场开话头、冒泡表情。

设计原则：像人，不像广播。
- 一切主动行为走「概率 + 冷却 + 每分钟限额 + 活动时段」四重闸门
- 插话前随机延迟 4~20 秒，且带最近群聊上下文，接话要接得住
- 夜里（start_hour 之前 / end_hour 之后）绝不说话，真人是会睡觉的
- 全部状态落盘，重启不清零
"""

from __future__ import annotations

from astrbot.api.star import Context, Star, register

# 插件主类由各 mixin 组合而成；方法实现分别在 human_sim_core/<职责>.py。
from .human_sim_core.core import CoreMixin
from .human_sim_core.poke import PokeMixin
from .human_sim_core.probe import ProbeMixin
from .human_sim_core.repeat import RepeatMixin
from .human_sim_core.balance import BalanceMixin
from .human_sim_core.media import MediaMixin
from .human_sim_core.like import LikeMixin
from .human_sim_core.send import SendMixin
from .human_sim_core.commands import CommandsMixin
from .human_sim_core.policy import PolicyMixin
from .human_sim_core.pipeline import PipelineMixin
from .human_sim_core.activity import ActivityMixin

# 兼容转发：保持 main.py 原有模块级名字（供其他插件 / filters.py / 旧代码引用）。
from .human_sim_core.config import (
    CHAIN_FACE_BASE,
    CHAIN_FACE_FAMILY,
    CHAIN_FACE_ID,
    DATA_ROOT,
    DEFAULTS,
    EXTRA_SYSFACE_PATH,
    FACE_CONFIG_CANDIDATES,
    FACE_IDS,
    FACE_REFRESH_SECONDS,
    LOCAL_DECIDE_PROVIDER,
    PLUGIN_DIR,
    REACTION_IDS,
    STATE_PATH,
    STICKER_MAP,
    STICKER_ROOT,
    _PLUS1_RE,
    _chain_family,
    _is_plus1,
)
from .human_sim_core.prompts import (
    ANTI_AI_RULE,
    APOLOGY_RE,
    GRUDGE_TTL,
    INSULT_RE,
    LIKE_ACK_LINES,
    POKE_ACK_LINES,
    POKE_AFFECTION_RE,
    POKE_COLD_LINES,
    POKE_LINES,
    POKE_LINES_NAMED,
    POKE_REPEAT_LINES,
    POKE_REPEAT_LINES_NAMED,
    SYSTEM_PROMPT,
    TASTE_RULE,
    VULGAR_JOKE_RE,
)
from .human_sim_core.text import (
    BALANCE_ASK_RE,
    BALANCE_ERR_RE,
    BALANCE_NOISE_RE,
    HALLUCINATED_MEDIA_RE,
    LIKE_REQUEST_WORDS,
    POKE_REQUEST_RE,
    _BOT_ALIAS_RE,
    _DUP_SEND_LAST,
    _DUP_SEND_WINDOW,
    _FW_TOKEN_RE,
    _FY_CONTENT_RE,
    _HONORIFIC_RE,
    _IDENTITY_TAG_RE,
    _LIKE_BOT_WORDS,
    _LIKE_NEGATIVE_RE,
    _LIKE_NOISE_RE,
    _LIKE_PRAISE_RE,
    _LIKE_PRONOUNS,
    _LIKE_REPEAT_RE,
    _LIKE_SELF_WORDS,
    _LIKE_SHORT_RE,
    _LIKE_TARGET_RE,
    _LIKE_VERB_RE,
    _MIX_TOKEN_RE,
    _NO_STICKER_REQ_RE,
    _OWNER_CALL_RE,
    _PAREN_DESC_RE,
    _QUOTE_RE,
    _REACT_ACTION_RE,
    _REQ_PREFIX_RE,
    _STICKER_REQUEST_RE,
    _TAIL_PARTICLE_RE,
    _TOKEN_RE,
    _clean_quote,
    _cut_at_sentence_boundary,
    _is_like_request,
    _norm_like_text,
    _norm_request_text,
    _send_dedupe_key,
    _send_dedupe_text,
    _strip_tokens,
)
from .human_sim_core.runtime import (
    _CLEANER,
    _load_cleaner,
    _load_filters,
    _load_send_gate,
    filters,
    send_gate,
)


@register(
    "astrbot_plugin_human_sim",
    "dafeiyu-local",
    "模拟真人：潜水插话、冷场开话头、冒泡表情、消息表情回应，全带频控",
    "v1.3.1",
)
class HumanSim(
    CoreMixin,
    PokeMixin,
    ProbeMixin,
    RepeatMixin,
    BalanceMixin,
    MediaMixin,
    LikeMixin,
    SendMixin,
    CommandsMixin,
    PolicyMixin,
    PipelineMixin,
    ActivityMixin,
    Star,
):
    """模拟真人机器人：插件入口只负责组合 mixin 与 AstrBot 注册。"""
