"""酷狗登录 Cookie 与在线搜歌的模块级工具（从 main.py 机械迁移）。"""

from __future__ import annotations

import importlib.util as _ilu
import json
import os

from astrbot.api import logger

from .compat import install_helpers

install_helpers(globals())

KUGOU_COOKIE_PATH = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_basic_tools", "kugou_cookie.txt"
)
# 音乐库页面（audio 服务）扫码登录的统一存储：用户在那里扫的新鲜 cookie
# 也应该给机器人用，不然插件还拿着早过期的旧 cookie 被酷狗当匿名降成试听。
AUDIO_COOKIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(DATA_ROOT))),
    "services", "audio", "data", "source_cookies.json",
)
SHARE_DIR = os.path.join(DATA_ROOT, "stickers", "_share")


def _service_kugou_cookie() -> str:
    try:
        with open(AUDIO_COOKIES_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
        return str((data or {}).get("kugou") or "").strip()
    except (OSError, ValueError):
        return ""


def _kugou_cookie() -> str:
    """音乐库页面扫的新 cookie 优先，自己扫的旧文件兜底。"""
    cookie = _service_kugou_cookie()
    if cookie:
        return cookie
    try:
        with open(KUGOU_COOKIE_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_kugou_cookie(cookie: str) -> None:
    os.makedirs(os.path.dirname(KUGOU_COOKIE_PATH), exist_ok=True)
    with open(KUGOU_COOKIE_PATH, "w", encoding="utf-8") as f:
        f.write(cookie)
    # 反向同步到音乐库服务，两边登录状态保持一致
    try:
        os.makedirs(os.path.dirname(AUDIO_COOKIES_PATH), exist_ok=True)
        data: dict = {}
        try:
            with open(AUDIO_COOKIES_PATH, encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            pass
        data["kugou"] = cookie
        tmp = f"{AUDIO_COOKIES_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, AUDIO_COOKIES_PATH)
    except OSError as e:  # noqa: BLE001
        logger.warning(f"[basic_tools] 同步酷狗 Cookie 到音乐库失败: {e}")


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
