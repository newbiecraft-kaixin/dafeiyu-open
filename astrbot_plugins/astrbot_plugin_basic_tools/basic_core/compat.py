"""helpers.py 的统一加载器，以及 mixin 方法的注册归属处理。

AstrBot 用「函数 __module__」判断 handler/tool 属于哪个插件，所以 mixin 模块在
定义类时要临时把模块的 __name__ 指向插件入口 main，类定义结束后再还原。
"""

from __future__ import annotations

import importlib.util as _ilu
import os
import sys

_HELPERS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helpers.py"
)


def load_helpers():
    """整个插件只加载一份 helpers.py（常量与内存缓存不能分叉）。"""
    mod = sys.modules.get("dfy_bt_helpers")
    if mod is not None:
        return mod
    spec = _ilu.spec_from_file_location("dfy_bt_helpers", _HELPERS_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {_HELPERS_PATH}")
    mod = _ilu.module_from_spec(spec)
    sys.modules["dfy_bt_helpers"] = mod
    spec.loader.exec_module(mod)
    return mod


def install_helpers(namespace: dict) -> None:
    """把 helpers.py 的常量/函数注入 mixin 模块命名空间，方法体保持原样。"""
    namespace.update(
        {k: v for k, v in vars(load_helpers()).items() if not k.startswith("__")}
    )


def enter_plugin_main(namespace: dict) -> str:
    """进入类定义前调用：让后续 class/方法带上插件入口 main 的 __module__。"""
    real = namespace.get("__name__", "")
    parts = real.split(".")
    if "basic_core" in parts:
        idx = len(parts) - 1 - parts[::-1].index("basic_core")
        namespace["__name__"] = ".".join(parts[:idx] + ["main"])
    return real


def exit_plugin_main(namespace: dict, real: str) -> None:
    namespace["__name__"] = real
