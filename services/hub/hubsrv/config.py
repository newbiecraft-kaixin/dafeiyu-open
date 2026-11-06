"""hub 的常量与路径：端口、分层、音色、探测超时。

只放数据，不 import 其它 hubsrv 模块，避免依赖环。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

from infra import find_dir, port_of

_HERE = Path(__file__).resolve()
ROOT = _HERE.parents[1]          # services/hub
SERVICES_DIR = _HERE.parents[2]  # services
REPO_ROOT = _HERE.parents[3]     # 仓库根
DATA_DIR = ROOT / "data"

PORT = int(os.environ.get("HUB_PORT") or port_of("hub", 23408))

LAYERS = [
    "基础层",
    "输入输出层",
    "接入客户端",
    "中枢层",
    "能力层",
    "外部适配",
]
# 与 docs/dafeiyu-architecture.html 的分层保持一致；服务清单里写的是旧层名，
# 这里按服务名归一化，避免 bus/core 被塞进「外部适配」、dsh 掉到页面最底部。
LAYER_BY_NAME = {
    "hub": "中枢层",
    "brain": "中枢层",
    "store": "中枢层",
    "model": "基础层",
    "math": "基础层",
    "io-wechat": "输入输出层",
    "pet": "接入客户端",
    "gateway": "接入客户端",
    "dsh": "接入客户端",
    "napcat": "接入客户端",
    "voice": "能力层",
    "tts": "能力层",
    "media": "能力层",
    "subtitle": "能力层",
    "plugins": "能力层",
}
PINNED = {"pet", "dsh", "io-wechat"}
TIMEOUT = 1.8

# 语音参考声线（与 dsh-dafeiyu 内置的三套音色同名）
_COSY_REF = find_dir(["cosyvoice_ref"]) or (Path.home() / "dafeiyu" / "cosyvoice_ref")
VOICE_PROFILES = {
    "himeko": _COSY_REF / "profiles" / "姬子",
    "xiaoyi": _COSY_REF / "profiles" / "小忆",
    "liuying": _COSY_REF / "profiles" / "流萤",
}
VOICE_LABELS: Dict[str, str] = {"himeko": "姬子", "xiaoyi": "小忆", "liuying": "流萤"}

MANIFEST_NAMES: Dict[str, str] = {}
