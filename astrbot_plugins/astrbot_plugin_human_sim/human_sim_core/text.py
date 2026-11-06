"""文本归一化、正则与少量纯函数工具（叶子模块）。"""

from __future__ import annotations

import re
from astrbot.api.event import AstrMessageEvent, MessageChain, filter


# 纯动作请求：整句只有「拍/戳」没有别的诉求，才拦下来本地处理（省一轮 LLM）
POKE_REQUEST_RE = re.compile(
    r"^[\s，,。.!！~～、]*"
    r"(拍一拍|拍拍|戳一戳|戳戳|拍|戳)"
    r"(我|俺|咱|一下我|我一下)?"
    r"(一下|嘛|啊|呀|吧|呗|哦|下)?"
    r"[\s，,。.!！~～、]*$"
)

# 「你还有多少额度 / 余额」：本地直接查 DeepSeek 官方余额接口回一句，不惊动模型
BALANCE_ASK_RE = re.compile(r"(余额|额度|还剩多少|充了多少)")
BALANCE_NOISE_RE = re.compile(r"余额宝|支付宝|花呗|话费|积分|流量|电量|内存|硬盘|磁盘")
# 余额不足的报错：DeepSeek 402 / Insufficient Balance / 各种 quota 文案
BALANCE_ERR_RE = re.compile(
    r"insufficient[\s_]*(balance|quota|funds)|balance\s*(is\s*)?not\s*enough|余额不足"
    r"|exceeded\s+your\s+current\s+quota|payment\s*required|\b402\b",
    re.IGNORECASE,
)

# 纯动作请求：点赞。整句就是「赞 / 赞我 / 点赞 / 给发消息的人点赞」时本地直接点，
# 点完回一句「点了点了」，全程不进 AI —— 模型参与只会多花两秒，还可能自己编
# 一句「今天赞不了」；Daily quota（每人每天 10 赞）也由 QQ 侧限定，本地知道成败即可。
LIKE_REQUEST_WORDS = frozenset({
    "赞", "赞我", "点赞", "给发消息的人点赞",
    "给我点赞", "帮我点赞", "给我点个赞", "帮我点个赞",
    "点个赞", "点个赞呗", "点赞呗", "赞一下", "赞我一下",
    "给我点赞呗", "给我点个赞呗", "求赞", "来个赞", "点个赞吧",
})

# At 段 / @昵称 / 空白 / 标点：只做归一化
# 注意同时吃掉 AstrBot 的 [At:123] 段和 OneBot 的「@昵称」纯文本段——
# 同一个 At 在不同适配器/不同版本里是两种形态，只认前一种会导致整条漏判。
# （「这家店很赞」「太赞了」不该被当成点赞请求，所以下面还有负面词挡板）
_LIKE_NOISE_RE = re.compile(
    # 一切方括号段：[At:123] / [主人(1000000000)] / [图片] / [CQ:at,qq=123]
    # 实测 AstrBot 在部分群里把 At 渲染成「[主人(1000000000)] 赞我」，
    # 只认 [At:xxx] 会导致整条漏判 —— 这里直接整段吃掉，别猜形态。
    r"\[[^\]]{0,80}\]"
    r"|@[^\s，,。.!！?？:：]+"
    r"|[(（]\s*\d{5,}\s*[)）]"      # 裸 QQ 号被括号包住
    r"|\d{5,}"                      # 裸 QQ 号
    # 注意：这个字符类里绝不能放 [ ]，否则「空格 + '['」会抢在方括号规则前面被吞掉，
    # 把 [At:123] 拆成「At」残留（实测踩过）。
    r"|[\s，,。.!！?？~～、·…:：;；\"'“”‘’（）()【】]+"
)

# 称呼前缀：`大肥鱼点赞` / `肥鱼 给我点个赞` 这类把 bot 名字也一起归一化掉
_BOT_ALIAS_RE = re.compile(
    r"^(?:大肥鱼|肥鱼|小鲸鱼|鲸鱼|鱼鱼|鱼哥|大鱼|宝宝|老鱼|鱼宝|主人|老板|主人呀)"
)

_TAIL_PARTICLE_RE = re.compile(r"[啊呀吧呗嘛哦哇啦呢呦哟咯哈嘞]+$")

