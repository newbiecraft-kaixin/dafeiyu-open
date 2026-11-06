"""filters 决策层、出站节流 send_gate 与人味清洗器的运行时加载（叶子模块）。"""

from __future__ import annotations

import os
from astrbot.api import logger

from .config import DATA_ROOT, PLUGIN_DIR


# 输入层预处理 + 本地决策层（同目录 filters 包）。
# 走 importlib 按包加载：插件目录名带前缀，不一定能按包名导入。
def _load_filters():
    import importlib.util as _ilu
    import sys as _sys

    pkg_dir = os.path.join(PLUGIN_DIR, "filters")
    path = os.path.join(pkg_dir, "__init__.py")
    try:
        spec = _ilu.spec_from_file_location(
            "human_sim_filters", path, submodule_search_locations=[pkg_dir]
        )
        mod = _ilu.module_from_spec(spec)
        # 必须先注册到 sys.modules：dataclass 解析注解时会用 module.__dict__ 回查，
        # 不注册的话 exec_module 阶段直接 AttributeError（NoneType.__dict__）
        _sys.modules["human_sim_filters"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[human_sim] 决策/探测层加载失败（退回纯模型决策）: {e}")
        return None


filters = _load_filters()


def _load_send_gate():
    """跨插件出站节流（与 humanizer / sticker_pack 共用同一实例）。"""
    import importlib.util as _ilu
    import sys as _sys

    name = "fy_send_gate"
    if name in _sys.modules:
        try:
            return _sys.modules[name].get_gate()
        except Exception:  # noqa: BLE001
            return None
    path = os.path.join(DATA_ROOT, "plugin_data", "fy_send_gate.py")
    try:
        spec = _ilu.spec_from_file_location(name, path)
        mod = _ilu.module_from_spec(spec)
        _sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod.get_gate()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[human_sim] 发送节流模块加载失败（不节流）: {e}")
        return None


send_gate = _load_send_gate()


def _load_cleaner():
    """复用 humanizer 插件的人味清洗器；加载失败就退化成原样返回，绝不影响主流程。"""
    try:
        import importlib.util

        path = os.path.join(
            DATA_ROOT, "plugins", "astrbot_plugin_humanizer", "cleaner.py"
        )
        if not os.path.exists(path):
            return None
        spec = importlib.util.spec_from_file_location("humanizer_cleaner_shared", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.Cleaner(
            {"oral_noise_prob": 0.1, "drop_period_prob": 0.85, "kaomoji_mode": "strip"}
        )
    except Exception as e:
        logger.warning(f"[human_sim] 人味清洗器加载失败（不影响运行）: {e}")
        return None


_CLEANER = _load_cleaner()
