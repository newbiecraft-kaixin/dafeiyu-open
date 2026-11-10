"""AI 输出行清洗与去重：最近说过的句子太像就不再说（复读机治理）。

从 main.py 拆出来；_RECENT_AI_LINES 这份全局状态只归本模块所有。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# 最近说过的 AI 句子：太像的直接不说，专治「复读机」
_RECENT_AI_LINES: list[str] = []


def _clean_ai_line(s: str) -> str:
    """把 AI 回来的一行收拾成一句：去掉 markdown/引号/换行，限长。"""
    s = re.sub(r"```[\s\S]*?```", "", str(s or ""))
    s = re.sub(r"[*#`>]", "", s)
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip("\"'“”「」『』《》 ")
    return s[:80]


def _line_is_fresh(line: str) -> bool:
    """同一句或八成相似最近说过就不再重复。"""
    key = re.sub(r"\s+", "", line)
    if not key:
        return False
    if any(SequenceMatcher(None, key, old).ratio() > 0.62 for old in _RECENT_AI_LINES):
        return False
    _RECENT_AI_LINES.append(key)
    del _RECENT_AI_LINES[:-8]
    return True
