"""hub：能力注册中心 + MCP 风格工具调用。

端口：23408（HUB_PORT 可覆盖）

它只做三件事：
  1. 扫描 services/*/service.json，汇总各服务的能力与在线状态；
  2. 探测 /health，区分「必选」「可选」能力；
  3. 对外暴露 MCP 风格的 tools/list 与 tools/call，让上层不必知道具体端口。

可选能力（例如语音 / QQ / 微信）不在线时，tools/list 自动隐藏对应工具，
上层继续用字幕或其它能力，不需要改代码。

实现已按职责拆到 hubsrv 包，这里只负责装配与启动。
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import run  # noqa: E402

from hubsrv import PORT, routes  # noqa: E402

if __name__ == "__main__":
    print(f"[hub] listening on http://127.0.0.1:{PORT}", flush=True)
    run(PORT, routes, service="hub", version="1.0")
