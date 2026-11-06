"""复读命中结果。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RepeatHit:
    text: str = ""       # 要复读的内容（归一化前的原文）
    count: int = 0       # 窗口内出现过几次
    image: str = ""      # 需要复读的图片 URL（图片复读）
