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
    "输入输出层",
    "软件层",
    "能力层",
    "监控层",
    "中枢层",
]
# 服务清单里写的是层名，这里再按服务名兜底归一化，避免旧清单或新服务落到错误的层。
LAYER_BY_NAME = {
    "hub": "中枢层",
    "brain": "中枢层",
    "store": "中枢层",
    "model": "中枢层",
    "astrbot": "输入输出层",
    "io-wechat": "软件层",
    "napcat": "软件层",
    "pet": "监控层",
    "dsh": "监控层",
    "audio": "能力层",
    "voice": "能力层",
    "video": "能力层",
    "stickers": "能力层",
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
