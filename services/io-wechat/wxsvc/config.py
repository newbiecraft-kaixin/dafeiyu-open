"""常量与可调参数：端口、夜间免打扰、桌面让路、会话标识正则。"""

from __future__ import annotations

import os
import re
import time

from infra import port_of

PORT = port_of("io-wechat")
LEGACY_PORT = int(os.environ.get("WX_SVC_PORT", "23336"))
START_AT = time.time()

# 夜间主动消息硬禁：默认 22:00–08:00（本地时区）。
# 夜间只给「刚刚收到用户消息 → 立刻回复」开一个短窗口（默认 120 秒内最多 8 条），
# 窗口外的一切发送（定时关心、查询后的追加播报、延迟总结）全部拦截。
QUIET_START = int(os.environ.get("WX_QUIET_START", "22"))
QUIET_END = int(os.environ.get("WX_QUIET_END", "8"))
NIGHT_REPLY_WINDOW = int(os.environ.get("WX_NIGHT_REPLY_WINDOW", "120"))
NIGHT_REPLY_QUOTA = int(os.environ.get("WX_NIGHT_REPLY_QUOTA", "8"))

# 桌面让路：用户正在用电脑时不抢前台；等他停手（默认 3 秒无输入）再操作。
# 等待上限 90 秒（桥那边读超时 120 秒，留出发送耗时）：只要还在敲键盘鼠标就
# 不把微信顶上来，真停下来了才发。设 WX_DESKTOP_GUARD=0 可关闭。
DESKTOP_GUARD = os.environ.get("WX_DESKTOP_GUARD", "1") != "0"
GUARD_IDLE_SECONDS = float(os.environ.get("WX_GUARD_IDLE", "3"))
GUARD_MAX_WAIT = float(os.environ.get("WX_GUARD_MAX_WAIT", "90"))

# 勿扰应用：模拟器（MuMu / 雷电 / 蓝叠…）独占前台时会锁鼠标，微信一被激活
# 就把它的输入抢走。这类进程在前台时无论空闲多久都不抢窗口，等它让出前台；
# 等满 GUARD_MAX_WAIT 仍在前台就放弃本次发送。可用 WX_DND_PROCS 覆盖名单。
DND_GUARD = os.environ.get("WX_DND_GUARD", "1") != "0"
DND_PROCS = tuple(
    p.strip().lower()
    for p in os.environ.get(
        "WX_DND_PROCS",
        "mumu,nemu,dnplayer,ldplayer,hd-player,bluestacks,memu,nox,"
        "vboxheadless,virtualbox,qemu",
    ).split(",")
    if p.strip()
)

# 启动宽限：附着前这么多秒内的消息仍算新消息（数据库落盘有延迟），
# 更早的一律当作历史丢弃。设 WX_STARTUP_GRACE=0 表示只看附着时间点。
STARTUP_GRACE = float(os.environ.get("WX_STARTUP_GRACE", "5"))

AT_RE = re.compile(r"@([^\s@,，:：]{1,24})")
# 群消息正文形如 "wxid_xxx:\n正文"，前缀对下游没用，顺手去掉
GROUP_PREFIX_RE = re.compile(r"^wxid_[0-9a-zA-Z_]+:\s*\n")
# 微信会话的稳定标识：好友/群/文件传输助手。显示名会重名、会改，不能用来发送。
UID_RE = re.compile(
    r"^(filehelper|wxid_[0-9A-Za-z_-]+|gh_[0-9A-Za-z_-]+|[0-9A-Za-z_-]+@chatroom)$"
)