# 模型幻觉出的「假媒体」：它没有发图能力，却会写出
# 「[图片：一只蓝色小鲸鱼正扭着身体撒娇蹭你] 呼噜～」这种括号旁白，
# 原样进群就是最典型的莫名发言。剥掉括号串只留正文；带冒号才算，
# 免得误伤 face_convert 要转换的 [流泪] 这类真表情码。
HALLUCINATED_MEDIA_RE = re.compile(
    r"\s*\[(?:图片|图像|图|动图|表情包?|贴图|语音|视频)[：:][^\]]{0,60}\]"
)

# 短句兜底：整句就是「(给/帮/替 + 谁) + (点个) + 赞 (+ 我)」这一种结构才算。
# 用 fullmatch 而不是「包含赞」——「点赞之交」「这家店很赞」这类评价句必须挡在外面。
_LIKE_SHORT_RE = re.compile(
    r"^(?:那|就|快|麻烦|你|您|请|先|再){0,3}"
    r"(?:给|帮|替|为|来|求|要|点|顺便)?"
    r"(?:我|俺|咱|[^\s]{1,4})?"
    r"(?:点个|点一下|点下|来一个|一个|个)?"
    r"赞"
    r"(?:一下|一个|个|我)?$"
)
# 反向挡板：这些是评价/否定，不是在要赞
_LIKE_NEGATIVE_RE = re.compile(
    r"(不|别|没|拒绝|算了|免了|赞满|点过|点过了|赞不出|不要)"
)
_LIKE_PRAISE_RE = re.compile(r"(太|很|真|好|超|可|挺|有点)赞")

# 「给 XX 点赞」：带目标的点赞请求。XX 夹在「给/帮/替/为」和「赞」之间，
# 可能是 @段（正文里看不到，得从 event 的 At 组件拿）、裸 QQ 号、或群名片/昵称。
# 实测漏判样本：「给万川姐姐点赞」——旧的 _LIKE_SHORT_RE 要求「点」后必须跟
# 量词（点个/点下），「点赞」直接连着就整条漏掉，只能靠 AI 兜底多花一轮。
_LIKE_TARGET_RE = re.compile(
    r"^(?:那|就|快|麻烦|麻烦你|帮忙|帮我|帮我给|请|你|您|先|再|顺便|去)*"
    r"(?:给|帮|替|为)"
    r"(?P<who>.{1,16}?)"
    r"(?:点个|点下|点一下|点|来个|来一个|一个|个)?"
    r"(?:点赞|赞|赞一下|点个赞)"
    r"(?:一下|一个|个|吧|呗|嘛|呀|啊|哦|啦|了|哈)*$"
)
# 带目标判定的门槛动词：「这家店很赞」「太赞了」这类评价句没有「点」的动作，
# 不得走点赞流程（和 _LIKE_PRAISE_RE/_LIKE_NEGATIVE_RE 同一套挡板思路）
_LIKE_VERB_RE = re.compile(r"点(?:个|下|一下)?赞|赞一下|赞一个|来个赞|点个赞")
# 目标里出现的代词/称呼：指回发消息的人 → 交回旧的「给自己人点」流程
_LIKE_SELF_WORDS = frozenset({"我", "俺", "咱", "自己", "发消息的人", "发消息这位", "本人"})
# 指到机器人头上的（「给你点赞」是夸奖不是请求）→ 不动作，让 AI 接话
_LIKE_BOT_WORDS = frozenset({
    "你", "您", "你呀", "你啦", "机器人", "大肥鱼", "肥鱼", "鲸鱼", "小鲸鱼",
    "鱼鱼", "鱼哥", "大鱼", "宝宝", "老鱼", "鱼宝",
})
# 解析昵称时剥掉的称呼后缀（先整名精确匹配，再逐层剥）
_HONORIFIC_RE = re.compile(
    r"(?:小姐姐|小哥哥|老哥|姐姐|哥哥|大佬|老板|老师|大人|同学|兄弟|美女|帅哥"
    r"|师傅|大夫|桑|酱|君|崽|仔|姐|哥|妹|弟|叔|姨|爷|奶|神|总|工)+$"
)


def _norm_like_text(text: str) -> str:
    """「[At:1000000000] @大肥鱼 点赞！」-> 「点赞」"""
    return _LIKE_NOISE_RE.sub("", text or "")


_REQ_PREFIX_RE = re.compile(r"\[[^\]]{0,80}\]|@[^\s，,。.!！?？:：]+")

