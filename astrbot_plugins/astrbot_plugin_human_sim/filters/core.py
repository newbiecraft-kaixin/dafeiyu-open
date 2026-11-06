"""输入层本地预处理 + 本地决策层的函数实现。

类定义已拆分到同包内的 probe_result / verdict / repeat_hit 模块，
本模块保留原有函数与常量。
"""

from __future__ import annotations

import re
import time

from .repeat_hit import RepeatHit
from .verdict import Verdict

# ---------------------------------------------------------------- 文本归一化

_PUNCT = "，,。.！!？?、~～…·:：;；\"'“”‘’()（）[]【】<>《》 \t\r\n"
_PUNCT_RE = re.compile(f"[{re.escape(_PUNCT)}]")
_AT_RE = re.compile(r"\[At[:：][^\]]*\]|@\S+")
_URL_RE = re.compile(r"https?://\S+")


def norm(text: str) -> str:
    """去掉 @、标点、空白、链接，只留「内容本身」，用于复读比对与兴趣统计。"""
    t = _URL_RE.sub("", text or "")
    t = _AT_RE.sub("", t)
    t = _PUNCT_RE.sub("", t)
    return t.strip().lower()


def tokens(text: str) -> list[str]:
    """极简切词：中文按 2 字滑窗，英文/数字按词。够用来算兴趣重叠，不用上分词器。"""
    t = norm(text)
    if not t:
        return []
    out: list[str] = []
    for m in re.finditer(r"[a-z0-9]{2,}", t):
        out.append(m.group())
    han = "".join(re.findall(r"[\u4e00-\u9fff]", t))
    out.extend(han[i : i + 2] for i in range(len(han) - 1))
    return out


# ---------------------------------------------------------------- 关键词探测

# 每个探测组：(触发词, 需要的目标类型)
# target: "member" = 要说的是某个人，"group" = 说的是这个群，"none" = 不需要目标
PROBE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("头像", "头图", "图像"), "member"),
    (("群名片", "名片", "群昵称", "昵称", "叫什么", "叫啥"), "member"),
    (("群人数", "多少人", "几个人", "满员", "群规模"), "group"),
    (("群主", "管理", "谁管"), "group"),
    (("入群", "进群", "什么时候来", "多久了"), "member"),
    (("资料", "信息", "等级", "几级"), "member"),
]


def probe_kind(text: str) -> str:
    """命中哪类探测；没命中返回 ""。"""
    t = norm(text)
    if not t:
        return ""
    for words, kind in PROBE_RULES:
        if any(w in t for w in words):
            return kind
    return ""


# 明确的「把头像发我」——这种连模型都不用请，本地直接把图甩出去
AVATAR_ASK_RE = re.compile(
    r"(?:发|给我|来|看看|看下|看一下|查|要)(?:一下|一个|个|张)?"
    r"(?:他|她|你|我)?的?头像"
    r"|头像(?:是啥|是什么|呢|长啥样|什么样|有吗|发我|给我)"
)
AVATAR_SELF_RE = re.compile(r"(?:你|你自己)的?头像")


def is_avatar_request(text: str) -> bool:
    t = norm(text)
    if not t or len(t) > 14:
        return False
    if not AVATAR_ASK_RE.search(t):
        return False
    # 带评价的是在吐槽，不是要看图：「这个头像好丑」「头像太糊了」
    if re.search(r"丑|糊|好看|难看|难崩|换了|怎么|为什么|为啥|谁", t):
        return False
    return True


def avatar_url(uid: str, size: int = 640) -> str:
    return f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s={size}"


def fmt_ts(ts: float | int | None) -> str:
    if not ts:
        return "未知"
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (ValueError, OSError):
        return "未知"


def build_member_hint(info: dict, uid: str) -> str:
    """把 get_group_member_info / get_stranger_info 的结果压成一行。"""
    if not info:
        return ""
    nick = str(info.get("nickname") or "").strip()
    card = str(info.get("card") or "").strip()
    parts = []
    if card and card != nick:
        parts.append(f"群名片「{card}」")
    if nick:
        parts.append(f"昵称「{nick}」")
    if info.get("role") == "owner":
        parts.append("群主")
    elif info.get("role") == "admin":
        parts.append("管理员")
    if info.get("level"):
        parts.append(f"群等级 {info['level']}")
    if info.get("join_time"):
        parts.append(f"{fmt_ts(info['join_time'])} 入群")
    if info.get("sex") in ("male", "female"):
        parts.append("男" if info["sex"] == "male" else "女")
    if info.get("age"):
        parts.append(f"{info['age']} 岁")
    parts.append(f"QQ {uid}")
    return "；".join(parts)


