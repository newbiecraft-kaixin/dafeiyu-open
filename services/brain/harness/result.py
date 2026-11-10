"""一次对话的返回结果。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChatResult:
    reply: str
    session_id: str
    latency_ms: int
    role: str
    profile: str
    provider: str = ""
    model: str = ""
