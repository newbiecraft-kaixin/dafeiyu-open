"""io-wechat：PC 微信 4.x（wechatauto / RPA）→ 统一信封。

端口：23392（主，IO_WECHAT_PORT 可覆盖）+ 23336（旧接口兼容，WX_SVC_PORT 可覆盖）

接口
  GET  /health                服务与登录状态（旧格式）
  GET  /events?since=&wait=   管道长轮询取统一信封
  GET  /sessions              会话列表（旧接口）
  GET  /groups                群列表（id + 群名，中枢群管控用）
  GET  /friends               好友列表（id + 昵称，中枢好友管控用）
  GET  /avatar?id=            联系人真实头像（解 head_image.db，中枢懒加载）
  GET  /messages?since&wait   旧版增量消息（桌宠/AstrBot 兼容）
  GET  /mood                  收发计数（旧接口）
  GET  /capabilities          平台能力（仅文字收发 + 通讯录）
  POST /send                  {to, text, exact?, proactive?}

实现分层见 wxsvc/：
  config 常量 / state 共享状态 / inbox 来消息 / window 窗口操作 /
  contacts 会话解析 / sending 发送 / actions 路由处理 / api 路由 / attach 附着

微信平台差异：
  - UIA 单实例串行：所有窗口操作走 wx_lock，窗口必须可见
  - 消息没有可靠 id：Dedupe 退化为「会话+发送者+内容+时间窗」指纹
  - 已移除朋友圈、拍一拍、撤回、引用、@、图片/文件/语音发送：
    这些都需要额外窗口点击或转文件发送，微信侧难以稳定实现
"""
from __future__ import annotations

import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import List

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import serve  # noqa: E402

from wxsvc.api import routes  # noqa: E402
from wxsvc.attach import attach_loop  # noqa: E402
from wxsvc.config import LEGACY_PORT, PORT  # noqa: E402
from wxsvc.state import counts  # noqa: E402
from wxsvc.util import log  # noqa: E402


def main() -> None:
    log(f"starting on 127.0.0.1:{PORT} (legacy {LEGACY_PORT})")
    threading.Thread(target=attach_loop, name="wx-attach", daemon=True).start()

    servers: List[ThreadingHTTPServer] = []
    threads: List[threading.Thread] = []
    for port in {PORT, LEGACY_PORT}:
        try:
            srv = serve(port, routes, service="io-wechat", version="0.3.0", stats=counts)
        except OSError as exc:
            log(f"bind {port} failed:", exc)
            continue
        th = threading.Thread(target=srv.serve_forever, name=f"io-wechat-{port}", daemon=True)
        th.start()
        servers.append(srv)
        threads.append(th)
        log(f"listening on {port}")

    if not servers:
        raise SystemExit("没有可用端口")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    main()
