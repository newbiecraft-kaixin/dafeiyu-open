"""视频分析结果与抽帧相关的辅助函数。"""

import math
from dataclasses import dataclass, field


def timestamp(seconds: float) -> str:
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    return f"{minutes:02d}:{seconds:02d}"


def frame_times(duration: float, limit: int) -> list[float]:
    """均匀覆盖首尾；短视频约每四秒取一帧，长视频不超过 limit 帧。"""
    count = min(limit, max(1, math.ceil(duration / 4)))
    end = max(0.0, duration - min(0.5, duration / 2))
    return [end * i / (count - 1) for i in range(count)] if count > 1 else [0.0]


@dataclass
class VideoAnalysis:
    duration: float = 0
    covered: float = 0
    items: list[tuple[float, str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        notes = "；".join(dict.fromkeys(self.notes))
        if not self.items:
            return f"视频暂时未能识别（{notes or '没有可用结果'}），可以请对方描述或重发。"
        header = (
            f"视频总长 {timestamp(self.duration)}，分析范围 00:00–{timestamp(self.covered)}。"
            "画面来自间隔抽帧，不能推断未采样的动作或细节。"
        )
        if notes:
            header += f"注意：{notes}。仅依据已得到的内容回答，不要声称已完整看完。"
        lines = [f"{label}：{text}" for _, label, text in sorted(self.items)]
        return header + "\n" + "\n".join(lines)
