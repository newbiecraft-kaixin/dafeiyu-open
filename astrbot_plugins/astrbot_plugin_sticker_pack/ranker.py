"""全库文字相似度排序器与查询扩展。"""

from __future__ import annotations

import re

STOP_CHARS = set("的了吗呢吧啊呀哦嗯是我你他她它这那有就都也不很太个把被给对说会能可以还来着过里上下在")

PUNCT_RE = re.compile(r"[\s:：,，、。.．!！?？~～…·\-—_（）()\[\]【】\"'“”‘’/\\|]+")


def _norm_text(s: str) -> str:
    """去标点空白、转小写：只留字面，剩下的交给相似度。"""
    return PUNCT_RE.sub("", (s or "").lower())


def _tokens(text: str) -> list[str]:
    """中文按「二元组 + 单字」切；英文按整词切。

    英文一定不能切二元组：否则 "token" 会切出 "ok"，一张算 token 的图
    就成了「收到」的候选（这坑实测踩过）。
    """
    t = _norm_text(text)
    toks: list[str] = [t[i : i + 2] for i in range(len(t) - 1)
                       if "\u4e00" <= t[i] <= "\u9fff" and "\u4e00" <= t[i + 1] <= "\u9fff"]
    toks.extend(c for c in t if "\u4e00" <= c <= "\u9fff" and c not in STOP_CHARS)
    toks.extend(re.findall(r"[a-z0-9]+", t))
    return toks


# 情绪词 -> 近义说法。**只用于查询扩展**，不是分类：AI 写「开心」，库里写的是
# 「笑出声」「吃饱饱」「满足」也该能对上。任何图都不会被排除在这张表之外。
EMOTION_LEXICON: dict[str, list[str]] = {
    "开心": ["开心", "高兴", "快乐", "笑", "乐", "哈哈", "好耶", "愉快", "满足", "舒服", "爽", "庆祝",
             "恭喜", "上岸", "中奖", "发财", "好运", "走运", "喜提"],
    "得意": ["得意", "骄傲", "牛", "厉害", "最强", "炫耀", "赢了", "升咖", "棒", "优秀", "闪亮"],
    "阴阳": ["阴阳", "嘴欠", "嘲讽", "反讽", "瞧不起", "看不起", "爹味", "杂鱼", "酸", "就这", "典", "嘴硬"],
    "生气": ["生气", "愤怒", "火大", "凶", "骂", "滚", "揍", "黑化", "发火", "烦", "气死", "警告"],
    "委屈": ["委屈", "难过", "可怜", "哭", "泪", "惨", "呜呜", "求", "安慰", "心疼"],
    "摆烂": ["摆烂", "躺平", "摸鱼", "放弃", "瘫", "不干了", "寄", "咸鱼", "偷懒", "摆"],
    "吃瓜": ["吃瓜", "围观", "看戏", "八卦", "热闹", "前排", "看片", "剧情", "凑热闹", "搬板凳", "看热闹",
             "瓜", "后续", "蹲一个"],
    "震惊": ["震惊", "惊讶", "惊", "卧槽", "离谱", "瞪眼", "吓", "懵", "呆住", "什么鬼", "真的假的"],
    "饿了": ["饿", "吃", "吃饭", "干饭", "想吃", "肚子", "宵夜", "美食", "吃饱"],
    "害羞": ["害羞", "脸红", "羞", "不好意思", "腼腆", "亲亲", "心动", "脸红红"],
    "撒娇": ["撒娇", "抱抱", "贴贴", "粘人", "可爱", "亲", "摸摸", "求抱", "贴"],
    "无语": ["无语", "沉默", "服了", "白眼", "翻白眼", "算了", "裂开", "尴尬", "不知道说什么", "emm",
             "呵呵", "随便你", "行吧", "无语了", "傻", "蠢", "笨", "离谱"],
    "累了": ["累", "疲惫", "没劲", "休息", "熬夜", "撑不住", "打不动", "不想动", "困"],
    "想睡": ["睡觉", "晚安", "困", "躺床", "赖床", "打盹", "睡", "晚安啦"],
    "干活": ["干活", "工作", "加班", "忙", "修", "测", "bug", "上线", "部署", "写代码", "跑任务", "打工", "搬砖", "上班",
             "老板", "领导", "开会", "赶工"],
    "收到": ["收到", "遵命", "办完", "完成", "搞定", "没问题", "ok"],
    "感谢": ["谢谢", "感谢", "多谢", "谢", "领情", "红包", "爱你"],
    "疑惑": ["疑惑", "疑问", "为什么", "咋", "问号", "不懂", "嗯？", "怎么回事"],
    "害怕": ["害怕", "怕", "恐怖", "吓人", "怂", "发抖", "救命"],
    "期待": ["期待", "等", "盼望", "快点", "来吧", "想看"],
    "失望": ["失望", "落差", "白等", "唉", "没戏", "算了吧"],
    "感动": ["感动", "暖", "泪目", "走心"],
    "加油": ["加油", "鼓励", "冲", "支持", "打气", "干巴爹"],
    "告别": ["再见", "拜拜", "走了", "下线", "下次", "晚安"],
    "打招呼": ["打招呼", "你好", "在吗", "来了", "hello", "嗨", "早安"],
}


