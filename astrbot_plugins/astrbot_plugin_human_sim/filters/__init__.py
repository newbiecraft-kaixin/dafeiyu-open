"""输入层本地预处理 + 本地决策层。

两个目标：
1. **省 token**：能在本地算出来的东西，绝不花一次模型调用去问。
   - `probe()`  命中「头像 / 群人数 / 群名片 / 谁说的」这类关键词时，
     本地直接调 OneBot 接口取事实，拼成一行【本地已查】塞进提示词。
     模型因此不必发起 get_avatar / get_group_members 这样的工具调用
     （每次工具调用 = 一整轮请求往返，最贵的部分）。
   - `score()`  本地特征打分，把「要不要接话」拆成必回 / 必不回 / 交模型三档，
     只有第三档才真的发起一次决策请求。
2. **像人**：复读、兴趣延续这些行为用本地统计做，不依赖模型。

所有函数都是纯计算（除了 probe 会调接口），不做 IO、不依赖插件实例状态。
"""

from __future__ import annotations

from .core import (
    AVATAR_ASK_RE,
    AVATAR_SELF_RE,
    EMOTION_WORDS,
    FILLER,
    HELP_WORDS,
    INTEREST_CAP,
    INTEREST_DECAY,
    PROBE_RULES,
    QUESTION_WORDS,
    REPLY_THRESHOLD,
    REPEAT_MIN_LEN,
    REPEAT_WINDOW_SECONDS,
    SAD_WORDS,
    SILENT_THRESHOLD,
    _PUNCT,
    _PUNCT_RE,
    _AT_RE,
    _URL_RE,
    _REPEAT_TOKEN_RE,
    avatar_url,
    build_group_hint,
    build_member_hint,
    detect_image_repeat,
    detect_repeat,
    fmt_ts,
    interest_learn,
    is_avatar_request,
    norm,
    probe_kind,
    score,
    tokens,
)
from .probe_result import ProbeResult
from .repeat_hit import RepeatHit
from .verdict import Verdict

__all__ = [
    "ProbeResult",
    "Verdict",
    "RepeatHit",
    "norm",
    "tokens",
    "PROBE_RULES",
    "probe_kind",
    "AVATAR_ASK_RE",
    "AVATAR_SELF_RE",
    "is_avatar_request",
    "avatar_url",
    "fmt_ts",
    "build_member_hint",
    "build_group_hint",
    "HELP_WORDS",
    "QUESTION_WORDS",
    "EMOTION_WORDS",
    "FILLER",
    "SAD_WORDS",
    "REPLY_THRESHOLD",
    "SILENT_THRESHOLD",
    "score",
    "INTEREST_CAP",
    "INTEREST_DECAY",
    "interest_learn",
    "REPEAT_WINDOW_SECONDS",
    "REPEAT_MIN_LEN",
    "detect_repeat",
    "detect_image_repeat",
]
