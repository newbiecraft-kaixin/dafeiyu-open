"""常量、路径、默认配置与表情/接龙静态表（叶子模块，不依赖其他业务模块）。"""

from __future__ import annotations

import os
import re


# 本文件位于 human_sim_core/ 子包内，上跳一层才是插件目录
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))  # .../data



STATE_PATH = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_human_sim", "state.json"
)
STICKER_ROOT = os.path.join(DATA_ROOT, "stickers")
STICKER_MAP = os.path.join(STICKER_ROOT, "map.txt")
# NapCat 的内置表情表是写死的，新出的超级表情（朵莉亚 524、王者召唤 512…）
# 发出去会报「消息体无法解析」。napcat.mjs 已被 patch 成「内置表查不到时读这个
# 外部表」，所以学到新表情时把 pack/sticker 元数据写进这里，不用再重启。
EXTRA_SYSFACE_PATH = r"C:\path\to\dafeiyu\napcat-win\napcat\fy_extra_sysface.json"

DEFAULTS = {
    "watch_groups": [
        "大肥鱼QQ:GroupMessage:1000000001",
        "大肥鱼QQ:GroupMessage:1000000002",
    ],
    "unlimited_groups": [],      # 名单内的群绕过每分钟限额与冷却（如走官方 API 的群）
    "interject_probability": 0.02,
    "ai_decide": True,            # True=每条群消息都让 AI 自主决定是否回话（替代随机概率）
    "ai_decide_probability": 0.3, # 参与决策的消息抽样比例（1.0=全部），嫌 LLM 调用频繁可调低
    # 按「群人数 + 近 5 分钟活跃度」缩放上面这些自主概率：
    #   人越多越容易刷屏、她的话越容易被淹 → 人数系数递减（100 人=1，2000 人=audience_min_factor）
    #   消息越少越容易冷场 → 活跃度系数反向补偿（0 条时最高，刷屏时最低），免得小群没内容
    # 点名 / @ / 回复 / 指令永远按原概率走，不受这套缩放影响
    "audience_scaling": True,
    "audience_min_size": 100,          # 此人数以下按 1.0 系数，不再奖励
    "audience_max_size": 2000,         # QQ 群上限，到这里降到 audience_min_factor
    "audience_min_factor": 0.25,       # 人数系数的下限（2000 人群 = 0.25）
    "audience_activity_window": 300,   # 活跃度统计窗口：近 300 秒（5 分钟）
    "audience_activity_min": 0.35,     # 活跃度系数下限（消息很多时最多压到 0.35）
    "audience_activity_max": 3.0,      # 活跃度系数上限（一条没有时最多抬到 3.0）
    "audience_size_ttl_seconds": 600,  # 群人数缓存有效期，避免每条消息都问接口
    # 长期没说话就越来越想开口：沉默越久，自主发言概率越往上补，到 silence_ramp_full_minutes
    # 补到 100%；一开口就清零重来（再从基准概率慢慢爬）
    "silence_ramp": True,
    "silence_ramp_start_minutes": 45,   # 沉默超过这么久才开始加概率
    "silence_ramp_full_minutes": 180,   # 沉默到这个时长，概率补到 100%
    # 话题跟自己有关（别人在聊机器人/问我）时提高接话概率；点名（bot_names）本来就必进决策
    "self_topic_keywords": ["机器人", "bot", "Bot", "AI", "人工智能", "小助手", "你刚才"],
    "self_topic_boost": 2.5,
    "ai_decide_per_minute": 2,    # AI 自主回话的每分钟上限/群（0=不限）
    "ai_decide_cooldown_seconds": 90,   # 两次自主回话的最小间隔
    "decision_check_interval_seconds": 8,  # 同群两次决策调用的最小间隔（防请求堆积）
    # —— 跟进窗口：有人 @/点名/引用机器人之后，接下来一段时间专门盯住这个人 ——
    # 只靠概率抽样时，群里稍热闹一点，刚问完的人后面接着说的话就被抽掉了，
    # 聊到一半突然没下文。窗口内改成只看这个人，不再抽样。
    "followup_seconds": 180,            # 专项关注持续时长（秒）
    "followup_max_messages": 8,         # 窗口内最多为 TA 触发几次决策（防连发）
    "followup_min_gap_seconds": 10,     # 窗口内同一个人两次决策的最小间隔（防刷屏）
    "interject_cooldown_minutes": 45,
    "interject_per_minute": 1,    # 旧随机插话路径的每分钟上限/群
    "lurk_check_interval_minutes": 20,
    "group_idle_minutes": 120,
    "group_idle_probability": 0.15,
    "group_idle_per_minute": 1,   # 冷场开话头的每分钟上限/群
    "face_probability": 0.2,
    "sticker_probability": 0.55,
    # —— 拍一拍（全在本地按规则处理，不惊动 LLM）——
    "poke_respond": True,             # 被别人拍时回应（真人不会装死）
    "poke_cooldown_seconds": 20,      # 同群两次被拍的最小间隔（防戳一戳对刷）
    "poke_back_probability": 0.75,    # 被拍后戳回去的概率
    "poke_text_probability": 0.55,    # 被拍后冒一句的概率
    "poke_sticker_probability": 0.35, # 被拍后甩个表情包的概率
    # 被拍后的台词改成「本地随机挑 + 模型补充」：太老/重复的删掉，池子不够就让模型写一条新的
    "poke_line_ai": True,             # 台词池由模型补充（关掉 = 只用内置台词）
    "poke_pool_min": 8,               # 池子低于这个条数就让模型补一条
    "poke_pool_max": 30,              # 池子上限，超出删最老的
    "poke_pool_ttl_days": 30,         # 台词放这么多天后算老，自动删掉
    "poke_repeat_window_seconds": 180,  # 同一个人在这个时间内再拍 = 连拍，必回戳
    "poke_active": True,              # 主动拍一拍总开关（只在回完对方的话后生效）
    "poke_active_probability": 0.18,  # 回完话顺手拍对方一下的概率（AI 自主回话也走这条）
    "poke_active_cooldown_minutes": 20,   # 同群两次主动拍的最小间隔
    "poke_directed_boost": 3.0,       # 对方是点名找你时，回完顺手拍的概率倍数
    "poke_request_local": True,       # 「拍拍我 / 戳我」这类纯动作请求本地直接处理，不进 AI
    "sticker_request_local": True,    # 「发个表情 / 来张图 / 斗图」本地直接甩图，不等 AI（小模型不会调工具）
    "sticker_back_probability": 0.35,        # 群友只甩表情包时，回一个表情斗图的概率（主人/点名必回）
    "sticker_back_cooldown_seconds": 240,    # 同群两次斗图的最小间隔，防两个人对着刷图
    # 点单发现后发哪个表情由模型按当前聊天场景挑（梗图还是超级表情、具体哪一张）；
    # 模型没挑出来/调用失败时才回落到下面的硬规则
    "emoji_ai_pick": True,
    "emoji_ai_pick_history": 6,       # 挑表情时参考最近几条聊天
    "emoji_ai_pick_candidates": 12,   # 给模型看的候选数量（太多会把本地小模型绕晕）
    "like_request_local": False,      # 输入层口语点赞（默认关：统一走 /点赞 命令）
    "like_at_target": True,           # 回话时 @ 上要赞的人：真人交代事情会冲着人说
    # 点赞后的那句话不再让小模型现编（关掉 = 只回固定台词），
    # 小模型不参与“自己决定说什么”，避免它自主发挥
    "like_reply_ai": False,
    "like_reply_max_chars": 16,       # 这句话的字数上限（只在 like_reply_ai 打开时生效）
    "owner_id": "",                   # 主人 QQ：被踢出群时私聊告知（留空 = 只写日志）
    "group_leave_notify": True,       # 被踢 / 主动退群时给主人发私聊
    "member_notice_enabled": True,    # 群友进/退群提醒总开关（消息按群用 /入群 /退群 设置）
    # —— 输入层预处理（本地查、不花 token）——
    "local_probe": True,              # 命中「头像/群人数/群名片/入群时间」等关键词时本地取事实，塞进提示词
    "probe_cache_seconds": 120,       # 同一目标探测结果复用时长（连着问不重复调接口）
    "avatar_local": True,             # 「发我头像」这类明确请求本地直接甩图，连模型都不叫
    # —— 本地决策层（先本地判，再决定要不要叫模型）——
    "decision_layer": True,           # True=本地三分法（必回/必不回/交模型）；False=每条都问模型（费 token）
    "silent_keepalive_probability": 0.05,  # 判无声的按这个比例仍交模型看一眼（不然平淡群里变哑巴）
    "interest_learn": True,           # 我回过的话题记进兴趣表，之后同类话题更容易被判为「该接」
    # —— 复读（对感兴趣的东西跟着来一遍）——
    "repeat_enabled": True,
    "repeat_probability": 0.25,       # 群里在复读时跟一遍的概率
    "repeat_cooldown_seconds": 600,   # 同群复读冷却（不然就成了复读机）
    "image_repeat_probability": 0.15,  # 同一张图被刷起来时，跟着发一遍的概率
    # 系统不替它抽 +1 的奖：撞上感兴趣的话题时就交给 AI 自己判断要不要跟（plus1 工具）
    "interest_echo_probability": 0.0,  # 0 = 关闭随机跟话，只由 AI 决定
    "interest_repeat_min_weight": 4,   # 兴趣权重到多少才配 +1（越高越挑）
    "interest_repeat_max_chars": 40,   # +1 复制的最大字数，太长截断
    # —— +1 接龙（输入层机械拦截：别人连着 +1，我直接原样跟一条）——
    # 默认关：跟不跟由 AI 自己感兴趣决定，系统不随机替它派发
    "plus1_enabled": False,
    "plus1_gap_seconds": 3,           # 同群两次跟队的间隔（同一条接龙只跟一次）
    "plus1_window_seconds": 300,      # 接龙多久没人接就自然散场
    "plus1_max_chars": 60,            # 复制的最大字数，太长截断
    # 充值：绝不主动发，只在别人找我、而 API 余额不足时被动提示一次
    "balance_empty_notice_enabled": True,
    "balance_empty_notice_cooldown_minutes": 10,  # 同一群提示的最小间隔，别每句都刷
    "recharge_url": "https://platform.deepseek.com/top_up",  # DeepSeek 官方充值页
    "recharge_tiers": [10, 20, 50, 100],  # 充值档位（元）：/充值 只发票价表，/充值 10 才给收款码
    # 动态出码：每笔现去充值页点「微信支付」，抓一张 15 分钟有效的新码
    "recharge_mode": "link",          # link=群里只发链接（点开现抓码）；deepseek=任何场景都直接发图；static=固定图
    "recharge_link_base": "https://example.com/recharge",  # 现抓码网页（可自行替换）
    "recharge_script": r"C:\path\to\dafeiyu\recharge_page\ds_qr.py",
    "recharge_python": r"C:\path\to\dafeiyu\astrbot-win\venv\Scripts\python.exe",
    "recharge_qr_timeout": 60,        # 抓码最长等多久（秒），超时只发链接
    "recharge_qr_path": "",           # static 模式用：自己准备好的收款码图片
    "recharge_owner_only": False,     # 充值和余额只认主人 QQ（默认关：潜水池群里也搭理）
    "global_gap_min": 20,        # 防封号：任意两次自主发言的全局最小间隔（秒）
    "global_gap_max": 60,
    "global_per_minute": 3,      # 防封号：所有群自主发言每分钟总量上限（0=不限）
    # 同群所有机器人合计每分钟最多 20 条（跨进程共享，防多机器人互相接话吵起来；
    # 跟 global_per_minute 的「本进程自主发言」是两回事，这条管的是含正常回复在内）
    "shared_minute_cap": 20,
    "shared_rate_db": "",        # 自定义共享计数库路径；留空用 data/plugin_data/fy_rate_shared.db
    "solo_face_probability": 0.06,     # 巡查时纯发黄脸表情的概率（主动发表情，不配文字）
    "solo_sticker_probability": 0.05,  # 巡查时直接甩一张表情包图的概率
    "react_after_reply_probability": 0.25,  # 旧随机路径：回话后 25% 贴表情（概率保留，仅随机模式用）
    "react_only_probability": 0.06,   # 旧随机路径：沉默时 6% 只贴表情（概率保留，仅随机模式用）
    # 贴表情回应改由模型自己抽：算法只摆候选，贴不贴、贴哪个都归模型写 REACT(表情名)
    "react_ai_decide": True,          # 默认开：AI 自己决定
    "react_random_decide": False,     # 旧的两条随机抽奖默认关（回话后 25% / 沉默 6%）
    "react_ai_candidates": 10,        # 一次列给模型看的候选表情数量
    "react_cooldown_seconds": 120,    # 同群两次表情回应的最小间隔（回应不是发言，但要防刷）
    "reaction_ids": [14, 21, 80, 64, 6, 23, 15, 13, 29, 5, 90, 43, 82, 76, 20],
    # 经典黄脸，均可做消息回应：呲牙/偷笑/强/玫瑰/流泪/白眼/惊讶/调皮/憨笑/得意/OK/鼓掌/握手/月亮/吐
    "react_back_on_reaction": True,         # 用户在你自己消息下贴表情回应时，机器人回贴一个（对称互动，感知被回应）
    "reaction_plus1": True,                 # 输入层 +1：别人在同一条消息下连贴同一个表情，跟着贴一个
    "reaction_plus1_cooldown_seconds": 60,  # 同一条消息两次跟贴的最小间隔
    "react_back_text_probability": 0.12,    # 对方用了机器人反应池里的表情（明确正/负向信号）时，小概率额外回一句文字
    "reply_delay_seconds": [4, 20],
    "start_hour": 9,
    "end_hour": 23,
    "night_frequency_scale": 0.25,  # 时段外的频率系数：自主插话/冷场开话头/冒泡表情概率 × 此值（点名回复不受影响；0=夜间完全不说话）
    "max_chars": 60,                  # 闲聊回复长度（一条说完最像人；30 会把话切半）
    "long_max_chars": 800,            # 对方要内容时（讲故事/写东西/详细说）的上限，别拿 30 字砍死
    "long_form_hints": [
        "讲故事", "讲个故事", "编个故事", "编故事", "故事", "小说", "写一篇",
        "写个", "写一篇", "续写", "作文", "剧本", "详细说", "详细讲讲",
        "展开说", "展开讲", "多说点", "长一点", "多写点", "写长", "分析一下",
        "解释一下", "科普", "介绍一下", "诗歌", "段子", "文案",
    ],
    "bot_names": ["大肥鱼", "肥鱼", "鲸鱼", "小鲸鱼", "鱼鱼", "鱼哥", "大鱼"],
    "directed_reply_delay_seconds": [2, 6],
    "at_reply_probability": 0.2,
    "at_last_speaker_probability": 0.2,
    "at_random_member_probability": 0.3,
    "enabled": True,
}

