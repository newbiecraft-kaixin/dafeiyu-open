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

# 语音参考声线：直接扫 cosyvoice_ref/profiles，声线管理里新增的档案不用改代码就能用；
# 姬子/小忆/流萤 保留历史英文 id（himeko/xiaoyi/liuying），其它声线用目录名做 id。
_COSY_REF = find_dir(["cosyvoice_ref"]) or (Path.home() / "dafeiyu" / "cosyvoice_ref")
_VOICE_ALIASES = {"himeko": "姬子", "xiaoyi": "小忆", "liuying": "流萤"}


def _scan_voice_profiles() -> tuple[Dict[str, Path], Dict[str, str]]:
    base = _COSY_REF / "profiles"
    alias_of = {name: alias for alias, name in _VOICE_ALIASES.items()}
    profiles: Dict[str, Path] = {}
    labels: Dict[str, str] = {}
    try:
        names = sorted(item.name for item in base.iterdir() if item.is_dir())
    except OSError:
        names = []
    for name in names:
        key = alias_of.get(name, name)
        profiles[key] = base / name
        labels[key] = name
    if not profiles:  # 档案目录还没准备好时保留内置三套，界面不至于空
        for alias, name in _VOICE_ALIASES.items():
            profiles[alias] = base / name
            labels[alias] = name
    return profiles, labels


VOICE_PROFILES, VOICE_LABELS = _scan_voice_profiles()

MANIFEST_NAMES: Dict[str, str] = {}
