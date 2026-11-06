"""把 DSH（DeepSeek Harness）当主脑执行器：一个角色一套 profile、一份凭据、一组人设。

执行已下沉到 model 服务（services/model）：brain 只负责生成 profile、拼人设、管会话，
通过 POST {model_url}/complete 让 model 层决定 provider / model / key 并调用。
- profile 由 roles.json 幂等生成（package.json / cordis.yml / cordis.patch.yml），
  人设写进 system-prompt.personaPrefix，凭据模式决定用账号还是环境变量里的 API key。
- 所有实际执行工具（文件、命令、联网、子代理、技能）都关掉，主脑只做聊天与判断。
"""
from __future__ import annotations

from .client import Harness, build_key
from .errors import HarnessError, SessionStale
from .profile import (
    DISABLED_TOOL_IDS,
    PROFILE_CORDIS,
    PROFILE_PACKAGE,
    load_persona,
    render_patch,
    strip_markdown_header,
)
from .result import ChatResult
from .sessions import SessionStore
from .stream import ParsedStream, parse_stream

__all__ = [
    "DISABLED_TOOL_IDS",
    "PROFILE_CORDIS",
    "PROFILE_PACKAGE",
    "ChatResult",
    "Harness",
    "HarnessError",
    "ParsedStream",
    "SessionStale",
    "SessionStore",
    "build_key",
    "load_persona",
    "parse_stream",
    "render_patch",
    "strip_markdown_header",
]
