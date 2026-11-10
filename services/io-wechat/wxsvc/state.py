"""进程级共享状态：附着对象、锁、队列、计数与软禁言记录。

所有模块都通过 `state.xxx` 读写，避免 `from state import wx` 拿到旧引用。
"""

from __future__ import annotations

import os
import threading
from collections import deque
from typing import Any, Dict, List

from infra import Dedupe, EventQueue, Stats

wx: Any = None
bot_name = os.environ.get("WX_BOT_NAME", "")
bot_wxid = ""
# 附着成功的时刻。比它更早的消息是微信库里的历史/停机积压，一律不进管道，
# 避免重启后机器人把旧对话重放一遍。
attach_time = 0.0
wx_lock = threading.Lock()
queue = EventQueue(maxlen=5000)
dedupe = Dedupe()
counts = Stats()

lock = threading.Condition()
# stats 是普通 dict，计数分布在 attach 线程和多个 HTTP 线程；+= 不是原子操作，
# 高并发下会丢计数，统一走 stats_lock。
stats_lock = threading.Lock()
raw_messages: deque = deque(maxlen=2000)
raw_seq = 0
# 已被上游（AstrBot 桥）取走的最后序号：len(raw_messages) 只代表保留的历史，
# 真实积压要用这个游标算，否则面板永远显示 2000 条「堆积」。
raw_read_seq = 0
stats: Dict[str, Any] = {
    "received": 0,
    "sent": 0,
    "groups": 0,
    "duplicate": 0,
    "blocked_night": 0,
}
reply_windows: Dict[str, List[float]] = {}  # uid -> [截止时间, 剩余条数]
send_gate_lock = threading.Lock()
recent_sends: deque = deque(maxlen=300)

# 微信没有“单人禁言”这个客户端能力，这里的禁言是机器人级软禁言：
# 记录 (会话, 成员) 到截止时间，期间该成员的消息不再进管道、机器人不回复。
# 解禁即移除记录。与 QQ 的 set_group_ban 是两种调用行为，指令层保持一致。
mutes: Dict[str, float] = {}
mute_lock = threading.Lock()
