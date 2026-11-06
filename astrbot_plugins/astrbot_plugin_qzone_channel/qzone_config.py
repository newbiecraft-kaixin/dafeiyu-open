"""QQ 空间信道的路径、默认配置和接口常量。"""

from __future__ import annotations

import os

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))  # .../data
STATE_PATH = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_qzone_channel", "state.json"
)

DEFAULTS = {
    "enabled": True,
    "poll_interval_minutes": 5,
    "max_feeds": 10,
    "max_replies_per_round": 3,
    # 每条说说累计最多回这么多条：两个 AI 互相回评时会一来一回无限刷，
    # 到上限就闭嘴（人跟人正常聊基本碰不到 20 条）
    "max_replies_per_feed": 20,
    # /看空间 默认只拉几条，拉太多一条条刷屏容易打扰到人
    "view_default_count": 5,
    "view_max_count": 20,
    # /看空间 拉到的说说顺手点个赞（已经赞过的跳过）
    "view_auto_like": True,
    "reply_enabled": True,
    "min_comment_chars": 2,
    "history_turns": 4,
    "owner_notify": True,
    "owner_id": "",
    # 浏览器登录空间后粘贴的 cookie；QQ 重登刷新不了空间登录态时用它兜底
    "manual_cookie": "",
    "system_prompt": (
        "你是大肥鱼，在 QQ 空间回复别人给你说说留的评论。就像朋友圈回评一样："
        "一两句、口语、有情绪，不客套不道歉不客服腔，不用 emoji 颜文字，不加名字前缀。"
        "别人问什么就先接住那个点，别自说自话。"
    ),
    "prefer_provider_id": "deepseek/deepseek-flash",
}

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)
H5_HOME = "https://h5.qzone.qq.com/mqzone/index"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MSG_LIST_CANDIDATES = [
    # NapCat 自己发说说用的是 taotao.qzone.qq.com，列表接口优先按同域试
    ("https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
     "/cgi-bin/emotion_cgi_msglist_v6", "GET"),
    ("https://user.qzone.qq.com/proxy/domain/taotao.qqs1c.qq.com"
     "/cgi-bin/emotion_cgi_msglist_v6", "GET"),
]
RE_FEEDS_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
    "/cgi-bin/emotion_cgi_re_feeds"
)
LIKE_URL = (
    "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com"
    "/cgi-bin/likes/internal_dolike_app"
)