# 常用 QQ 小黄脸 id（官方配置读不到时的兜底）
FACE_IDS = [1, 2, 4, 20, 21, 8, 9, 32, 28, 86, 66, 11]

# QQ 客户端自带的官方表情配置：黄脸和超级表情都在这里，随 QQ 版本/资源更新。
# 运行时直接读它，不把结果静态入库；basic_tools 每周刷新一次并写桌面 txt 核对。
FACE_CONFIG_CANDIDATES = [
    os.environ.get("QQ_FACE_CONFIG", ""),
    os.path.join(
        os.path.expanduser("~"), "Documents", "Tencent Files", "nt_qq", "global",
        "nt_data", "Emoji", "emoji-resource", "face_config.json",
    ),
    os.path.join(
        os.path.expanduser("~"), "dafeiyu", "stack", "napcat", "qq", "nt_qq",
        "global", "nt_data", "Emoji", "emoji-resource", "face_config.json",
    ),
]

# 官方表情清单是动态数据：进程内缓存一周，到期自动重读 face_config.json
FACE_REFRESH_SECONDS = 7 * 24 * 3600

# 消息表情回应兜底池（config 未配置时用）：呲牙/偷笑/强/玫瑰/流泪/白眼/惊讶/调皮/憨笑/得意/OK/鼓掌/握手/月亮/吐
REACTION_IDS = [14, 21, 80, 64, 6, 23, 15, 13, 29, 5, 90, 43, 82, 76, 20]

