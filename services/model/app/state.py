"""model 运行时单例：路由器、计数、最近错误。"""
from __future__ import annotations

import logging
from collections import deque

from infra import Stats, port_of

from router import Router

LOG = logging.getLogger("model")
PORT = port_of("model")
router = Router()
counts = Stats()
_recent_errors: deque = deque(maxlen=20)
