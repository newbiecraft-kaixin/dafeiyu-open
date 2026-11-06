"""兼容层：核心实现已拆分到 video_core 包，这里保留原有模块级名字。

本文件会被 web_lookup 的 lookup.py 与 face_convert 的 main.py 以顶层模块名
"fy_video_parse" 直接加载（没有包上下文），因此先补一个虚拟包，包内相对导入
才能正常工作；正常按包导入时则直接透传。
"""

from __future__ import annotations

import os
import sys
import types

if __package__ in (None, ""):
    _PKG = "fy_video_core_pkg"
    if _PKG not in sys.modules:
        _mod = types.ModuleType(_PKG)
        _mod.__path__ = [os.path.dirname(os.path.abspath(__file__))]
        sys.modules[_PKG] = _mod
    __package__ = _PKG

from .video_core import *  # noqa: E402,F401,F403
