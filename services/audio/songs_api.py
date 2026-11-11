"""songs（歌曲库）：音源配置 / 搜索 / 入库 / 试听。

属于「音频能力域」的可选扩展：装了就能搜歌、入库、试听；
不装也不影响桌宠对话。由 service.py 装配到 23366 端口。

接口
  GET    /songs/sources        音源列表
  POST   /songs/sources        新增或更新音源（按 id upsert，可开关 enabled）
  DELETE /songs/sources?id=    删除自定义音源（内置三条不可删）
  GET    /songs/search         按音源搜索；source=local 时扫描本地目录
  GET    /songs                已入库歌曲（支持 ?q= 过滤）
  POST   /songs                入库（给 path 校验文件存在，给 url 记为远程）
  DELETE /songs?id=            删除记录（不动本地文件）
  GET    /songs/audio?id=      试听：本地文件按扩展名给 Content-Type，支持 Range；远程 url 跳转

所有第三方音源都 fail-soft：超时/断网/解析失败只返回 fail("音源不可用：...")，
绝不抛出异常导致服务 500。

按职责拆分为子模块，本文件保留为门面、转发全部原有模块级名字：
  songs_common.py     常量与通用小工具（音源定义 / JSON 落盘 / 字段解析）
  songs_storage.py    数据落盘与曲库聚合（sources.json / songs.json / 插件曲库）
  songs_adapters.py   音源适配器（酷狗 / QQ / 网易 / 咪咕 / 本地扫描）
  songs_handlers.py   HTTP handlers 与路由
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from songs_adapters import (  # noqa: E402,F401
    _ADAPTERS,
    _clean_text,
    _http_json,
    _kugou_play_url,
    _kugou_search,
    _local_search,
    _migu_rows,
    _migu_search,
    _netease_play_url,
    _netease_search,
    _qqmusic_play_url,
    _qqmusic_search,
)
from songs_common import (  # noqa: E402,F401
    AUDIO_EXTS,
    BUILTIN_IDS,
    BUILTIN_LOGIN,
    BUILTIN_SOURCES,
    DATA_DIR,
    DEFAULT_LOCAL_DIR,
    MAX_LOCAL_FILES,
    SEARCH_TIMEOUT,
    SONGS_FILE,
    SOURCES_FILE,
    _MIME,
    _UA,
    _as_bool,
    _header,
    _parse_duration,
    _payload,
    _read_json,
    _write_json,
)
from songs_handlers import (  # noqa: E402,F401
    _delete_bot_song,
    _login_required,
    _parse_range,
    _serve_file,
    handle_audio,
    handle_search,
    handle_songs_delete,
    handle_songs_get,
    handle_songs_post,
    handle_sources_delete,
    handle_sources_get,
    handle_sources_post,
    routes,
)
from songs_storage import (  # noqa: E402,F401
    _LOCK,
    _all_songs,
    _bot_library_file,
    _bot_songs,
    _load_songs,
    _load_sources,
    _save_songs,
    _save_sources,
    status,
)

import source_login  # noqa: E402

# 扫码登录路由（酷狗/QQ/网易云）：拆分模块时必须并回总表，
# 不然「开始扫码/二维码/状态/退出登录」全部 404。
routes.update(source_login.routes)
