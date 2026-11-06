"""酷狗登录 Cookie 与在线搜歌的模块级工具（从 main.py 机械迁移）。"""

from __future__ import annotations

import importlib.util as _ilu
import os

from astrbot.api import logger

from .compat import install_helpers

install_helpers(globals())

KUGOU_COOKIE_PATH = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "kugou_cookie.txt"
)
SHARE_DIR = os.path.join(DATA_ROOT, "stickers", "_share")


def _kugou_cookie() -> str:
    try:
        with open(KUGOU_COOKIE_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_kugou_cookie(cookie: str) -> None:
    os.makedirs(os.path.dirname(KUGOU_COOKIE_PATH), exist_ok=True)
    with open(KUGOU_COOKIE_PATH, "w", encoding="utf-8") as f:
        f.write(cookie)


def _load_kugou_login():
    try:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kugou_login.py"
        )
        spec = _ilu.spec_from_file_location("dfy_kugou_login", path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[basic_tools] 酷狗扫码模块加载失败: {type(e).__name__}: {e}")
        return None


def _search_songs(lib, name: str, limit: int = 8) -> list[dict]:
    """在线搜歌：默认酷狗，带上登录 Cookie 才能搜到 VIP 曲。"""
    return lib.search_online(name, limit, _kugou_cookie())
