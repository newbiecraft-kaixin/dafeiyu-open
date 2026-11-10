"""人味清洗核心（纯函数，不依赖 astrbot，可被其他插件复用）。

设计依据：去 AI 味最见效的三招是「压长度、列黑名单、给正反例」。
这里做前两招的工程化：

- 结构层：markdown 标题/粗体/列表/表格/行内代码一律拆成素文本
- 腔调层：AI 样板句整句丢弃，句中腔调词只抠词
- 节奏层：长句在逗号处断开、排比句砍一句、句号一律不留在聊天里（句中转逗号，句末抹掉）
- 噪声层：低概率注入口语词（嗯/其实/就）

受保护片段：`:表情名:`、[表情文字]、URL、代码块、`行内代码`、@提及。
"""

from __future__ import annotations

import random
import re

PLACEHOLDER_RE = re.compile(r":([^:\s]{1,20}):")


def _strip_raw_tool_json(text: str) -> str:
    """切掉「模型把工具调用/流式 chunk 当正文吐出来」的残留。

    小模型（尤其 qwen-9b 本地档）有时会把 OpenAI 流式结构直接写成文字，
    例如「抱歉…请稍等。{"index""tool_calls","delta""assistant",…}」。
    这段一旦发到聊天里很难看，还会被存回上下文，下轮继续模仿。
    """
    for kw in ('"tool_calls"', '"tool_call_id"', '"reasoning_content"'):
        i = text.find(kw)
        if i == -1:
            continue
        cut = text.rfind("{", 0, i)
        if cut == -1:
            cut = i
        return text[:cut].rstrip()
    return text


