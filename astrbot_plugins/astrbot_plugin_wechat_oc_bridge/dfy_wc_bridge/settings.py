"""配置层：只负责读取 config.json，不掺业务逻辑。"""

from __future__ import annotations

import json
import os

from astrbot.api import logger

DEFAULTS = {
    "enabled": True,
    "io_base": "http://127.0.0.1:23392",
    "allow_chats": [],
    "enable_group": False,
    "max_chars": 400,
}


def load_settings(plugin_dir: str) -> dict:
    cfg = dict(DEFAULTS)
    path = os.path.join(plugin_dir, "config.json")
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, encoding="utf-8-sig") as f:
            loaded = json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[wechat_bridge] 读取 config.json 失败: {exc}")
        return cfg
    if isinstance(loaded, dict):
        cfg.update({k: loaded[k] for k in loaded if k in cfg})
    return cfg
