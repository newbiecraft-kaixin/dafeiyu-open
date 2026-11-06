"""video_parse.py 动态加载器（按文件 mtime 热重载）。"""
from __future__ import annotations

import os

_VIDEO_PARSE = None


def _video_parse():
    """动态加载插件目录下的 video_parse.py；用固定 sys.modules 名，保证与 face_convert 共享同一把平台锁。

    改完 video_parse.py 后插件热重载不会清 sys.modules，所以按文件 mtime 判断：
    文件变了就重新执行，免得一直跑旧代码（必须重启 AstrBot 才生效）。
    """
    global _VIDEO_PARSE
    import importlib.util
    import sys

    key = "fy_video_parse"
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "video_parse.py"
    )
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    mod = sys.modules.get(key)
    if mod is not None and getattr(mod, "_FY_MTIME", None) == mtime:
        _VIDEO_PARSE = mod
        return mod
    spec = importlib.util.spec_from_file_location(key, path)
    mod = importlib.util.module_from_spec(spec)
    mod._FY_MTIME = mtime
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    _VIDEO_PARSE = mod
    return mod
