"""本地探测结果。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProbeResult:
    """本地查到的事实。hint 直接进提示词，facts 留给命令/日志用。"""

    hint: str = ""
    facts: dict = field(default_factory=dict)