def build_group_hint(info: dict) -> str:
    if not info:
        return ""
    parts = []
    if info.get("group_name"):
        parts.append(f"群名「{info['group_name']}」")
    if info.get("member_count"):
        cap = info.get("max_member_count")
        parts.append(
            f"当前 {info['member_count']} 人" + (f"（上限 {cap}）" if cap else "")
        )
    if info.get("owner_id"):
        parts.append(f"群主 QQ {info['owner_id']}")
    return "；".join(parts)


# ---------------------------------------------------------------- 决策层

# 使唤/求助：这类必须回，而且回了要真做事
HELP_WORDS = (
    "帮我", "给我", "替我", "帮忙", "来一个", "来张", "发个", "发一张", "发张",
    "查一下", "查查", "搜一下", "看看", "看下", "教教", "教我", "写个", "写一段",
    "翻译", "算一下", "推荐", "整理", "总结", "解释", "讲讲", "说一下", "出个主意",
    "怎么弄", "怎么办", "能不能", "可不可以", "会不会", "有没有",
)
QUESTION_WORDS = (
    "吗", "呢", "怎么", "为什么", "为啥", "啥", "谁", "哪", "多少", "是不是",
    "如何", "什么", "几时", "多久", "能不能", "有没有",
)
EMOTION_WORDS = (
    "哈哈", "笑死", "草", "卧槽", "我操", "呜呜", "破防", "离谱", "绝了",
    "蚌埠", "麻了", "绷不住", "牛逼", "6", "急了", "乐", "乐死", "好家伙",
    "救命", "离谱", "服了", "无语", "服气", "就这", "啊这",
)
# 纯语气 / 敷衍，接上去很尴尬
FILLER = {
    "嗯", "哦", "啊", "好", "行", "对", "是的", "1", "6", "？", "?", "…",
    "收到", "ok", "好滴", "好的", "知道了", "嗯嗯", "哦哦", "哈哈", "在",
    "在吗", "有人吗", "打卡", "签到", "上", "下",
}
# 负面/求助情绪：这种话哪怕不带疑问词也得认真接，别judge成闲聊
SAD_WORDS = (
    "难受", "难过", "崩了", "破防", "烦", "累死", "想死", "哭", "委屈",
    "被骂", "被开", "分手", "失业", "考砸", "凉了", "完蛋", "求救", "救命",
    "慌", "紧张", "焦虑", "害怕", "惨", "倒霉", "郁闷", "生气", "气死", "emo",
)

REPLY_THRESHOLD = 4.5
SILENT_THRESHOLD = 0.0   # 0 分以下（一点信号都没有）直接判无声，省掉一次请求


def score(
    text: str,
    *,
    directed: bool = False,
    bot_names: tuple[str, ...] = (),
    bot_recent: str = "",
    interest: dict[str, int] | None = None,
    msg_per_30s: int = 0,
    has_image: bool = False,
    has_text: bool = True,
    is_repeat: bool = False,
) -> Verdict:
    """本地打分。返回 reply / silent / ask 三档。

    设计取舍：宁可多问模型一档，也不要把该回的话判死。
    所以 silent 的判据非常保守（只否掉纯语气、纯图、刷屏），
    其余模糊地带一律 ask，交给模型——模型只在「值得」的时候才被叫醒。
    """
    t = norm(text)
    s = 0.0
    why: list[str] = []

    if directed:
        s += 6.0
        why.append("点名")

    if not has_text or (has_image and not t):
        s -= 6.0
        why.append("纯图无字")

    if t in FILLER:
        s -= 4.0
        why.append("纯语气")
    elif len(t) <= 1:
        s -= 3.0
        why.append("太短")

    if any(w in text for w in HELP_WORDS):
        s += 3.2
        why.append("使唤/求助")

    if text.rstrip().endswith(("?", "？")) or any(w in text for w in QUESTION_WORDS):
        s += 2.2
        why.append("疑问")

    if any(w in text for w in EMOTION_WORDS):
        s += 2.0
        why.append("有情绪")

    if any(w in text for w in SAD_WORDS):
        s += 2.0
        why.append("情绪低落")

    # 有人认真打了一长段，基本不是随口敷衍，值得让模型看一眼
    if len(t) >= 15:
        s += 1.2
        why.append("说了不少")

    # 接着我自己的话头说：关键词重叠
    if bot_recent and t:
        mine = set(tokens(bot_recent))
        theirs = set(tokens(text))
        if mine and theirs:
            overlap = len(mine & theirs) / max(3, min(len(mine), len(theirs)))
            if overlap >= 0.34:
                s += 2.6
                why.append("接我的话")

    # 兴趣模型：这话题我以前接过，说明我感兴趣
    if interest and t:
        hits = sum(interest.get(w, 0) for w in set(tokens(text)))
        if hits >= 3:
            s += 1.5
            why.append(f"感兴趣({hits})")

    if is_repeat:
        s += 1.0  # 复读由独立逻辑处理，这里只轻微加权避免判 silent
        why.append("复读中")

    if msg_per_30s > 12:
        s -= 3.5
        why.append("刷屏")

    if any(n and n in text for n in bot_names):
        s += 5.0
        why.append("提到我名字")

    if s >= REPLY_THRESHOLD:
        return Verdict("reply", s, why)
    if s <= SILENT_THRESHOLD:
        return Verdict("silent", s, why)
    return Verdict("ask", s, why)