def _load_sticker_names() -> set[str]:
    """合法表情名 = 表情库索引(map.txt) + face_convert 的黄脸表(face_map.json)。

    两个渲染器认的名字都算合法，避免误删能正常发出的表情。
    """
    names: set[str] = set()
    try:
        import json
        import os

        data_root = os.path.dirname(  # .../data
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        map_path = os.path.join(data_root, "stickers", "map.txt")
        if os.path.exists(map_path):
            with open(map_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    names.add(line.split(":", 1)[0].strip())
        face_path = os.path.join(
            data_root, "plugins", "astrbot_plugin_face_convert", "face_map.json"
        )
        if os.path.exists(face_path):
            with open(face_path, "r", encoding="utf-8") as f:
                names.update(json.load(f).keys())
    except Exception:
        pass
    return names


STICKER_NAMES = _load_sticker_names()

# ---------------- 受保护片段 ----------------
PROTECT_PATTERNS = [
    re.compile(r":[^:\s]{1,20}:"),
    re.compile(r"\[[^\[\]\s]{1,8}\]"),
    re.compile(r"https?://\S+"),
    re.compile(r"```.*?```", re.S),
    re.compile(r"`[^`\n]+`"),
    re.compile(r"@\S{1,20}"),
]

# ---------------- AI 腔：强删除（整句） ----------------
AI_TONE_HARD_RE = re.compile(
    r"^(作为一[个名]\s*(AI|ai|人工智能|语言模型|助手)"
    r"|身为\s*(AI|ai|人工智能|语言模型)"
    r"|(语言|大)模型|人工智能助手|AI\s*助手|智能助手"
    r"|综上(所述)?|总的来说|总而言之|总之|简而言之|一句话总结|总结一下|小结"
    r"|首先|其次|再者|最后一点|另外一点|第[一二三四五][，、,]"
    r"|我希望|希望对你有(所)?帮助|希望(这些|以上|本)?(内容|信息|回答|答案)?(能|可以)?对你有(所)?帮助|希望能(够)?帮到你"
    r"|以下是|如下所示|具体如下|分为以下几点"
    r"|请问有(什么|何)|很高兴(为您|为你)服务|已收到|感谢您的反馈"
    r"|欢迎随时(告诉|联系|提问)|可以随时(告诉|联系)我|随时告诉我|随时(找|喊|联系)我"
    r"|有什么(需要|能|可以)(帮忙|帮你|帮助)(的)?(吗|么|呢|呀)"
    r"|需要(我)?(帮忙|帮助)(的话|吗|么|呢)"
    r"|有(什么|啥)(事|问题)(就|随时|尽管)(找|说|告诉)"
    r"|我(一直|会|就)?在(这里|这儿)(呢|哦|呀)"
    r"|有没有(什么|啥)?(新鲜|好玩|有趣)"
    r"|祝你(一切)?顺利|愿你|期待(与你|和你)的|加油哦|相信你可以的)",
    re.I,
)

# 身份自曝：不管句首句中，整句都不能留（最致命的 AI 味）
AI_IDENTITY_RE = re.compile(
    r"(作为|身为|我是)\s*(一[个名])?\s*"
    r"(AI|ai|人工智能|语言模型|大模型|AI\s*助手|智能助手|聊天机器人|机器人助手)"
    r"|(语言|大)模型(无法|不能|不会)"
    r"|我(并)?(只)?是一[个名]\s*(AI|人工智能|语言模型|程序)"
    r"|我(只)?是(您|你)(的)?(助手|助理|智能助手|聊天助手|机器人助手)",
    re.I,
)

# 客服腔片段：整句未必是套话，但后半截常挂着「有什么需要帮忙的吗」这类客服话术。
# AI_TONE_HARD_RE 整条被 ^ 锚在句首，句中出现的这类话术它抓不到，这里补一个无锚定版本。
AI_SNIPPET_RE = re.compile(
    r"有什么(事情|事)?(需要|能|可以)?(我)?(帮忙|帮你|帮助)(的)?(吗|么|呢|呀)?"
    r"|(需要|要)(我)?(帮忙|帮助)(的话|吗|么|呢)?"
    r"|随时(都)?(可以)?(告诉|找|喊|联系)我(哦|呀|呢|吧|~)?"
    r"|随时告诉我(哦|呀|呢|吧|~)?"
    r"|我(一直|会|就)?在(这里|这儿)(呢|哦|呀|~)?"
    r"|(今天|咱们)?想聊(点|些)?(什么|啥)(呢|呀)?"
    r"|有什么(可以|能|需要)?(帮到|为)(您|你)[^。！？\n]{0,4}(吗|么|呢)?"
    r"|(如果|要是|如)(您|你)?有(其他|别的)?(问题|需要|疑问|需求)[，,。]?(请|随时)?(告诉|找|联系)(我)"
    r"|(如果|要是|如)?(您|你)?有(其他|别的)?(问题|需要|疑问|需求)[^。！？\n]{0,8}(告诉|找|联系)(我)"
    r"|我(会)?(尽力|尽量)(帮助|帮|协助)(您|你)"
)

# 客套开场 / 无能为力：整句丢（「您好！有什么可以帮到您的吗」「抱歉，目前我无法获取…」）
AI_CANNED_RE = re.compile(
    r"^(?:(?:您好|你好|哈喽|hello|hi)[！!，,。]?\s*$"
    r"|(?:您好|你好)[！!，,。]?\s*(?:我是(?:您|你)(?:的)?(?:助手|助理)|很高兴|请问|有什么)"
    r"|抱歉[，,]?\s*(?:目前|现在)?\s*我(?:无法|不能|没办法)(?:获取|提供|查询|回答|满足))",
    re.I,
)
CANNED_REPLY = "咋了"
# 裁完之后只剩这些连接词，说明这半句本来就是纯套话，整句丢掉
EMPTY_FILLER_RE = re.compile(
    r"^((如果|要是|或者|还是|然后|所以|不过|但是|而且|另外|想|就|也|那|这样)"
    r"[你您我]?[，,、]?)+$"
)

# 裁剪客服片段后留下的残句：连词紧贴标点或顶到句尾（「…问题或，请」），修补不了，整句丢
RESIDUAL_CLAUSE_RE = re.compile(r"(?:而且|另外|[或和并但])\s*(?:[，,。！？!?、；;]|$)")

# 模型兜底失败的系统话术（「当前没有可用的工具…」）：整句丢弃
AI_SYSTEM_FALLBACK_RE = re.compile(
    r"^(当前)?没有(可用|能使用|可以使用的)?(的)?(工具|技能|能力)"
    r"|进行简单的文本交流|处理(您|你)的请求"
)
SYS_FALLBACK_REPLY = "这个我整不了呀"

# 工具失败后的「道歉 + 播报重试」：真人不会边道歉边播音，整句丢弃
AI_STALL_RE = re.compile(
    r"^(?:那|好|嗯)?[，,]?\s*(?:请)?稍等(?:片刻|一下|一会儿)?(?:哦|呀|呢)?[。！？!?~…]?$"
    r"|^(?:我)?(?:马上|立刻|现在|这就|先)(?:重新|再|继续)?"
    r"(?:尝试|试|来|查|看|获取|弄)(?:一次|一下|下)?(?:哦|呀)?[。！？!?~…]?$"
    r"|^(?:别担心|不用担心|别急)[，,]?(?:我)?(?:再|重新|马上)?"
    r"(?:尝试|试|来|查)(?:一次|一下)?(?:哦|呀)?[。！？!?~…]?$"
    r"|^(?:那|好|嗯)?[，,]?\s*(?:请)?稍等[^。！？\n]{0,14}?(?:重新|再|继续)?"
    r"(?:尝试|试|来|查|看|获取|弄|处理)"
)
AI_EXCUSE_RE = re.compile(
    r"(?:尝试|刚才|刚刚|稍后|等下)[^。！？\n]{0,30}(?:技术问题|网络问题|出了点问题|卡住了?)"
    r"|(?:技术问题|网络问题)[^。！？\n]{0,10}(?:稍后|稍等|重试|再试)"
)
STALL_REPLY = "网卡了，等下再弄"

# 规则/设定播报 + 工具内部状态播报：
# 「『主人』这个称呼我只认你一个人」「这条得改改」「按我的设定」
# 「工具只说调用成功」「我这边看不到到没到」「日志里返回成功」。
# 真人不会把自己的人设规则、接口返回值拿出来讲，这类整句一律丢。
AI_META_RE = re.compile(
    r"(这个|那个)?称呼[^。！？\n]{0,12}(只认|只叫|只承认|只认你)"
    r"|这条(得|要|该)(改|改改|调整)"
    r"|按(照)?(我|我的)(设定|规则|人设)"
    r"|我的(设定|规则|人设)(是|里|说)"
    r"|(工具|接口|系统)(只)?(说|返回|提示|显示)[^。！？\n]{0,12}(成功|失败|错误)"
    r"|(调用|接口)[^。！？\n]{0,8}(成功|返回|失败)"
    r"|我(这边|这里)?(也)?(看不到|没法确认)[^。！？\n]{0,8}(到没到|有没有到|有没有发)"
    r"|(到没到|有没有到)[^。！？\n]{0,10}(看不到|没法确认)"
    r"|日志[^。！？\n]{0,12}(返回|显示|说|写)"
    r"|(库里|图库|表情库)[^。！？\n]{0,10}(没有|没)[^。！？\n]{0,8}(表情|图)"
    r"|(这次)?先不发[，,][^。！？\n]{0,8}(文字|打字)(就行|吧)"
    r"|图(收不进来|收不成|没收到)"
)

# ---------------- AI 腔：弱清洗（只抠词） ----------------
AI_TONE_SOFT_RE = re.compile(
    r"(我理解你的感受|这很正常|可以理解|值得注意的是|需要指出的是|值得一提的是"
    r"|需要注意的是|请注意|需要提醒的是|务必注意|确实|诚然|不妨|颇为|或许|亦是"
    r"|希望对你有(所)?帮助)"
)

# ---------------- markdown ----------------
MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
MD_BOLD_RE = re.compile(r"(\*\*\*|\*\*|__)(.+?)\1", re.S)
MD_ITALIC_RE = re.compile(r"(?<![\w*])(\*|_)(?!\s)(.+?)(?<!\s)\1(?![\w*])", re.S)
MD_LIST_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,2}[.)])\s+", re.M)
MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.M)
MD_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$", re.M)
MD_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]{4,}\|?\s*$", re.M)
MD_LINK_RE = re.compile(r"\[([^\[\]]+)\]\((https?://[^)\s]+)\)")

