"""本地决策结果。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Verdict:
    action: str          # "reply" 必回 / "silent" 必不回 / "ask" 交模型
    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return f"{self.action}({self.score:+.1f}) " + ",".join(self.reasons)
