"""兼容层：核心实现已拆分到 web_lookup_core 包，这里保留原有模块级名字。

main.py 通过 spec_from_file_location 以顶层模块名加载本文件（没有包上下文），
因此先补一个虚拟包，包内相对导入才能正常工作；正常按包导入时则直接透传。
"""

from __future__ import annotations

import os
import sys
import types

if __package__ in (None, ""):
    _PKG = "fy_web_lookup_core_pkg"
    if _PKG not in sys.modules:
        _mod = types.ModuleType(_PKG)
        _mod.__path__ = [os.path.dirname(os.path.abspath(__file__))]
        sys.modules[_PKG] = _mod
    __package__ = _PKG

from .web_lookup_core import *  # noqa: E402,F401,F403