ORAL_HEAD = ["嗯", "其实", "诶", "那个", "啊这"]
SHORT_REPLIES = ["嗯", "？", "行", "6", "哦", "……", "啥"]

MAX_SENT_CHARS = 34
MAX_SENTENCES = 3      # 单条回复最多几句（超出并进最后一句，不删内容）
REPEAT_THRESHOLD = 0.72

# ---------------- 颜文字 / 符号脸（真人不打字画脸，改发真表情） ----------------
# 纯符号括号组：(¬‿¬) (￣▽￣) :) —— 括号内没有文字/数字才算符号脸
KAO_PAREN_RE = re.compile(r"[(（][^\w\s()（）<>]{1,8}[)）]")
# 括号旁白：只剥「语气/情绪标签」这类纯旁白（（笑）（小声）（叹气））。
# 动作描写（攥住手腕、埋进颈窝）必须原样留着——人设要求动作写在正文里，
# 以前这里是 [（(][^（）()]{1,20}[）)] 一刀切，动作全被吃掉，看着就像人设没生效。
PAREN_NOTE_WORDS = (
    "笑|干笑|轻笑|苦笑|坏笑|冷笑|微笑|大笑|偷笑|哼笑"
    "|叹气|轻叹|长叹|无奈|沉默|认真|正经|小声|低声|轻声"
    "|嘟囔|嘀咕|嘟嘴|撇嘴|皱眉|脸红|心虚|得意|摊手|耸肩|委屈|补充"
)
PAREN_NOTE_RE = re.compile(
    rf"[（(]\s*(?:{PAREN_NOTE_WORDS})\s*[。，…~～]*\s*[）)]"
)
# 无括号散装符号脸
KAO_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(QAQ|T_T|TvT|T▽T|OTZ|orz|>_<|>﹏<|╯°□°|⊙_⊙|・ω・|´・ω・)(?![A-Za-z0-9])"
)

