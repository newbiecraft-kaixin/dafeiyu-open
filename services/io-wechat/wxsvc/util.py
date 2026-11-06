"""日志。服务由 WMI 拉起时没有控制台，stdout 只作为兜底。"""

from __future__ import annotations

import time
from typing import Any


def log(*args: Any) -> None:
    print(f"[io-wechat {time.strftime('%H:%M:%S')}]", *args, flush=True)
