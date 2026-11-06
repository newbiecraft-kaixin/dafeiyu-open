"""harness 调用错误。"""
from __future__ import annotations


class HarnessError(RuntimeError):
    """DSH 调用失败。"""


class SessionStale(HarnessError):
    """--session-id 指向的会话已不存在或不可续用。"""