# 决策/插话固定用本地模型（廉价任务，不走分群路由，不烧云端 token）
LOCAL_DECIDE_PROVIDER = "ollama-chat/qwen3.5-9b"

# +1 接龙：字面「+1 / ＋1 / +10086 / 1」和「把上一条原样粘贴」都算接龙信号
_PLUS1_RE = re.compile(r"^[+＋]*(?:1|10086|1000000|1s|1秒)$")

# QQ「接龙超级表情」（AniStickerType=3）：点「接」会随机发中/大/隐藏变体，
# 同一条接龙里每条消息的 face id 都不一样（火车 419 → 中火车 420 → 大火车 421），
# 按 id 比永远对不上。所以按名字归族：同一族的变体算同一个接龙。
# 新活动接龙表情上线后，在这里补名字即可（中X/大X 会被自动归到 X）。
CHAIN_FACE_FAMILY = {
    "/龙年快乐": "龙年", "/新年中龙": "龙年", "/新年大龙": "龙年",
    "/划龙舟": "龙舟", "/中龙舟": "龙舟", "/大龙舟": "龙舟",
    "/火车": "火车", "/中火车": "火车", "/大火车": "火车",
    "/蛇年快乐": "蛇年", "/蛇身": "蛇年", "/蛇尾": "蛇年", "/灵蛇献瑞": "蛇年",
    # 往届接龙的隐藏款（普通超级表情 type1，但同属一条接龙）
    "/粽于等到你": "龙舟", "/复兴号": "火车",
    # type2 的互动表情（骰子/猜拳/篮球）同样是成套出现的
    "/骰子": "骰子", "/包剪锤": "猜拳", "/篮球": "篮球",
}

