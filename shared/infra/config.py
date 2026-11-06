"""全局端口与常量。

所有端口都可用环境变量覆盖，规则是服务名大写、横杠换下划线、加 _PORT：
    io-wechat  -> IO_WECHAT_PORT  (默认 23392)
"""
from __future__ import annotations

import os
from typing import Dict, Optional

# 服务名 -> 默认端口
PORTS: Dict[str, int] = {
    # 输入输出层（平台相关，独立进程）；QQ 由 NapCat 直连 AstrBot，不再有 io-qq
    "io-wechat": 23392,
    # 现有微信 RPA 服务（io-wechat 的下游/前置，暂时并存）
    "svc-wechat": 23336,
    # 桌宠中继（桌宠窗口本身 23335）
    "pet": 23334,
    # 静态文档 / 监控页
    "docs": 23390,
    "brain": 23402,
    # 能力层：音频字幕系统（ASR / 画面描述）
    "subtitle": 23405,
    # 模型调度层：身份 → provider/model/key（本地或远程）
    "model": 23406,
    # 统一记忆层：SQLite
    "store": 23407,
    # 能力注册中心 / MCP 工具入口
    "hub": 23408,
    # NapCat（OneBot WS）
    "napcat": 6199,
}

HOST = "127.0.0.1"

# HTTP 客户端默认超时（秒）
HTTP_TIMEOUT = 10.0
# 长轮询默认等待（毫秒）
LONGPOLL_WAIT_MS = 25000

# 信封与去重
MAX_TEXT_CHARS = 20000
DEDUPE_TTL_S = 180.0
DEDUPE_WINDOW_S = 120


def port_of(service: str, default: Optional[int] = None) -> int:
    """取某服务端口：环境变量优先，其次 PORTS，最后 default。"""
    env_key = service.upper().replace("-", "_") + "_PORT"
    raw = os.environ.get(env_key)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    if service in PORTS:
        return PORTS[service]
    if default is not None:
        return default
    raise KeyError(f"未知服务端口: {service}")


def base_url(service: str) -> str:
    return f"http://{HOST}:{port_of(service)}"
