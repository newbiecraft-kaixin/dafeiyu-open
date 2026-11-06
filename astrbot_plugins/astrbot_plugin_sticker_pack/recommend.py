"""文字相似度候选池、语义组折叠与兜底候选。"""

from __future__ import annotations

import random

from .ranker import EMOTION_LEXICON, StickerRanker
from .settings import NEG_MOOD_RE

# ==================== 推荐层：纯文字相似度（v1.3.0 重写） ====================
# 为什么要改成这样：
#   池子只活在插件里，AI 看不到任何一个图名（inject_index=False），它只会写
#   :情绪词: 或 :感受短语:。所以「推荐」这件事只能是【文字匹配】——拿 AI 写的字，
#   去跟全库每一条的「名字 + 含义」比字面相似度。
#   旧做法是先把图硬分进 8 个情绪桶，再按词查桶：桶是手写的、封闭的，
#   图被分错/漏分就永远出不来，收藏的新图更是几乎进不了桶 —— 这才是一直那几个。
# 两条硬规矩：
#   1. 推荐阶段绝不碰图片指纹（dHash）。长相相近的图互相压制，池子只会越用越窄。
#      指纹只在**收藏入库查重**时用（见 _find_duplicate）。
#   2. 主库 stickers/ 与收藏库 stickers/fav/ 物理分开，但推荐时一起参与排名，
#      新收的图立刻可用；库越大、含义越杂，抽出来越活。


# 语义分组：**同一组只出一条候选**。
#
# 库里 97 张动图，一半是同一个意思的变体：开心/开心3/开心4/好开心/太幸福了、
# 晚安/困了/赖床/睡大觉/deepsleep。旧逻辑给 AI 5 个候选，它会拿到「开心、开心3、
# 好开心、太幸福了、吃棒棒糖」——五张一个意思，等于只给了一个选项，
# 还占掉了本该给「累了」「委屈」的位置。这里按组折叠：一组只留一张代表，
# 组内轮换（今天「好开心」明天「开心3」），组间互补。
#
# 顺序即优先级：含义里带「阴阳」「嘲讽」的图往往顺带写「你好」「睡」，
# 排在后面会被别的组先吃掉（你好区、怎么睡得着、你已急哭 都是这路货）。
# 匹配先看「含义」、再看「名字」——「吃棒棒糖」的含义是「开心时发」，
# 先看名字会因为一个「吃」字被塞进吃饭组。
STICKER_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("阴阳", ("阴阳", "嘲讽", "嘴硬", "便宜货", "模型", "大烧货", "癖好", "豆包", "区神", "急哭")),
    ("讨钱", ("讨要", "催债", "零花钱", "打赏", "索要", "要奖励")),
    ("吃瓜", ("吃瓜", "看戏", "围观", "八卦", "观察")),
    ("睡觉", ("睡觉", "晚安", "睡", "困", "赖床", "被窝", "打哈欠", "熬夜", "失眠")),
    ("安慰", ("安慰", "陪", "抱抱", "抱", "摸摸", "不哭", "别哭", "别难过")),
    ("累了", ("累", "疲", "瘫", "咸鱼", "躺平", "不想动", "喘不过气")),
    ("遭罪", ("被光", "强光", "被火烧", "被电", "烤熟", "嗷嗷叫", "晕头转向")),
    ("饿了", ("饿", "吃", "干饭", "好吃", "馋", "宵夜", "美食", "甜甜圈", "流口水", "很满足")),
    ("慌了", ("慌", "彻底怒", "救命")),
    ("生气", ("生气", "火大", "发火", "凶", "骂", "揍", "喷火")),
    ("无语", ("无语", "沉默", "服了", "尴尬", "某人很傻")),
    ("震惊", ("震惊", "惊叹", "惊呼", "卧槽", "哇", "呆住", "懵", "离谱")),
    ("疑惑", ("疑惑", "疑问", "询问")),
    ("拒绝", ("拒绝", "不要给我", "蹭", "护食")),
    ("委屈", ("委屈", "哭", "泪", "惨", "可怜", "求你们", "嘲笑", "被嫌弃")),
    ("坏笑", ("坏笑", "不怀好意", "憋着坏", "嘿嘿")),
    ("打招呼", ("打招呼", "你好", "来了", "ciallo", "挥手", "喊你", "报名", "举手", "登场", "报到")),
    ("卖萌", ("卖萌", "可爱", "撒娇", "呆萌", "眨眼", "害羞", "脸红")),
    ("摆烂", ("摆烂", "摸鱼", "放弃", "偷懒", "游戏")),
    ("干活", ("干活", "工作", "打工", "加班", "卖力", "屏幕")),
    ("开心", ("开心", "高兴", "快乐", "幸福", "笑", "哈", "好耶", "庆祝", "送花", "红包", "敲锣", "搞笑")),
    ("得意", ("得意", "骄傲", "厉害", "最强", "降价", "仰视", "大佬", "优秀")),
    ("调皮", ("调皮", "捣蛋", "歪脑筋")),
    ("画饼", ("画饼", "预览版")),
    ("等待", ("等对方", "安静站着")),
    ("乖巧", ("乖巧", "听话", "被夸")),
    ("赞同", ("赞同", "确认")),
    ("整活", ("整活",)),
    ("求照顾", ("包养", "照顾")),
)

