"""共享端口探活：registry / manage / tools 都从这里取，避免重复实现。

只做 TCP 连接判定，不依赖任何第三方库；连接超时保持短，避免拖慢能力聚合。
"""
from __future__ import annotations

from typing import Optional

import socket

TIMEOUT = 0.5


def port_alive(port: Optional[int], timeout: float = TIMEOUT) -> bool:
    """端口是否在监听；端口不是整数时直接判为不可用。"""
    if not isinstance(port, int):
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0