# face id → 族：QQ 偶尔只给 id 不带 faceText，两套 id 都认
CHAIN_FACE_ID = {
    "392": "龙年", "393": "龙年", "394": "龙年",
    "415": "龙舟", "416": "龙舟", "417": "龙舟",
    "419": "火车", "420": "火车", "421": "火车",
    "429": "蛇年", "430": "蛇年", "431": "蛇年", "432": "蛇年",
    "422": "龙舟", "423": "火车",
    "358": "骰子", "359": "猜拳",
}


# 族 → 起头用的基础款 face id：素材丢了也能照着原样「接」一个
CHAIN_FACE_BASE = {
    "龙年": 392, "龙舟": 415, "火车": 419, "蛇年": 429,
    "骰子": 358, "猜拳": 359, "篮球": 114,
}


def _chain_family(name: str, sticker_type=None, face_id="") -> str:
    """接龙表情归族：id 优先，其次名字查表；未知的 type3 按「中X / 大X → X」猜。"""
    fam = CHAIN_FACE_ID.get(str(face_id or "").strip())
    if fam:
        return fam
    n = (name or "").strip()
    if not n:
        return ""
    fam = CHAIN_FACE_FAMILY.get(n) or CHAIN_FACE_FAMILY.get("/" + n.lstrip("/"))
    if fam:
        return fam
    if str(sticker_type) == "3":
        bare = n.lstrip("/")
        if bare.startswith(("中", "大")) and len(bare) > 1:
            return bare[1:]
        return bare
    return ""


def _is_plus1(text: str) -> bool:
    return bool(_PLUS1_RE.match(re.sub(r"[ \t\u3000]", "", text or "")))
