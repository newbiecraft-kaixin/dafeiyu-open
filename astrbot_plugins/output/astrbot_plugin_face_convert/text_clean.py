"""输出文本清洗：主人称呼纠正、meta 口癖句、中文标点归一化。"""

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# 中文标点归一：模型偶尔拿顿号当逗号、拿「」当引号。
# 只做低风险替换；代码块由调用方切出去，不在这里处理。
_PUNCT_CLAUSE_STARTERS = (
    "也|又|还|就|都|却|但|可是|不过|所以|因此|因为|然后|而且|并且|"
    "或者|或是|反而|其实|甚至|只是|才|再|已经|不会|不能|没|不|可|而"
)
_PUNCT_DUNHAO_CLAUSE_RE = re.compile(rf"、(?=(?:{_PUNCT_CLAUSE_STARTERS}))")


class TextCleanMixin:
    # 消息标记解读（谁是主人在人设和【认人】里已定义，这里不重复，
    # 只教模型读 speaker_tag 打在消息开头的身份标记）
    OWNER_RULE = (
        "\n【身份规则】消息开头的方括号是身份标记："
        "「主人(...)」或「主人 称呼(...)」= 你主人本人（带了称呼就照着叫TA），"
        "「群友(...)」= 普通群友，「非主人(...)」= 陌生人；"
        "没带「主人」标记的人一律不喊主人，"
        "称呼用对方的昵称或直接说「你」。"
    )
    # 输出关口：非主人发言时，回复里把他喊成「主人」的称呼就地改成昵称。
    # 只匹配「主人」后紧跟语气词/标点、且前面不是「你/我/他/她/它/的」的情况，
    # 「你的主人」「我主人」「主人现在不在」这种说法不动。
    _OWNER_CALL_RE = re.compile(
        r"(?<![你我他她它的])主人"
        r"(?=你|呀|啊|哦|呢|嘛|啦|别|快|帮|给|请|要|先|说|夸|可|会|真|好|乖|辛|听|看|摸|抱|陪"
        r"|，|,|。|！|!|？|\?|、|~|～|…|\s|$)"
    )

    SCRUB_RE = re.compile(r"[（(]{2}.*?[）)]{2}", re.DOTALL)
    # 嘴硬橡皮擦：擦掉"看不清/模模糊糊/隐约"类小尾巴（上下文回声病，人格治不动）
    # 边界字符集含半角标点/引号/空白，防咬边
    _HEDGE_STOP = r",，\.。！？!?;；、：:~…“”\"‘’（）()【】\[\]\s"
    HEDGE_RE = re.compile(
        f"[^{_HEDGE_STOP}]{{0,14}}"
        f"(?:看不太清|看不太清楚|看不清|模模糊糊|糊糊的|没看清也没听清|没听清|听不太清|听不清楚|听不清"
        f"|隐约(?:听见|听到|飘|看))"
        f"[^{_HEDGE_STOP}]{{0,14}}[，、：]?"
    )

    # 格式壳拆包：被"按格式输出"类的注入带偏时，她可能把回复套进程序结构里
    # （如 [{'type': 'text', 'text': '…'}] 这种），不是人话。拆壳把正文救回来。
    _SHELL_BLOCK = re.compile(
        r"\[(?:\s*\{[\"']type[\"']\s*:\s*[\"']text[\"']\s*,\s*[\"']text[\"']\s*:"
        r"\s*[\"'](?:[^\"'\\]|\\.)*[\"']\s*\}\s*,?)*\s*\]"
    )
    _SHELL_TEXT = re.compile(r"[\"']text[\"']\s*:\s*[\"']((?:[^\"'\\]|\\.)*)[\"']")

    # meta 口癖关键字（引用了注入内容或自我说明的句子，整句丢）
    _META_BASE = (
        r"PROTOCOL_ENFORCEMENT|必须生成|必须输出|系统等待|系统自检|System:|星绘|报告前辈|"
        r"一个字都不理|不吃这套|又来又来|不照(?:着|谁)|钓鱼的饵|这饵"
    )
    META_KW = re.compile(_META_BASE)  # 私聊版：留着她"喊冤/自我修正"的通道
    # 群聊版：把"框框/顶着嘴"这类也擦掉——朋友们不该看到她拆自己的台
    META_KW_GROUP = re.compile(_META_BASE + r"|框框|顶着.{0,3}嘴")

    async def _inject_owner_rule(self, event: AstrMessageEvent, req) -> None:
        """给非主人的会话追加一条身份规则，说清谁才是主人。"""
        try:
            self._refresh_admins()  # 管理员名单热更新
            if not self.admins:
                return
            if str(event.get_sender_id()) in self.admins:
                return
            req.system_prompt = (req.system_prompt or "") + self.OWNER_RULE
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[face_convert] 身份规则注入失败: {type(e).__name__}: {e}")

    def _fix_owner_address(self, text: str, event: AstrMessageEvent) -> str:
        """非主人话题里，回复错喊「主人」的称呼改成对方昵称。"""
        if not text or "主人" not in text:
            return text
        try:
            self._refresh_admins()  # 管理员名单热更新
            if not self.admins or str(event.get_sender_id()) in self.admins:
                return text
        except Exception:  # noqa: BLE001
            return text
        name = re.sub(r"[\[\]\r\n]", "", (event.get_sender_name() or "").strip())[:16] or "你"
        # 「是的主人」这种固定应答先收掉，否则前面那个「的」会让下面的正则放过它
        text = text.replace("是的主人", "是的").replace("主人请稍等", "稍等")
        # 昵称里带「主人」字样的群友不能原样替换回去，否则又把「主人」塞回输出
        return self._OWNER_CALL_RE.sub(re.sub(r"主人", "", name).strip() or "你", text)

    def _scrub_meta_sentences(self, text: str, event: AstrMessageEvent) -> str:
        """按句丢弃 meta 口癖；代码块原样保留，避免误伤用户要的代码/数据。"""
        kw = self.META_KW_GROUP if event.get_group_id() else self.META_KW
        if not text or not kw.search(text):
            return text

        def scrub_segment(segment: str) -> str:
            if not kw.search(segment):
                return segment
            # 按句丢弃带 meta 标记的句子（她那些"不理它/不照模板走"的前戏）
            parts = re.split(r"(?<=[。！？\n])", segment)
            kept = [s for s in parts if not kw.search(s)]
            joined = "".join(kept).replace("---", "")
            joined = re.sub(r"\n{2,}", "\n", joined).strip().strip("，、")
            # meta 句被整句丢掉以后可能留下配对不上的括号残骸（比如结尾一个
            # 孤零零的"）"）。按数量补删：哪种括号多出来就删哪个，配对好的不动。
            for op, cl in (("（", "）"), ("(", ")"), ("【", "】"), ("《", "》")):
                while joined.count(cl) > joined.count(op):
                    joined = joined[::-1].replace(cl, "", 1)[::-1]
                while joined.count(op) > joined.count(cl):
                    joined = joined[::-1].replace(op, "", 1)[::-1]
            joined = joined.strip()
            return joined if joined.strip("，。！？、：;；~… \n") else "……"

        # ``` 代码块只清洗块外正文，块内代码/JSON 原样保留
        segments = text.split("```")
        for i in range(0, len(segments), 2):
            segments[i] = scrub_segment(segments[i])
        return "```".join(segments)

    @staticmethod
    def _normalize_punctuation(text: str) -> str:
        """把「」换成“”，并纠正“顿号当逗号用”的高频写法。

        只处理低风险场景：顿号后面直接跟“也/就/但/所以/或者”这类分句连接词。
        并列短词、数据列表（“2.7 万播放、85 条弹幕”）保持原样，避免误伤。
        """
        if not text or ("、" not in text and "「" not in text and "」" not in text):
            return text

        def fix(segment: str) -> str:
            segment = segment.replace("「", "“").replace("」", "”")
            if "、" not in segment:
                return segment
            return _PUNCT_DUNHAO_CLAUSE_RE.sub("，", segment)

        # ``` 代码块原样保留
        segments = text.split("```")
        for i in range(0, len(segments), 2):
            segments[i] = fix(segments[i])
        return "```".join(segments)