# 名字优先：这组图的「含义」写得五花八门，但名字一看就知道是一家的。
# 「喝奶茶」配「想喝奶茶了」、「喝奶茶2」配「开心时发」——不按名字收，
# 同一套图会被拆进「饿了」和「开心」两组，候选里同时出现两张奶茶。
# 注意只能放「名字唯一对应一个意思」的：大肥鱼系列不能放，8 是遭罪、
# 11 是累了、13 是开心，名字一样意思全不同。
STICKER_GROUP_BY_NAME_FIRST: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("奶茶", ("奶茶",)),
)

# 名字层兜底：含义里一个字都不沾时才看名字（「专注看屏幕」这种名字即含义的图）
STICKER_GROUP_BY_NAME: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("干活", ("专注", "屏幕", "打工", "干活")),
    ("卖萌", ("呆萌", "可爱", "萌")),
    ("开心", ("开心", "好耶", "幸福")),
    ("睡觉", ("睡", "困", "晚安", "赖床")),
    ("累了", ("累", "咸鱼")),
)


# 语义组名 -> 情绪簇。组名和簇名大体同名（开心/生气/无语…），
# 不一样的在这里补映射，用于「这句话明确说了哪个情绪」时把对应组顶到最前。
GROUP_TO_CLUSTER: dict[str, tuple[str, ...]] = {
    "睡觉": ("想睡", "告别"),
    "安慰": ("委屈", "感动"),
    "卖萌": ("害羞", "撒娇"),
    "遭罪": ("害怕",),
    "慌了": ("害怕",),
    "讨钱": ("感谢",),
    "奶茶": ("饿了",),
    "摆烂": ("摆烂",),
    "累了": ("累了",),
    "饿了": ("饿了",),
    "开心": ("开心",),
    "生气": ("生气",),
    "无语": ("无语",),
    "震惊": ("震惊",),
    "疑惑": ("疑惑",),
    "委屈": ("委屈",),
    "得意": ("得意",),
    "吃瓜": ("吃瓜",),
    "干活": ("干活",),
    "阴阳": ("阴阳",),
    "打招呼": ("打招呼",),
    "感谢": ("感谢",),
}


def _hit_clusters(query: str) -> set[str]:
    """这句话明确说出的情绪簇（整词命中，不含扫尾层）。

    「你猜我为什么生气」里「生气」是整词：就算检索把一堆同分图排在前面，
    生气组也必须优先占候选位，不然会出现「说了生气却一张生气的图都没有」。
    """
    q = query or ""
    out: set[str] = set()
    for canonical, variants in EMOTION_LEXICON.items():
        if canonical in q or any(v in q for v in variants):
            out.add(canonical)
    return out