# QQ 的引用段在 message_str 里是「[引用消息(元元: 一长段话)] 」这种纯文本
_QUOTE_RE = re.compile(r"\[引用消息\((.{0,600}?)\)\]", re.S)
# 「给他点赞 / 给她点赞」里的「他」得往上找最近一个发言人
_LIKE_PRONOUNS = {
    "他", "她", "它", "ta", "TA", "Ta", "tA",
    "这个人", "那位", "这家伙", "这货", "上一个人", "上面那个",
}
# 「再次点赞 / 再点一次」这类省略宾语的重复请求：要沿用上一次的目标，不能改成给自己点
_LIKE_REPEAT_RE = re.compile(
    r"再次|再点|再赞|再来一?次|又点|又赞|继续点|接着点|又一次|还是他|还是她|同上"
)


def _clean_quote(text: str, bot_names: tuple[str, ...] = ()) -> str:
    """把引用段改写成有归属的说明，别让被引用的旧话算到当前发言人头上。

    message_str 里引用内容是裸文本，以前原样进历史：
    「七月鹊桥仙(1000000000)：[引用消息(大肥鱼: 你今天很乖)] 这个是你」
    小模型读到的就是「主人引用了一段大肥鱼的话」→ 把机器人自己的发言当成主人说的。
    """
    def repl(m: re.Match) -> str:
        raw = (m.group(1) or "").strip()
        m2 = re.match(r"(.{0,40}?)[：:]\s*(.+)$", raw, re.S)
        if not m2:
            return "（引用了一段旧消息，作者不明，不是当前发言人说的）"
        who = m2.group(1).strip()
        body = m2.group(2).strip().replace("\n", " ")
        body = body if len(body) <= 60 else body[:60] + "…"
        if any(n and n in who for n in bot_names):
            return f"（这是你自己（机器人）之前说的，不是当前发言人说的：{body}）"
        return f"（引用了 {who} 的旧消息，不是当前发言人说的：{body}）"

    out = _QUOTE_RE.sub(repl, text or "")
    return out.replace(
        "[引用消息]", "（引用了一段旧消息，作者不明，不是当前发言人说的）"
    )


def _norm_request_text(text: str) -> str:
    """点单短句归一化：「[At:1000000000] 发几个超级表情」-> 「发几个超级表情」。

    以前直接拿 message_str 量长度，@ 段占位（[At:123] 共 16 字）把短句撑到
    20+ 字，点单被长度门槛误判成闲聊直接丢弃 —— 用户眼里就是「让他发他不发」。
    引用段整段剥掉：点单只看新说的话，被引用的旧内容既不算长度也不算意图。
    """
    t = _QUOTE_RE.sub("", text or "")
    t = _REQ_PREFIX_RE.sub("", t)
    return t.strip(" \t\r\n，,。.!！~～")


def _is_like_request(text: str) -> bool:
    """整句是否为点赞请求。

    三层判定，越往后越宽，但每层都有挡板，避免「这家店很赞」被误点：
      ① 白名单精确命中（含剥掉句末语气词）
      ② 剥掉称呼前缀再命中（「大肥鱼 给我点个赞」）
      ③ 短句 + 明确的点赞动词 + 无否定/评价词
    """
    t = _norm_like_text(text)
    if not t:
        return False
    t = _TAIL_PARTICLE_RE.sub("", t)
    if t in LIKE_REQUEST_WORDS:
        return True
    t = _BOT_ALIAS_RE.sub("", t)
    if t in LIKE_REQUEST_WORDS:
        return True
    if len(t) <= 12 and _LIKE_SHORT_RE.fullmatch(t):
        if _LIKE_NEGATIVE_RE.search(t) or _LIKE_PRAISE_RE.search(t):
            return False
        return True
    return False


def _cut_at_sentence_boundary(text: str, cap: int) -> str:
    """超长时只在**句末标点**处收尾；没有句末就整段照发，绝不硬切。

    原来 text[:cap] 会把模型写到一半的话砍在词中间（「…下次记得带点」）；
    上一版改成按逗号切，结果还是半句（「行吧，那我先」）。
    结论：宁可整条长一点，也不能出现半句话——分段发送会替我们处理长度。
    """
    if len(text) <= cap:
        return text
    window = text[: max(cap, int(cap * 1.8))]
    ends = [
        m.end() for m in re.finditer(r"[。！？!?~…]", window) if m.end() >= max(6, cap * 0.5)
    ]
    if ends:
        return window[: ends[-1]].rstrip()
    # 窗口内一个句末都没有 → 说明是长句/被 token 砍断，整段交出去，
    # 让 humanizer 的 _split_long_sentences 按标点分段，而不是这里切半截
    return text
