"""字幕服务的运行参数。"""
from __future__ import annotations

import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "shared"))

from infra import port_of  # noqa: E402

SERVICE = "subtitle"
PORT = port_of("subtitle", 23405)
OLLAMA = os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434")
VISION_MODEL = os.environ.get("SUBTITLE_VISION_MODEL", "qwen3.5:9b")
ASR_MODEL = os.environ.get("SUBTITLE_ASR_MODEL", "small")
MAX_BYTES = 60 * 1024 * 1024