# 情绪字：整词都对不上时的「扫尾层」。中文里情绪常常藏在一个字里——
# 含义写「眼泪汪汪」不一定出现「委屈」，但「泪」出现了。这一层让池子有尾巴，
# 不至于同一个词永远只有四五张图轮着用；分数排在上面的整词命中之后，
# 只有核心那几张都发过了，才轮到它们出场。
EMOTION_CHARS: dict[str, str] = {
    "开心": "笑乐喜爽嗨哈",
    "得意": "强牛棒炫耀",
    "阴阳": "讽嘲鄙嫌杂酸典",
    "生气": "怒气凶骂滚火揍狠",
    "委屈": "哭泪惨怜呜委屈",
    "摆烂": "累躺摆瘫懒寄苟",
    "吃瓜": "瓜奇围剧看啥",
    "震惊": "惊震呆吓懵瞪",
    "饿了": "饿饭餐馋吃",
    "害羞": "羞红脸腼",
    "撒娇": "抱亲贴萌娇",
    "无语": "默裂尴无语",
    "累了": "累疲困乏熬",
    "想睡": "睡困觉床晚",
    "干活": "干忙修码班工",
    "收到": "收办完搞领",
}


def _cluster_chars(query: str) -> set[str]:
    """查询命中的情绪簇 -> 该簇的「情绪字」集合（扫尾层用）。"""
    q = query or ""
    out: set[str] = set()
    for canonical, chars in EMOTION_CHARS.items():
        if canonical in q or any(v in q for v in EMOTION_LEXICON.get(canonical, [])):
            out.update(chars)
    return out


def _expand(query: str) -> list[str]:
    """查询扩展：AI 说的词命中哪个情绪簇，就把那一簇的近义说法都拿去比字面。"""
    q = query or ""
    out: list[str] = []
    for canonical, variants in EMOTION_LEXICON.items():
        if canonical in q or any(v in q for v in variants):
            out.append(canonical)
            out.extend(variants)
    return out


class StickerRanker:
    """全库文字相似度排序器：只吃文本，不碰图片。

    打法是「最优命中」而不是余弦：对每个候选图，取「原句 + 情绪近义词」里
    对它匹配得最好的那一个词作为分数——
        · 词整串出现在「含义 + 名字」里      -> 1.0（「笑」命中「心情很好的温柔笑」）
        · 单个字的情绪字命中                -> 0.55（「累」命中「累成咸鱼」）
        · 只有二元组部分重合                -> 按重合比例打折
        · 名字就是查询词                    -> 1.3（AI 或用户点名了）
    不稀释、不衰减，所以「开心」不会因为名字里带「开心」的那五张就把
    「笑出声」「吃饱饱」这些语义近的图挤出去——池子自然就宽了。
    """

    def __init__(self, docs: dict[str, str]):
        self.names: list[str] = [n for n, d in docs.items() if d]
        self._dtok: dict[str, set[str]] = {}
        self._draw: dict[str, str] = {}
        self._dname: dict[str, str] = {}
        for name in self.names:
            self._dtok[name] = set(_tokens(docs[name]))
            self._draw[name] = _norm_text(docs[name])
            self._dname[name] = _norm_text(name)

    @staticmethod
    def _quality(term: str, dtok: set[str], draw: str) -> float:
        """一个词对一张图（含义文本）的匹配质量，0~1。"""
        t = _norm_text(term)
        if not t:
            return 0.0
        if t.isascii():
            # 英文按整词比：不然 "ok" 会命中 "token"，一张算 token 的图就成了「收到」
            return 1.0 if t in dtok else 0.0
        if len(t) >= 2:
            if t in draw:
                return 1.0
            bgs = [t[i : i + 2] for i in range(len(t) - 1)]
            hit = sum(1 for b in bgs if b in draw)
            if not bgs or not hit:
                return 0.0
            ratio = hit / len(bgs)
            # 过半二元组命中算「大概是这个词」，否则只算擦边
            return 0.9 * ratio if ratio >= 0.5 else 0.4 * ratio
        return 0.55 if t in draw else 0.0

    def rank(self, query: str) -> list[tuple[str, float]]:
        q = (query or "").strip()
        if not q or not self.names:
            return []
        terms = [q] + _expand(q)
        chars = _cluster_chars(q)
        qn = _norm_text(q)
        scored: list[tuple[str, float]] = []
        for name in self.names:
            dt = self._dtok[name]
            dr = self._draw[name]
            s = 0.0
            # 取「最优命中」，不能覆盖：近义词表里排在后面的词对这张图往往一个字
            # 都不沾（「生气啦」被「为什么」「你猜」这些扩展词一路覆盖成 0），
            # 覆盖式写法会让整词命中的图凭空消失，候选池就只剩擦边的那些。
            for term in terms:
                qs = self._quality(term, dt, dr)
                if qs > s:
                    s = qs
                if s >= 1.0:
                    break
            # 整词都没命中，退到「情绪字」层：命中的字越多分越高，但永远排在整词命中之后
            if s < 0.5 and chars:
                hit = sum(1 for c in chars if c in dr)
                if hit:
                    s = max(s, 0.26 + min(0.2, 0.07 * hit))
            dn = self._dname[name]
            if qn and qn == dn:
                s = max(s, 1.3)
            elif len(qn) >= 2 and qn in dn:
                s = max(s, 1.05)
            elif len(dn) >= 2 and dn in qn:
                s = max(s, 1.05)
            if s > 0:
                scored.append((name, round(s, 4)))
        scored.sort(key=lambda x: -x[1])
        return scored