# :名称: 表情占位符：主动发言不走主流水线，必须在输出层自己兜底
_TOKEN_RE = re.compile(r":([^:\s]{1,12}):")
# 模型自选「贴表情回应」：REACT(表情名) / 【REACT(14)】，单独一行，发正文前先剥掉
_REACT_ACTION_RE = re.compile(
    r"[\[【]?\s*REACT\s*[（(]([^)）\]\n]{1,16})[)）\]】]?", re.I
)
# 模型偶尔把冒号打成全角（：西瓜：），半角正则认不出就会原样发进群，这里归一化
_FW_TOKEN_RE = re.compile(r"：([^：:\s]{1,12})[：:]")
# 半角开+全角闭（:害羞：）这种混用也认，任何冒号组合都别漏到聊天窗里
_MIX_TOKEN_RE = re.compile(r"[:：]([^:：\s]{1,12})[:：]")
# 用户明着要表情（"发个表情/斗图/来张图"）：模型没配表情也必须真甩一张，别只回一句话。
# 必须带请求动词才算点单：只提一句「表情包/梗图/贴图」不发表情，
# 否则「这是表情包仓库？」「猫猫虫咖波表情包大全」都会被打成点单。
_STICKER_REQUEST_RE = re.compile(
    r"(?:发|来|整|甩|要|给)[^，,。!！?？]{0,3}?(?:表情包?|梗图|贴图|图)"
    r"|斗图|卖个萌"
)
# 否定/抱怨句不是点单：「不要啊怎么又禁言了」里的「要」不能当成要表情
_NO_STICKER_REQ_RE = re.compile(r"不要|不用|不需要|无需|别发|别来|别再|没让你|谁要")
# face_convert 塞进 message_str 的身份前缀（[主人(123)] / [群友 昵称(123)]）：
# 历史行里已经有「昵称(QQ号)：」了，这里剥掉，避免同一行里出现两套名字把小模型绕晕
_IDENTITY_TAG_RE = re.compile(r"^\[(?:主人|群友|非主人)[^\]]{0,40}\]\s*")
# 非主人话题里被错喊的「主人」称呼。小模型最爱说「主人别担心」「谢谢主人夸奖」，
# 所以除了语气词，还要认别/夸/帮/给这类祈使词；前面有「我/你/他…」的是陈述不是称呼，不动
_OWNER_CALL_RE = re.compile(
    r"(?<![你我他她它的])主人"
    r"(?=你|呀|啊|哦|呢|嘛|啦|别|快|帮|给|请|要|先|说|夸|可|会|真|好|乖|辛|听|看|摸|抱|陪"
    r"|，|,|。|！|!|？|\?|、|~|～|…|\s|$)"
)


def _strip_tokens(text: str) -> str:
    """把各种冒号组合的占位符一起抹掉（落历史、防模型照抄旧消息用）。"""
    return _MIX_TOKEN_RE.sub("", text)


# 同一条回复偶尔会被流水线完整跑两遍（result_decorate → respond 各执行两次，
# 间隔 150~400ms），用户那边就是连收两条一模一样的。这里在发送前按
# 「同一会话 + 同一发送者 + 同一文本 + 几秒内」去重，第二遍直接清掉结果。
_DUP_SEND_WINDOW = 3.0
_DUP_SEND_LAST: dict[str, tuple[float, str]] = {}


def _send_dedupe_key(event: AstrMessageEvent) -> str:
    return f"{event.unified_msg_origin}|{event.get_sender_id() or ''}"


def _send_dedupe_text(event: AstrMessageEvent) -> str:
    result = event.get_result()
    chain = getattr(result, "chain", None) or []
    text = "".join(getattr(c, "text", "") or "" for c in chain)
    return re.sub(r"\s+", " ", _strip_tokens(text)).strip()


# face_convert 会把图片内容翻成一句话塞进 message_str：
#   「[对方发来一张图片，上面写着…]」「[对方发来一条语音…]」是有内容，可以接；
#   「[对方发来一个表情包（像在跟你斗图）…]」没有任何内容，等于还是纯表情包。
# 判「纯媒体」时只放行带内容的描述，斗图描述一律当没说话。
_FY_CONTENT_RE = re.compile(
    r"\[对方发来(?:一张图片|一条语音)|\[对方发来的视频"
    r"|\[对方发来一张卡片|\[对方分享了一个(?:B站视频|视频链接)"
)
# face_convert 还会把 @/拍一拍/表情写成「（喊了你一声）」这种括号说明。
# 整条被括号包住的短句一律当系统说明，不能当复读/接龙素材。
_PAREN_DESC_RE = re.compile(r"^[（(【\[][^）)】\]]{1,30}[）)】\]]$")
