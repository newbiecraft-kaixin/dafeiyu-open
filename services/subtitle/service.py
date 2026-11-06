"""subtitle：音频字幕系统（能力层）。

职责：
- 语音/视频转写：音频消息、视频文件 → 文字（本地 faster-whisper，CPU int8，免费）
- 画面描述：图片/视频帧 → 一句话画面（本地 Ollama vision）
不承担聊天、记忆、平台收发；调用方是 io 层服务或插件层。

运行环境需要 faster-whisper + numpy + ffmpeg（复用 AstrBot 的 venv）。
实现见 subsvc/：config / records / engines / api。
"""

from __future__ import annotations

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "shared"))

from infra import run  # noqa: E402
from subsvc import PORT, ROUTES, SERVICE  # noqa: E402

if __name__ == "__main__":
    run(PORT, ROUTES, service=SERVICE, version="1.0")