# ---------------------------------------------------------------- 兴趣模型

INTEREST_CAP = 120      # 最多记多少个词
INTEREST_DECAY = 0.985  # 每次更新整体衰减，老兴趣自然淡出


def interest_learn(interest: dict[str, int], text: str, weight: int = 2) -> dict[str, int]:
    """我被这条消息吸引并回了话 → 把这句的词记进兴趣表。"""
    out = {k: v * INTEREST_DECAY for k, v in (interest or {}).items()}
    for w in set(tokens(text)):
        out[w] = out.get(w, 0) + weight
    if len(out) > INTEREST_CAP:
        out = dict(sorted(out.items(), key=lambda kv: -kv[1])[:INTEREST_CAP])
    return {k: round(v, 3) for k, v in out.items() if v >= 0.5}


# ---------------------------------------------------------------- 复读检测

REPEAT_WINDOW_SECONDS = 90
REPEAT_MIN_LEN = 2
# 复读暗号：+1、1、+10086、233 这类纯数字（可带一个 +）。长度过不了 MIN_LEN
# 但属于典型的「跟风复读」，detect_repeat 会为它开豁免。
_REPEAT_TOKEN_RE = re.compile(r"^\+?[0-9]{1,6}$")


def detect_repeat(
    buf: list[tuple[str, str, float, str]],
    text: str,
    uid: str,
    now: float | None = None,
    window: float = REPEAT_WINDOW_SECONDS,
) -> RepeatHit:
    """buf 元素：(归一化文本, 发送者, 时间戳, 图片URL)

    判定：同一句话（归一化后长度 >= 2）在窗口内被 **至少两个不同的人** 说过
    → 群里在复读，跟着复读一次。同一个人刷两遍不算（那是他自己刷屏）。
    例外：「+1 / 1 / +10086 / 233」这类复读暗号不受最小长度限制——
    norm() 会把 "+" 剥掉只剩 "1"，原本永远过不了 REPEAT_MIN_LEN 的闸。
    """
    now = now or time.time()
    key = norm(text)
    is_token = bool(_REPEAT_TOKEN_RE.match((text or "").strip()))
    if len(key) < REPEAT_MIN_LEN and not is_token:
        return RepeatHit()
    recent = [b for b in buf if now - b[2] <= window]
    holders = {b[1] for b in recent if b[0] == key}
    if len(holders) >= 2 and uid not in holders:
        return RepeatHit(text=text.strip(), count=len(holders) + 1)
    if len(holders) >= 3:
        return RepeatHit(text=text.strip(), count=len(holders) + 1)
    return RepeatHit()


def detect_image_repeat(
    buf: list[tuple[str, str, float, str]],
    url: str,
    uid: str,
    now: float | None = None,
    window: float = REPEAT_WINDOW_SECONDS,
) -> RepeatHit:
    """同一张图被不同的人刷了两遍以上 → 这图火了，跟着发一遍。"""
    if not url:
        return RepeatHit()
    now = now or time.time()
    recent = [b for b in buf if now - b[2] <= window]
    holders = {b[1] for b in recent if b[3] == url}
    if len(holders) >= 2 and uid not in holders:
        return RepeatHit(image=url, count=len(holders) + 1)
    return RepeatHit()