def _load_kaomoji_map() -> dict[str, str]:
    """从 sticker_pack 的映射表抽 颜文字->黄脸名（known kaomoji 可转占位符）"""
    try:
        import os
        import re as _re
        src = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "plugins", "astrbot_plugin_sticker_pack", "faces.py",
        )
        with open(src, encoding="utf-8") as f:
            m = _re.search(r"KAOMOJI_TO_FACE[^=]*= (\{.*?\})", f.read(), _re.S)
        return eval(m.group(1)) if m else {}
    except Exception:
        return {}

KAOMOJI_TO_FACE = _load_kaomoji_map()

# ---------------- Unicode emoji ----------------
# 免费小模型特别爱用 😊🐟🌊💤🐳 收尾，QQ 里一眼就是「AI 写的」。
# 真人要么不发，要么发真表情包；这里统一剥掉（占位符已先被 _protect 藏起来，不受影响）。
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"    # 各类 pictograph / emoji（🐟🌊💤🐳😊👍💯…）
    "\U00002600-\U000027BF"    # misc symbols & dingbats（❤✨⭐✅…）
    "\U00002B00-\U00002BFF"    # 杂项符号（⬛⬜⭐…）
    "\U0000FE00-\U0000FE0F"    # variation selector（emoji 后缀）
    "\U0001F1E6-\U0001F1FF"  # 国旗区域指示符
    "\U0000200D"               # ZWJ 连接符
    "]"
)

DEFAULT_CFG = {
    "strip_markdown": True,
    "strip_ai_tone": True,
    "oral_noise_prob": 0.15,
    "kaomoji_mode": "convert",  # convert=库里有的转 :黄脸名: 占位符/其余剥掉；strip=全部剥掉
    "strip_emoji": True,
}


def jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class Cleaner:
    """把一段模型输出洗成人话。无状态，可复用。"""

    def __init__(self, cfg: dict | None = None):
        self.cfg = dict(DEFAULT_CFG)
        if cfg:
            self.cfg.update(cfg)

    # ---------- 保护 / 还原 ----------
    @staticmethod
    def _drop_unknown_placeholders(text: str) -> str:
        """模型常瞎编 :sleepy: / :狗头: 这种库里没有的名字，留着就会以字面文本发出去。
        库里有名字表时按表剔除；没有表时至少把纯英文名剔掉。"""

        def check(m: re.Match) -> str:
            name = m.group(1)
            if name.isdigit():
                # 「12:12:34」里的 :12: 是时间不是表情名，别把数字冒号吞了
                return m.group(0)
            if STICKER_NAMES:
                return m.group(0) if name in STICKER_NAMES else ""
            return "" if name.isascii() else m.group(0)

        def fix_fw(m: re.Match) -> str:
            # 全角冒号同样是占位符（：吃瓜：），库里有这个名字才转半角交给下面渲染；
            # 纯英文名（：sleepy：）一定不是正文，直接抹；中文原名留着别误删正文
            name = m.group(1)
            if name.isdigit():
                # 「时间：12:30」这类数字紧跟冒号不是占位符，原样保留
                return m.group(0)
            if STICKER_NAMES and name in STICKER_NAMES:
                return f":{name}:"
            return "" if name.isascii() else m.group(0)

        # 模型常把冒号打成全角（：sleepy: / ：吃瓜：），先归一化再判断
        text = re.sub(r"：([^：:\s]{1,20})[：:]", fix_fw, text)
        return PLACEHOLDER_RE.sub(check, text)

    @staticmethod
    def _protect(text: str) -> tuple[str, list[str]]:
        vault: list[str] = []

        def stash(m: re.Match) -> str:
            vault.append(m.group(0))
            return f"\x00{len(vault) - 1}\x00"

        for pat in PROTECT_PATTERNS:
            text = pat.sub(stash, text)
        return text, vault

    @staticmethod
    def _restore(text: str, vault: list[str]) -> str:
        for i, item in enumerate(vault):
            text = text.replace(f"\x00{i}\x00", item)
        return text

    # ---------- 各层清洗 ----------
    @staticmethod
    def _strip_emoji(text: str) -> str:
        """剥掉 Unicode emoji 与变体选择符，顺手收掉剥完留下的多余空白。

        剥干净后 "嗨！你好呀！😊 今天…" 不会留下 "！  " 这种空档，
        也不会被分条逻辑把孤零零的 🐟🌊 切成单独一条气泡。
        """
        text = EMOJI_RE.sub("", text)
        text = re.sub(r"[ \t]+(?=[。，！？、；：~…])", "", text)
        text = re.sub(r"(?<=[。，！？、；：~…])[ \t]+", "", text)
        return re.sub(r"  +", " ", text)

    def _strip_markdown(self, text: str) -> tuple[str, bool]:
        before = text
        text = MD_HR_RE.sub("", text)
        text = MD_TABLE_SEP_RE.sub("", text)
        text = MD_HEADING_RE.sub("", text)
        text = MD_QUOTE_RE.sub("", text)
        text = MD_LINK_RE.sub(lambda m: m.group(1), text)
        if "|" in text:
            lines = []
            for line in text.splitlines():
                if line.count("|") >= 2:
                    cells = [c.strip() for c in line.strip().strip("|").split("|")]
                    cells = [c for c in cells if c]
                    if cells:
                        lines.append("、".join(cells))
                        continue
                lines.append(line)
            text = "\n".join(lines)
        text = MD_BOLD_RE.sub(lambda m: m.group(2), text)
        text = MD_ITALIC_RE.sub(lambda m: m.group(2), text)
        text = MD_LIST_RE.sub("", text)
        return text, text != before

    def _drop_ai_sentences(self, text: str, keep_breaks: bool = False) -> tuple[str, int]:
        parts = re.split(r"(?<=[。！？!?~…\n])", text)
        kept, dropped = [], 0
        sys_hit = False
        canned_hit = False
        meta_hit = False
        for s in parts:
            head = s.lstrip()
            if head and AI_SYSTEM_FALLBACK_RE.search(head):
                # 模型兜底失败的客服系统话术，整句不能留
                dropped += 1
                sys_hit = True
                continue
            if head and AI_META_RE.search(head):
                # 播报自己的「设定/规则」或工具返回状态，真人不会这么说
                dropped += 1
                meta_hit = True
                continue
            if head and (
                AI_CANNED_RE.search(head)
                or AI_TONE_HARD_RE.search(head)
                or AI_IDENTITY_RE.search(head)
            ):
                dropped += 1
                canned_hit = True
                continue
            # 句首没命中、但句中挂着客服话术：「大肥鱼在群里呢，有什么需要帮忙的吗」
            if head and AI_SNIPPET_RE.search(head):
                ms = list(AI_SNIPPET_RE.finditer(s))
                trimmed = AI_SNIPPET_RE.sub("", s)
                trimmed = re.sub(r"[，,、；;]{2,}", "，", trimmed)
                trimmed = re.sub(r"([。！？!?~…])[，,、；;]+", r"\1", trimmed)
                trimmed = re.sub(r"[，,、；;]+([。！？!?~…])", r"\1", trimmed)
                trimmed = re.sub(r"^[，,、；;~\s]+", "", trimmed)
                trimmed = re.sub(r"[，,、；;~\s]+$", "", trimmed)
                # 套话正好在句末：原句的「？」「！」是给那句套话的，跟着一起走
                if ms and not s[ms[-1].end():].strip("，,、；;。！？!?~… \n"):
                    trimmed = trimmed.rstrip("，,、；;。！？!?~… ")
                core = trimmed.strip("，。！？!?、；;~… \n")
                if len(core) < 4 or EMPTY_FILLER_RE.match(core):
                    # 裁完只剩下「或者」「如果你，或者」这种残渣，说明本来就是个空套话
                    dropped += 1
                    continue
                if RESIDUAL_CLAUSE_RE.search(trimmed):
                    # 裁完剩下「…问题或，请」这种连词贴标点的断句，修补不了，整句丢
                    dropped += 1
                    continue
                s = trimmed
            kept.append(s)
        if dropped and not "".join(kept).strip("，。！？!?、:：;；~… \n"):
            # 整条全是套话：别沉默，留第一句剥掉套话前缀
            first = parts[0].lstrip() if parts else ""
            stripped = AI_TONE_HARD_RE.sub("", first).lstrip("，,。、:： ")
            if (
                stripped
                and not AI_CANNED_RE.search(stripped)
                and not AI_SNIPPET_RE.search(stripped)
                and not AI_SYSTEM_FALLBACK_RE.search(stripped)
                and not AI_META_RE.search(stripped)
                and not RESIDUAL_CLAUSE_RE.search(stripped)
            ):
                kept = [stripped]
                dropped -= 1
        if sys_hit and not "".join(kept).strip("，。！？!?、:：;；~… \n"):
            # 整条都被系统兜底话术丢空了：换一句人话，别发空串
            kept = [SYS_FALLBACK_REPLY]
        # 整条都是「您好/抱歉，我无法…」这类客服腔：换成一句随口的「咋了」
        if canned_hit and not "".join(kept).strip("，。！？!?、:：;；~… \n"):
            kept = [CANNED_REPLY]
        if meta_hit and not "".join(kept).strip("，。！？!?、:：;；~… \n"):
            # 整条都在讲设定/接口状态，丢空后随便应一声，别发空串
            kept = [random.choice(SHORT_REPLIES)]
        # 工具失败时的「道歉 / 请稍等 / 马上重试」整句丢掉；全丢空就换一句人话
        stall_free = [
            s for s in kept
            if not (AI_STALL_RE.search(s.lstrip()) or AI_EXCUSE_RE.search(s.lstrip()))
        ]
        if len(stall_free) != len(kept):
            dropped += len(kept) - len(stall_free)
            kept = stall_free
            if not "".join(kept).strip("，。！？!?、:：;；~… \n"):
                kept = [STALL_REPLY]
        text = "".join(kept)
        if AI_TONE_SOFT_RE.search(text):
            text = AI_TONE_SOFT_RE.sub("", text)
            text = re.sub(r"([。！？!?~…])[，,、]+", r"\1", text)
            text = re.sub(r"[，,]\s*(?=[，,。！？!?])", "", text)
            text = re.sub(r"^[，、,;；\s]+", "", text)
            text = re.sub(r"[，,]{2,}", "，", text)
        # 闲聊要把空行压掉（一句一条最像人）；长文正相反，分段就是排版
        if not keep_breaks:
            text = re.sub(r"\n{2,}", "\n", text)
        return text, dropped

    @staticmethod
    def _merge_list_lines(text: str) -> str:
        """清洗后剩一堆又短又齐的行（典型 AI 清单）→ 并成一句话。"""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 3:
            return text
        short = [ln for ln in lines if len(ln) <= 14]
        if len(short) < len(lines) * 0.7:
            return text
        return "，".join(ln.rstrip("，,、。") for ln in lines)

    @staticmethod
    def _split_long_sentences(text: str) -> str:
        """超长行在标点处断开（用句号，让分段发送拆成多条像随手打的）。

        只在标点边界断——绝不切在 @昵称 / 英文单词 / 表情占位符中间。
        原来按字数硬切，切出来的「@逍遥古」「记得带点」这种半截词
        比一大段更难看得多。单段真没标点时才退让到空格/@ 前。
        """
        out_lines = []
        for line in text.splitlines():
            if len(line) <= MAX_SENT_CHARS:
                out_lines.append(line)
                continue
            # 先按标点切成小段，段内不再动刀
            chunks = re.findall(r"[^，,；;、。！？!?~…]+[，,；;、。！？!?~…]?", line)
            pieces, buf = [], ""
            for chunk in chunks:
                if buf and len(buf) + len(chunk) > MAX_SENT_CHARS:
                    pieces.append(buf.rstrip("，,；;、"))
                    buf = ""
                buf += chunk
            if buf.strip():
                pieces.append(buf.rstrip("，,；;、"))
            # 兜底：单个没有标点的超长段，退让到最近的空格 / @ 之前
            safe: list[str] = []
            limit = MAX_SENT_CHARS * 2
            for p in pieces:
                while len(p) > limit:
                    idx = max(p.rfind(" ", 0, limit), p.rfind("@", 0, limit))
                    if idx <= 0:
                        idx = limit
                    safe.append(p[:idx].strip())
                    p = p[idx:].strip()
                if p:
                    safe.append(p)
            out_lines.append(Cleaner._join_pieces(safe))
        return "\n".join(out_lines)

    @staticmethod
    def _join_pieces(pieces: list[str]) -> str:
        """把同一超长行的若干小段拼回去：前一段已有终止标点就不再补句号。

        原来无条件 "。".join，导致 "…怎么样呀？" + "。" + "有没有…" 出现 "？。"。
        """
        merged = ""
        for x in pieces:
            x = x.strip()
            if not x:
                continue
            if merged and not merged.endswith(
                ("。", "！", "？", "!", "?", "~", "…", "，", ",", "、", "；", ";", "：", ":")
            ):
                merged += "。"
            merged += x
        return merged

    @staticmethod
    def _kill_parallel(text: str) -> str:
        """三句以上长度相近且句尾同字 → 排比对仗，去掉机械感。

        原来直接 group[:2] 把后面几句删掉——AI 只要写成「…了。…了。…了。」
        第三句就整段消失，用户看到的就是话没说完。现在改成合并：
        排比的整齐感没了，信息一句不少。
        """
        sents = [s for s in re.split(r"(?<=[。！？!?~…])", text) if s.strip()]
        if len(sents) < 3:
            return text
        out, i = [], 0
        while i < len(sents):
            group = [sents[i]]
            j = i + 1
            while (
                j < len(sents)
                and abs(len(sents[j]) - len(sents[j - 1]))
                <= max(3, len(sents[j - 1]) * 0.3)
                and sents[j].strip()[-1:] == sents[j - 1].strip()[-1:]
            ):
                group.append(sents[j])
                j += 1
            if len(group) >= 3:
                body = "，".join(
                    g.strip().rstrip("。！？!?~…") for g in group[1:] if g.strip()
                )
                end = group[-1].strip()[-1:] or "。"
                if end not in "。！？!?~…":
                    end = "。"
                out.append(group[0].strip() + body + end)
            else:
                out.extend(group)
            i = j
        return "".join(out)

    @staticmethod
    def _cap_sentences(text: str, cap: int = MAX_SENTENCES) -> str:
        """句数封顶：超出的句子**并进最后一句**，不删。

        原来是直接丢弃多余句子 → 用户看到的就是话说到一半（「下次记得带点」后面没了）。
        群聊里宁可一条稍长，也不能把句子切断；纯文本没有信息损失才重要。
        """
        if cap <= 0:
            return text
        sents = re.split(r"(?<=[。！？!?~…\n])", text)
        sents = [s for s in sents if s.strip()]
        if len(sents) <= cap:
            return text
        head = "".join(sents[: cap - 1]).rstrip() if cap > 1 else ""
        rest = "".join(sents[cap - 1:]).strip()
        if not head:
            return rest
        # head 已以任意标点收尾时不要再叠逗号（原来 "呀！" + "，" → "呀！，"）
        joiner = "" if head.endswith((
            "，", ",", "、", "：", ":", "；", ";",
            "。", "！", "？", "!", "?", "~", "…",
        )) else "，"
        return head + joiner + rest.lstrip("，,、；; ")

    def _final_touches(self, text: str) -> str:
        text = re.sub(r"\s*[—–]{1,2}\s*", "，", text)   # 破折号改逗号
        # 聊天里不需要句号：句中的换成逗号，句末的随逗号一起裁掉
        text = re.sub(r"。+", "，", text)
        text = re.sub(r"[，,]{2,}", "，", text)
        # 句末终止标点后不该再跟逗号/顿号；两个不同终止标点也只留第一个
        text = re.sub(r"([。！？!?~…])[，,、；;]+", r"\1", text)
        text = re.sub(r"([。！？!?~…])[。！？!?~…]+", r"\1", text)
        text = re.sub(r"^[，、,]+", "", text.strip())
        # 裁掉句尾客服话术后留下的半个逗号（「大肥鱼在群里呢，」）
        text = re.sub(r"[，,、；;]+$", "", text)
        return text.strip()

    def _oral_noise(self, text: str) -> str:
        prob = float(self.cfg.get("oral_noise_prob", 0.15))
        if random.random() >= prob:
            return text
        sents = [s for s in re.split(r"(?<=[。！？!?~…\n])", text) if s.strip()]
        if not sents:
            return text
        # 只在整条回复开头加。原来随机挑句插入，会变成
        # 「半夜别爬起来偷看后台。啊这关灯，睡了」——句号后硬塞语气词
        idx = 0
        body = sents[idx].lstrip()
        if len(body) < 5 or AI_TONE_HARD_RE.search(body):
            return text
        if body[0] in "嗯欸诶啊就那":
            # 「欸」和「诶」是两个码点，都得挡，防止「就欸，…」这种叠语气词
            return text
        particle = random.choice(ORAL_HEAD)
        sep = "，" if len(particle) >= 2 and random.random() < 0.7 else ""
        sents[idx] = particle + sep + sents[idx]
        return "".join(sents)

    # ---------- 颜文字层 ----------
    def _handle_kaomoji(self, text: str) -> str:
        """真人不在文字里画脸：库里有的颜文字转 :黄脸名: 占位符（sticker_pack 会渲染成真表情），
        库里没有的、以及括号小动作一律剥掉。"""
        mode = self.cfg.get("kaomoji_mode", "convert")

        def kao_repl(m: re.Match) -> str:
            kao = m.group(0).strip("()（）")
            if mode == "convert":
                name = KAOMOJI_TO_FACE.get(kao) or KAOMOJI_TO_FACE.get(m.group(0))
                if name:
                    return f":{name}:"
            return " "

        text = KAO_PAREN_RE.sub(kao_repl, text)
        text = KAO_TOKEN_RE.sub(kao_repl, text)
        # 剩下的括号旁白（含中文那种）直接丢：括号里塞解释/动作是最典型的 AI 味
        text = PAREN_NOTE_RE.sub(" ", text)
        # 剥完括号会留下「报错 。」这种空格，收掉
        text = re.sub(r"\s+([。，！？、；：])", r"\1", text)
        return re.sub(r"  +", " ", text)

    # ---------- 主入口 ----------
    def humanize(self, text: str) -> tuple[str, dict]:
        stats = {"md": False, "ai": 0}
        original = text
        text = _strip_raw_tool_json(text)  # 先切工具调用残留，别让后面的清洗规则把它拆得更碎
        # 内容型输出（讲故事 / 写东西 / 详细解释）不能被「两句封顶」砍掉。
        # 超过阈值就只做安全的表面清洗（去 markdown、去 AI 腔），
        # 跳过句数封顶、列表合并、拆长句、口语噪声——那些是给闲聊用的。
        try:
            threshold = int(self.cfg.get("long_bypass_chars", 150))
        except (TypeError, ValueError):
            threshold = 150
        long_mode = threshold > 0 and len(text) >= threshold
        text = self._handle_kaomoji(text)             # 符号脸先处理，防止后续层把它切碎
        text = self._drop_unknown_placeholders(text)  # 库外表情名直接抹掉
        protected, vault = self._protect(text)
        if self.cfg.get("strip_emoji", True):
            protected = self._strip_emoji(protected)
        if self.cfg.get("strip_markdown", True):
            protected, md_changed = self._strip_markdown(protected)
            stats["md"] = md_changed
        if self.cfg.get("strip_ai_tone", True):
            protected, dropped = self._drop_ai_sentences(protected, keep_breaks=long_mode)
            stats["ai"] = dropped
        if not long_mode:
            protected = self._merge_list_lines(protected)
            protected = self._split_long_sentences(protected)
            protected = self._kill_parallel(protected)
            protected = self._cap_sentences(
                protected, int(self.cfg.get("max_sentences", MAX_SENTENCES))
            )
            protected = self._final_touches(protected)
            protected = self._oral_noise(protected)
        restored = self._restore(protected, vault)
        restored = re.sub(r"\n{3,}", "\n\n", restored).strip()
        restored = re.sub(r"。$", "", restored).strip()  # 内容型长文也别拿句号收尾
        if not restored.strip("，。！？!?、:：;；~… \n"):
            restored = "……"
        return restored, {**stats, "changed": restored != original, "long": long_mode}


_default_cleaner = Cleaner()


def humanize(text: str, cfg: dict | None = None) -> str:
    """一句话接口：返回洗净的文本（其他插件可直接调用）。"""
    cleaner = Cleaner(cfg) if cfg else _default_cleaner
    out, _ = cleaner.humanize(text)
    return out
