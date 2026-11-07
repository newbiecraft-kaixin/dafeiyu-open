"""全库文字相似度排序器与查询扩展（v1.4：jieba 分词 + 分层权重）。

为什么要换掉 v1.3 的写法：
  v1.3 是「原句 + 一整簇近义词」平权比字面——写「我服了」时，无语簇的
  「傻/蠢/笨」也拿到 1.0 分，于是会推一张「傻子」的图；写「不开心」时
  「开心」簇先命中，配出来的全是呲牙。语义不匹配就是这么来的。
  这一版做三件事：
    1) 用结巴分词，中文按「词」比，不再拿单字在整句里乱撞；
    2) 分层给分：原句/词命中最高，同簇标准词次之，近义说法再降一档，
       近义词永远进不了「核心命中」那一层，只能做长尾补充；
    3) 否定式（不开心/不好笑/没意思）单独成簇，不再被肯定式抢先命中。
  jieba 不可用时退回二元组分词，行为与旧版接近。
"""

from __future__ import annotations

import logging
import re

try:  # 结巴不是硬依赖：装了就用，没装退回二元组
    import jieba

    jieba.setLogLevel(logging.WARNING)
except Exception:  # noqa: BLE001
    jieba = None

STOP_CHARS = set("的了吗呢吧啊呀哦嗯是我你他她它这那有就都也不很太个把被给对说会能可以还来着过里上下在")

PUNCT_RE = re.compile(r"[\s:：,，、。.．!！?？~～…·\-—_（）()\[\]【】\"'“”‘’/\\|]+")

EMOTION_CHARS_ALL = "笑乐喜爽嗨哈强牛棒炫讽嘲鄙嫌杂酸典怒气凶骂滚火揍狠哭泪惨怜呜委屈累躺摆瘫懒寄苟瓜奇围剧看啥惊震呆吓懵瞪饿饭餐馋吃羞红脸腼抱亲贴萌娇默裂尴无语疲困乏熬睡觉床晚干忙修码班工收办完搞领"


def _norm_text(s: str) -> str:
    """去标点空白、转小写：只留字面，剩下的交给相似度。"""
    return PUNCT_RE.sub("", (s or "").lower())


def segment_all(text: str) -> list[str]:
    """分词但不过滤单字——配脸要认「累」「哭」这种单字关键词。"""
    t = _norm_text(text)
    if not t:
        return []
    toks: list[str] = []
    if jieba is not None:
        for w in jieba.lcut(t):
            w = w.strip()
            if w:
                toks.append(w)
    if not toks:
        toks = [t[i : i + 2] for i in range(max(0, len(t) - 1))] if len(t) >= 2 else [t]
    seen: set[str] = set()
    out: list[str] = []
    for w in toks:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def segment(text: str) -> list[str]:
    """中文分词：优先结巴；没装就退回二元组。

    只保留「词」（≥2 字的中文词、整段英文/数字）。单字不放进来——「一切」
    里的「切」、「积累」里的「累」就是这么把无关表情命中出来的。
    """
    return [
        w for w in segment_all(text)
        if re.fullmatch(r"[a-z0-9]+", w) or len(w) >= 2
    ]


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

# 否定式：连同否定的原文一起映射到真正的情绪簇，避免「不开心」被「开心」抢走。
NEG_CLUSTER: dict[str, str] = {
    "不开心": "委屈", "不高兴": "委屈", "不快乐": "委屈", "不好笑": "无语",
    "没意思": "无语", "不想动": "累了", "睡不着": "想睡", "不想睡": "想睡",
    "不饿": "无语", "没劲": "累了", "笑不出来": "委屈",
}

NEG_CHARS = set("不没别勿无")

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


def _has_unneg(text: str, word: str) -> bool:
    """词在句子里出现过，且至少有一次没被「不/没/别」否定掉。"""
    i = text.find(word)
    while i >= 0:
        if i == 0 or text[i - 1] not in NEG_CHARS:
            return True
        i = text.find(word, i + 1)
    return False


def hit_clusters(query: str) -> set[str]:
    """这句话明确说出的情绪簇（先处理否定式）。"""
    q = query or ""
    out: set[str] = set()
    for key, cluster in NEG_CLUSTER.items():
        if key in q:
            out.add(cluster)
    for canonical, variants in EMOTION_LEXICON.items():
        if canonical in out:
            continue
        if _has_unneg(q, canonical) or any(_has_unneg(q, v) for v in variants):
            out.add(canonical)
    return out