class RecommendMixin:
    def _build_ranker(self) -> None:
        """把主库 + 收藏库的「名字 + 含义」建成文字索引（图库变动时重建一次）。"""
        docs = {
            n: f"{v.get('meaning') or ''} {n}" for n, v in self.stickers.items()
        }
        self._ranker_obj = StickerRanker(docs)
        self._rank_cache.clear()
        self._fallback_cache.clear()
        self._group_cache.clear()

    def _ranker(self) -> StickerRanker:
        if self._ranker_obj is None:
            self._build_ranker()
        return self._ranker_obj  # type: ignore[return-value]

    def _rank_pool(
        self,
        query: str,
        umo: str = "",
        limit: int | None = None,
        kinds: tuple[str, ...] = ("image",),
    ) -> list[str]:
        """文字相似度候选池：两层拼出来，不是一个分类一张表。

        第一层是「整词命中」的全部候选（语义确定对得上），第二层用情绪字扫尾
        补到 topk——同一个情绪词因此能在一二十张里换着抽，而不是只有一张脸。
        收藏库的新图给一点点新鲜度加成，收进来就能见到，而不是躺在目录里。
        """
        scored = self._rank_cache.get(query)
        if scored is None:
            scored = self._ranker().rank(query)
            if len(self._rank_cache) > 256:
                self._rank_cache.clear()
            self._rank_cache[query] = scored
        if not scored:
            return []
        cnt = self._use_count.get(umo, {}) if umo else {}
        boost = float(self.config.get("fav_novelty_boost", 1.12))
        items: list[tuple[float, str]] = []
        for name, s in scored:
            item = self.stickers.get(name)
            if not item or item.get("kind") not in kinds:
                continue
            if item.get("source") == "fav" and not cnt.get(name):
                s *= boost
            if item.get("kind") == "super":
                # 超级表情和表情包同池竞争，但库大、字面分容易被普通图压住，
                # 给一点点加成，让「笑死/气死」这类明确情绪真能推到大表情
                s *= float(self.config.get("super_face_boost", 1.25))
            items.append((s, name))
        if not items:
            return []
        items.sort(key=lambda x: -x[0])
        topk = int(limit or self.config.get("text_match_topk", 24))
        core_cut = float(self.config.get("text_match_core_score", 0.85))
        floor = float(self.config.get("text_match_min_score", 0.15))
        # 第一层：整词命中（含名字命中）——语义确定对得上的，全部先收进来
        core = [n for s, n in items if s >= core_cut][:topk]
        if len(core) < topk:
            # 第二层：情绪字扫尾——把池子补到 topk，让长尾图也有出场机会
            have = set(core)
            core += [
                n for s, n in items
                if n not in have and core_cut > s >= floor
            ][: topk - len(core)]
        return core

    def _pick_fresh(self, pool: list[str], umo: str) -> str | None:
        """池内加权抽取：越贴切的（排越前）权重越高，但冷门图始终留着机会。

        池子是按文字相似度排好序传进来的：位置 0 权重 1，位置 20 只剩 0.12。
        再叠加「用得越少越容易被抽到」，两个力一夹，就是既要贴切又要不重样。
        """
        if not pool:
            return None
        used = self._recent_used(umo)
        cnt = self._use_count.get(umo, {})
        idx = [i for i, n in enumerate(pool) if n not in used] or list(range(len(pool)))
        weights = [
            1.0 / (1.0 + 0.35 * i) / (1.0 + cnt.get(pool[i], 0)) for i in idx
        ]
        return pool[random.choices(idx, weights=weights, k=1)[0]]

    def _pick_image(self, word: str, umo: str) -> str | None:
        """AI 写了一个情绪词/感受短语，就在全库里按文字相似度抽一张。

        注意：**不再**因为「库里正好有同名图」就直接返回那一张。
        「开心」「无语」「可爱」这些词本身就是图名，旧版短路导致这几个词
        永远只发同一张图——这才是「老用那几个」最狠的一刀。
        同名图现在只是分数最高（1.3）的候选之一，跟其他相关图一起轮换。
        """
        word = (word or "").strip()
        if not word:
            return None
        pool = self._rank_pool(
            word, umo, limit=max(int(self.config.get("text_match_topk", 24)), 8)
        )
        # 「没匹配到就别发」：砍掉旧版两条全库乱捞路径（narrow pool 探索 + 空池探索）。
        # 池子空 = 文字和全库对不上，宁可空手也不发一张驴唇不对马嘴的。
        return self._pick_fresh(pool, umo) if pool else None

    def _match_candidates(
        self,
        text: str,
        n: int,
        umo: str = "",
        allow_fallback: bool = True,
        min_score: float = 0.0,
    ) -> list[str]:
        """按整句话和全库「名字 + 含义」的文字相似度取候选，交给 AI 挑。

        只有文字匹配：这句话里的情绪/动作词命中哪些图的含义，就推哪些。
        候选池里的名字随机排序，所以同样的语境每次推的也不一样。
        最近发过的垫底（还有得选就不重复）；一个都排不出来 = 库里真缺这种情绪，
        交给 _note_deficit 记一笔，等群里出现新图时补。

        allow_fallback=False 时不做「情绪簇随机兜底」：给 send_meme 用。
        那条路是 AI 直接发第一张、没有二次筛选，随机兜底等于把「热死了」
        配成「干饭求打赏」——宁可一次不发，也别发一张完全不搭的。
        """
        if NEG_MOOD_RE.search(text or ""):
            return []
        # 池子先拉宽：下面要按语义组折叠，一组只出一条，备料不够会折叠成一两张。
        # 也不能太小：整词命中的图动辄二十多张同分，「生气啦」排到第 16 位就被
        # 截断的话，整句话里明明说了「生气」，候选里却一张生气的图都没有。
        want = max(n * 8, 48)
        sc = dict(self._ranker().rank(text))
        # 候选池 = 表情包 + 超级表情（同池竞争）。小黄脸仍然不进：
        # 它混进来 AI 挑中的概率不低，用户看到的就是小黄脸而不是表情包。
        pool = self._rank_pool(text, umo, limit=want, kinds=("image", "super"))
        # 优先放带描述的：没描述的图 AI 完全没法判断贴不贴切（「老吴」这种名字给不出信息）
        pool = [
            x for x in pool
            if self.stickers.get(x, {}).get("kind") in ("image", "super")
        ]
        # 硬门槛：send_meme 直接发第一张，没有 AI 二次筛选，
        # 「看到傻子言论」靠「看到」二字擦边命中「哇好圆」这种必须掐掉。
        if min_score > 0:
            pool = [x for x in pool if sc.get(x, 0.0) >= min_score]
        with_desc = [
            x for x in pool if (self.stickers.get(x, {}).get("meaning") or "").strip()
        ]
        if len(with_desc) >= 3:
            pool = with_desc
        # 备料按相关性补齐（顺序取，不随机）：折叠是按组去的，料不够会折叠成一两张
        if len(pool) < want:
            for nm, s in self._ranker().rank(text):
                if len(pool) >= want:
                    break
                if s < min_score:
                    break  # rank 已按分降序，后面只会更低
                d = self.stickers.get(nm, {})
                if nm in pool or d.get("kind") not in ("image", "super"):
                    continue
                if not (d.get("meaning") or "").strip():
                    continue
                pool.append(nm)
        # 同组折叠：一组只留一张代表（组内轮换），别再拿五张一样的图占满候选位
        picked, used_groups = self._collapse_by_group(
            pool, umo, n, sc, _hit_clusters(text)
        )
        # 最后一层兜底：上面全排不出来时，按「基础情绪簇」取代表图。
        # 「没匹配到就别发」：必须命中语义组关键词（_hit_clusters，词级）才允许兜底，
        # 旧版用 _cluster_chars（字符级）——「今天热死了」一个「热」字就能从
        # 热簇捞张图，字面对不上时看起来就是乱发。一个组关键词都没命中 = 空手。
        if not picked and allow_fallback and _hit_clusters(text):
            picked += self._fallback_candidates(
                umo, n - len(picked), exclude=set(picked),
                text=text, skip_groups=used_groups,
            )
        return picked[:n]

    def _group_of(self, name: str) -> str:
        """图 -> 语义组。规则表以外的一张自成一组（不和别人折叠）。

        先看含义再看名字：「吃棒棒糖」的含义是「开心时发」，按名字会被一个
        「吃」字塞进吃饭组，跟「干饭」「饿了」挤在一起——那正是要避免的重复。
        """
        g = self._group_cache.get(name)
        if g is not None:
            return g
        d = self.stickers.get(name) or {}
        meaning = d.get("meaning") or ""
        g = ""
        for grp, kws in STICKER_GROUP_BY_NAME_FIRST:
            if any(k in name for k in kws):
                g = grp
                break
        if not g:
            for grp, kws in STICKER_GROUPS:
                if any(k in meaning for k in kws):
                    g = grp
                    break
        if not g:
            for grp, kws in STICKER_GROUP_BY_NAME:
                if any(k in name for k in kws):
                    g = grp
                    break
        g = g or f"·{name}"
        self._group_cache[name] = g
        return g

    def _group_pick(
        self, members: list[str], umo: str, sc: dict[str, float] | None = None
    ) -> str:
        """组内挑代表：只在「同一档」的图里轮换。

        组里可能有 15 张，但不是每张都同样贴切：「恭喜上岸」这组里
        「好耶」是全词命中（1.0），「吃棒棒糖」只是含义写着「开心时发」，
        加权随机一路摇到后者就成了答非所问。所以先按最高分切一档
        （92% 以上才算同级替代），再在档内按「越靠前、越少用过」加权抽。
        """
        if len(members) == 1:
            return members[0]
        if sc:
            top = max(sc.get(m, 0.0) for m in members)
            same = [m for m in members if sc.get(m, 0.0) >= top * 0.92]
            if same:
                members = same
        used = self._recent_used(umo) if umo else set()
        cnt = self._use_count.get(umo, {})
        idx = [i for i, n in enumerate(members) if n not in used] or list(range(len(members)))
        weights = [
            1.0 / (1.0 + 0.6 * i) / (1.0 + cnt.get(members[i], 0)) for i in idx
        ]
        return members[random.choices(idx, weights=weights, k=1)[0]]

    def _collapse_by_group(
        self,
        pool: list[str],
        umo: str,
        n: int,
        sc: dict[str, float] | None = None,
        hit_clusters: set[str] | None = None,
    ) -> tuple[list[str], set[str]]:
        """候选池按语义组折叠：一组一张，组的顺序 = 组内最好那张的得分。

        pool 是按相关度降序传进来的。但整词命中的图往往一二十张并列 1.0 分，
        纯按池子顺序排组，会出现「你猜我为什么生气」说了生气、前五个候选位
        却被别的同分组占光的情况。所以先把「这句话明确说出的情绪组」顶到最前，
        组内再按最高分排；都没说的组维持池子顺序垫后。
        """
        buckets: dict[str, list[str]] = {}
        for name in pool:
            buckets.setdefault(self._group_of(name), []).append(name)
        hits = set(hit_clusters or ())

        def _group_pref(group: str) -> int:
            clusters = GROUP_TO_CLUSTER.get(group) or (
                (group,) if group in EMOTION_LEXICON else ()
            )
            return 0 if any(c in hits for c in clusters) else 1

        ordered = sorted(
            buckets.items(),
            key=lambda kv: (_group_pref(kv[0]), -(max(
                (sc or {}).get(m, 0.0) for m in kv[1]
            ))),
        )
        picked: list[str] = []
        for _g, members in ordered:
            if len(picked) >= n:
                break
            picked.append(self._group_pick(members, umo, sc))
        return picked, {self._group_of(x) for x in picked}

    def _fallback_candidates(
        self,
        umo: str,
        n: int,
        exclude: set[str] | None = None,
        text: str = "",
        skip_groups: set[str] | None = None,
    ) -> list[str]:
        """兜底候选：命中的情绪簇各取一张代表图，轮转后随机取 n 张。

        为什么按情绪簇而不是随机抽全库：随机抽会推出「斗图必杀技」「讨要小钱钱」
        这种强语境图，AI 一看就知道不贴切、直接弃权；而「笑/无语/累了/吃瓜」
        这类基础情绪图放在任何一句闲聊后面都不算违和，AI 才有得选。

        text 传进来时只从「这句话真正命中的簇」里取：原来不分青红皂白遍历
        全部 25 个簇，于是「这瓜真甜」会被塞进「想睡」簇的睡大觉图。
        """
        if n <= 0:
            return []
        exclude = set(exclude or ())
        used = self._recent_used(umo) if umo else set()
        # 已用过的语义组不再补：主路给了「开心」，兜底别再塞一张「开心3」
        skip = set(skip_groups or ())
        buckets: list[list[str]] = []
        seen: set[str] = set()
        emos = list(EMOTION_LEXICON)
        if text:
            hit = [
                e for e in emos
                if e in text or any(v in text for v in EMOTION_LEXICON[e])
            ]
            if hit:
                emos = hit
        for emo in emos:
            bucket: list[str] = []
            cache = self._fallback_cache.get(emo)
            if cache is None:
                for name in self._rank_pool(emo, "", limit=6):
                    d = self.stickers.get(name, {})
                    if d.get("kind") != "image":
                        continue
                    if not (d.get("meaning") or "").strip():
                        continue
                    if name in bucket:
                        continue
                    bucket.append(name)
                self._fallback_cache[emo] = bucket
                cache = bucket
            # 簇内去掉已经出现过的图，保证 5 张来自不同情绪
            fresh = [
                x for x in cache
                if x not in exclude and x not in seen and self._group_of(x) not in skip
            ]
            fresh = [x for x in fresh if x not in used] or fresh
            if fresh:
                pick = random.choice(fresh)
                seen.add(pick)
                skip.add(self._group_of(pick))
                buckets.append([pick])
        random.shuffle(buckets)
        return [b[0] for b in buckets][:n]
