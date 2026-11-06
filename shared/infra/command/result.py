"""指令执行结果。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class CommandResult:
    text: str = ""
    handled: bool = True
    action: Optional[Dict[str, Any]] = None
    error: str = ""

    @staticmethod
    def say(text: str) -> "CommandResult":
        return CommandResult(text=text)
