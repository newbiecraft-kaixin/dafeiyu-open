"""微信小号桥接的分层实现。

- settings：配置
- io_client：io-wechat 的 HTTP 调用
- chain：消息链拆解与 /send 落地
- adapter：wechatauto 平台注册（主动/分条发送）
- event：消息 -> AstrBot 事件
- poller：轮询与过滤，投递事件队列
"""