def _expand(query: str) -> list[tuple[str, float]]:
    """查询扩展：(词, 分数上限)。

    同簇标准词封顶 0.85（刚好够进核心层），近义说法封顶 0.62（只做长尾）。
    这样「我服了」能推到无语图，但不会顺手把整簇的「傻/蠢」也顶到满分。
    """
    q = query or ""
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for canonical in hit_clusters(q):
        if canonical not in seen:
            seen.add(canonical)
            out.append((canonical, 0.85))
        for v in EMOTION_LEXICON.get(canonical, []):
            if v in seen:
                continue
            seen.add(v)
            out.append((v, 0.62))
    return out


def _cluster_chars(query: str) -> set[str]:
    """查询命中的情绪簇 -> 该簇的「情绪字」集合（扫尾层用）。"""
    out: set[str] = set()
    for canonical in hit_clusters(query):
        out.update(EMOTION_CHARS.get(canonical, ""))
    return out


class StickerRanker:
    """全库文字相似度排序器：只吃文本，不碰图片。

    分层给分（取最高，不叠加）：
        · 名字就是查询词                    -> 1.3
        · 名字/查询互相包含                 -> 1.05
        · 查询整串出现在「含义 + 名字」里    -> 1.0
        · 分词后词命中比例                  -> 0.55 ~ 1.0
        · 同情绪簇的标准词                  -> ≤0.85
        · 同情绪簇的近义说法                -> ≤0.62
        · 只命中情绪字                      -> ≤0.46
    近义层够不着「核心命中」线（0.85），所以「服了」不会先推一张「傻子」，
    只会把无语图排进去；确实一句都不沾时才由情绪字扫尾补长尾。
    """

    def __init__(self, docs: dict[str, str]):
        self.names: list[str] = [n for n, d in docs.items() if d]
        self._dtok: dict[str, set[str]] = {}
        self._draw: dict[str, str] = {}
        self._dname: dict[str, str] = {}
        for name in self.names:
            self._dtok[name] = set(segment(docs[name]))
            self._draw[name] = _norm_text(docs[name])
            self._dname[name] = _norm_text(name)

    @staticmethod
    def _quality(term: str, dtok: set[str], draw: str) -> float:
        """一个词对一张图（含义文本）的匹配质量，0~1。"""
        t = _norm_text(term)
        if not t:
            return 0.0
        if t.isascii():
            # 英文按整词比：不然 "ok" 会命中 "token"
            return 1.0 if t in dtok else 0.0
        if len(t) >= 2:
            if t in draw:
                # 「不开心」里也有「开心」，但那是被否定的开心，不能算命中
                return 1.0 if _has_unneg(draw, t) else 0.0
            bgs = [t[i : i + 2] for i in range(len(t) - 1)]
            hit = sum(1 for b in bgs if b in draw)
            if not bgs or not hit:
                return 0.0
            ratio = hit / len(bgs)
            return 0.9 * ratio if ratio >= 0.5 else 0.4 * ratio
        return 0.55 if t in draw else 0.0

    def rank(self, query: str) -> list[tuple[str, float]]:
        q = (query or "").strip()
        if not q or not self.names:
            return []
        qn = _norm_text(q)
        # 被否定的词不算命中：「不开心」不能因为切出个「开心」就去推呲牙图
        primary = [t for t in segment(q) if _has_unneg(qn, t)]
        clusters = _expand(q)
        chars = _cluster_chars(q)
        scored: list[tuple[str, float]] = []
        for name in self.names:
            dt = self._dtok[name]
            dr = self._draw[name]
            dn = self._dname[name]
            s = 0.0
            if qn == dn:
                s = 1.3
            elif len(qn) >= 2 and qn in dn:
                s = 1.05
            elif len(dn) >= 2 and dn in qn and _has_unneg(qn, dn):
                # 名字包含在查询里也要管否定：「不开心」不能因为含「开心」就配开心图
                s = 1.05
            if len(qn) >= 2 and qn in dr and _has_unneg(dr, qn):
                s = max(s, 1.0)
            elif primary:
                hit = sum(1 for t in primary if t in dt)
                if hit:
                    cov = hit / len(primary)
                    s = max(s, 0.55 + 0.45 * cov)
            if s < 0.85:
                best = 0.0
                for term, cap in clusters:
                    qs = self._quality(term, dt, dr) * cap
                    if qs > best:
                        best = qs
                if best > s:
                    s = best
            if s < 0.5 and chars:
                hit = sum(1 for c in chars if c in dr)
                if hit:
                    s = max(s, 0.26 + min(0.2, 0.07 * hit))
            if s > 0:
                scored.append((name, round(s, 4)))
        scored.sort(key=lambda x: -x[1])
        return scored
