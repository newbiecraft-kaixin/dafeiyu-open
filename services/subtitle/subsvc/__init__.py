"""subsvc：字幕服务实现（原 service.py 拆分）。

- config.py   运行参数与模型选择
- records.py  最近调用记录（进程内环形缓冲）
- engines.py  音频下载、faster-whisper ASR、Ollama 视觉描述
- api.py      路由处理与可读页面
"""
from __future__ import annotations

from .config import PORT, SERVICE  # 先导入 config：它负责把 shared 加入 sys.path
from .api import ROUTES

__all__ = ["SERVICE", "PORT", "ROUTES"]
