"""NDJSON 事件流解析。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, List


@dataclass
class ParsedStream:
    session_id: str = ""
    texts: List[str] = field(default_factory=list)
    final: str = ""
    error: str = ""
    saw_turn: bool = False

    @property
    def reply(self) -> str:
        return self.final if self.final else (self.texts[-1] if self.texts else "")


def parse_stream(lines: Any) -> ParsedStream:
    """解析 --json 的 NDJSON 事件流；坏行直接跳过，不影响主流程。"""
    parsed = ParsedStream()
    for raw in lines:
        line = str(raw or "").strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "session":
            parsed.session_id = str(event.get("sessionId") or "")
        elif kind == "text":
            value = str(event.get("text") or "")
            if value:
                parsed.texts.append(value)
        elif kind == "final":
            parsed.final = str(event.get("text") or "")
            parsed.saw_turn = True
        elif kind == "status":
            if event.get("phase") in ("turn_start", "step_start"):
                parsed.saw_turn = True
        elif kind == "error":
            parsed.error = str(event.get("message") or "") or "DSH 执行出错"
    return parsed
