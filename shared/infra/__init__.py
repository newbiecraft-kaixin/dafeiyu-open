"""infra：全平台、全服务共用的基础层（纯标准库，零第三方依赖）。

这里不出现 QQ / 微信的业务规则：平台差异必须在各自服务里消化，
本项目只提供协议、指令框架、去重、限流、重试、HTTP 基座。

导入方式（给 services / plugins 下的脚本用）：
    import sys
    sys.path.insert(0, r"C:\\path\\to\\dafeiyu\\shared")
    from infra import build_envelope, Dedupe, RateLimiter, serve, ok
"""
from __future__ import annotations

__version__ = "0.2.0"

from .command import (
    MENU_ADMIN,
    MENU_NORMAL,
    PLATFORM_ALL,
    ROLE_ADMIN,
    ROLE_USER,
    Command,
    CommandContext,
    CommandRegistry,
    CommandResult,
    Resolution,
    parse_command,
)
from .config import HOST, LONGPOLL_WAIT_MS, PORTS, HTTP_TIMEOUT, base_url, port_of
from .dedupe import Dedupe, extract_message_id, make_key
from .eventqueue import EventQueue
from .protocol import (
    AT_ALL_ID,
    CHAT_TYPES,
    EVENT_KINDS,
    EnvelopeError,
    KNOWN_PLATFORMS,
    MEDIA_KINDS,
    SPACE_ACTIONS,
    at_all,
    bot_mentioned,
    build_envelope,
    build_space_event,
    clean_text_for_bot,
    from_json,
    is_space,
    is_valid,
    make_trace_id,
    mention_names,
    normalize_text,
    to_json,
    validate,
)
from .web import (
    Blob,
    Html,
    Request,
    ServiceHandle,
    Stats,
    data_page,
    fail,
    make_handler,
    ok,
    run,
    serve,
    serve_in_thread,
)
from .ratelimit import RateLimiter
from .retry import DeadLetter, RetryExhausted, RetryPolicy, call_with_retry

__all__ = [
    "HOST",
    "HTTP_TIMEOUT",
    "LONGPOLL_WAIT_MS",
    "PORTS",
    "base_url",
    "Blob",
    "Html",
    "port_of",
    # 协议
    "AT_ALL_ID",
    "CHAT_TYPES",
    "EVENT_KINDS",
    "KNOWN_PLATFORMS",
    "MEDIA_KINDS",
    "SPACE_ACTIONS",
    "EnvelopeError",
    "build_envelope",
    "build_space_event",
    "validate",
    "is_valid",
    "is_space",
    "make_trace_id",
    "normalize_text",
    "bot_mentioned",
    "at_all",
    "mention_names",
    "clean_text_for_bot",
    "to_json",
    "from_json",
    # 指令
    "PLATFORM_ALL",
    "ROLE_USER",
    "ROLE_ADMIN",
    "MENU_NORMAL",
    "MENU_ADMIN",
    "Command",
    "CommandContext",
    "CommandRegistry",
    "CommandResult",
    "Resolution",
    "parse_command",
    # 基础设施
    "Dedupe",
    "make_key",
    "extract_message_id",
    "EventQueue",
    "RateLimiter",
    "RetryPolicy",
    "RetryExhausted",
    "call_with_retry",
    "DeadLetter",
    "Stats",
    "Request",
    "ServiceHandle",
    "ok",
    "fail",
    "data_page",
    "make_handler",
    "serve",
    "serve_in_thread",
    "run",
    "__version__",
]
