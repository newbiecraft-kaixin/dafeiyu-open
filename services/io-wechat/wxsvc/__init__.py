"""io-wechat 服务实现包。

分层：
    config    常量与环境变量
    state     共享状态（wx、锁、队列、计数）
    util      日志
    inbox     微信回调 -> 统一信封
    window    前台/可见/让路等窗口操作
    contacts  会话解析（uid、显示名）
    sending   发送（含风控；仅文字）
    actions   /send 处理
    api       旧接口与路由表
    attach    后台附着微信
    ui        可视化状态面板（/ui）
"""

from __future__ import annotations
