"""正文抽取、相似度、失效页/导航页/验证墙识别与预排序。"""
from __future__ import annotations

import re

from .models import _STOP_WORDS, _domain_weight, _host
from .text_extract import _TextExtract

_NOISE_TAGS = (
    "script", "style", "noscript", "iframe", "nav", "footer", "header",
    "aside", "form", "select", "button", "template",
)


_ARTICLE_XPATH = (
    "//article | //main | //*[@id='content'] | //*[@id='article'] "
    "| //*[contains(@class,'content')] | //*[contains(@class,'article')] "
    "| //*[contains(@class,'post')] | //*[contains(@class,'detail')]"
)


def _lxml_text(html: str) -> tuple[str, str]:
    try:
        from lxml import etree, html as LH
    except Exception:  # noqa: BLE001
        return "", ""
    try:
        doc = LH.fromstring(html, parser=LH.HTMLParser(recover=True, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "", ""
    if doc is None:
        return "", ""
    try:
        etree.strip_elements(doc, *_NOISE_TAGS, with_tail=False)
    except Exception:  # noqa: BLE001
        pass
    title = ""
    try:
        t = doc.find(".//title")
        if t is not None:
            title = re.sub(r"\s+", " ", t.text_content() or "").strip()
    except Exception:  # noqa: BLE001
        title = ""
    best = ""
    try:
        for el in doc.xpath(_ARTICLE_XPATH)[:40]:
            s = " ".join(x.strip() for x in el.itertext() if x and x.strip())
            if len(s) > len(best):
                best = s
    except Exception:  # noqa: BLE001
        best = ""
    if len(best) < 200:  # 没命中文章块就用整页文本
        best = " ".join(x.strip() for x in doc.itertext() if x and x.strip())
    best = re.sub(r"[ \t\r\f\v]+", " ", best).strip()
    best = re.sub(r"\n\s*\n+", "\n", best)
    return title, best


def html_to_text(html: str) -> tuple[str, str]:
    """HTML → (标题, 正文)。优先用 lxml 按「文章块优先 + 去噪声标签」提取，
    lxml 不在/解析失败时退回纯 HTMLParser。

    业界正文抽取（trafilatura/readability）的核心就三步：先删脚本导航等噪声标签，
    再优先 <article>/<main>/正文容器，最后取文本最长的块。纯 HTMLParser 把所有
    div 文本一股脑拼起来，导航/推荐位很容易压过正文，抓下来就是「正文过短」。
    """
    title, text = _lxml_text(html)
    if text:
        return title, text
    p = _TextExtract()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return p.title.strip(), text.strip()


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[\s，。、；：！？!?,.:;\"'()（）\[\]【】|-]+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


_JIEBA = None


def _jieba_mod():
    global _JIEBA
    if _JIEBA is None:
        try:
            import jieba

            jieba.setLogLevel(60)  # 关掉首次加载的「Building prefix dict」日志
            _JIEBA = jieba
        except Exception:  # noqa: BLE001
            _JIEBA = False
    return _JIEBA or None


def _words(s: str) -> set[str]:
    """中文分词得到实词集合；没有 jieba 就退回二元组。"""
    jb = _jieba_mod()
    if not jb:
        return _bigrams(s)
    return {w for w in jb.lcut(s.lower()) if len(w) >= 2 and w not in _STOP_WORDS}


def similarity(query: str, title: str, body: str) -> float:
    """查询与标题+正文的相关度。

    「杰哥 阿伟 梗」这种长尾中文查询只有几个二元组，正文里没有一模一样的
    二元组就几乎得 0 分（之前 0.15 就是这个原因）；加入 jieba 词命中后，
    只要关键词出现过就能有分，长尾查询不再被误判成「没查到」。"""
    qb = _bigrams(query)
    if not qb:
        return 0.0
    doc = title + " " + body[:4000]
    score = len(qb & _bigrams(doc)) / len(qb)
    qt = _words(query)
    if qt:
        score = 0.55 * score + 0.45 * len(qt & _words(doc)) / len(qt)
    # 标题直接命中关键词再加权
    for w in re.split(r"\s+", query):
        if len(w) >= 2 and w.lower() in title.lower():
            score += 0.15
            break
    return score


_PAYWALL_STRONG = re.compile(
    r"百度安全验证|人机验证|滑块验证|请开启\s*JavaScript|请绑定大区|"
    r"扫码登录|扫码登录后|登录后才能(查看|浏览|访问)|访问验证|安全验证"
)


_PAYWALL_WEAK = re.compile(r"请登录|登录后查看|验证码|授权登录|请先登录")


def _looks_paywall(title: str, text: str) -> bool:
    head = text[:600]
    if _PAYWALL_STRONG.search(head):
        return True
    return len(text) < 500 and bool(_PAYWALL_WEAK.search(head))


_DEAD_RE = re.compile(
    r"404\s*Not\s*Found|页面(不存在|找不到|已删除|已失效|已下线)|"
    r"该(内容|文章|帖子|页面)(不存在|已删除|已被删除)|内容(不存在|已删除|已过期)|"
    r"抱歉[，, ]*(您|你)?(要|所)?(访问|查找|请求)?的?[^。]{0,12}(不存在|找不到|已删除)|"
    r"页面走丢了|链接(已|可能)失效|内容正在审核"
)


_NAV_WORDS = re.compile(r"首页|登录|注册|广告|版权|联系我们|关于我们|网站地图|友情链接")


def _is_dead_page(title: str, text: str) -> bool:
    return bool(_DEAD_RE.search((title or "") + "\n" + (text or "")[:400]))


def _avg_sentence_len(text: str) -> float:
    """平均句长。文章是真句子（长句多）；首页/聚合页是短语堆砌，几乎没有句号。"""
    sents = [s for s in re.split(r"[。！？!?]", text) if len(s.strip()) > 5]
    return sum(len(s) for s in sents) / len(sents) if sents else 0.0


def _looks_nav(title: str, text: str) -> bool:
    # 长文 + 长句 = 正常文章，导航词再多也放行
    if len(text) >= 800 and _avg_sentence_len(text) >= 18:
        return False
    return len(_NAV_WORDS.findall(text)) >= 4


_ARTICLE_RE = re.compile(r"[/(](article|news|a|p|post|detail|content|blog|story)[/\-]?")


_INDEX_HINT = re.compile(r"[/(](index|home|default|list|category|column)")


def _path_depth(src: str) -> int:
    path = src.split("/", 1)[1] if "/" in src else ""
    return len([s for s in path.split("/") if s])


def _page_bonus(src: str) -> float:
    """⑥ 权重加分：深路径 + 文章特征加分，首页/栏目页减分。"""
    depth = _path_depth(src)
    bonus = min(depth, 3) * 0.08
    if _ARTICLE_RE.search(src):
        bonus += 0.12
    elif depth <= 1 or _INDEX_HINT.search(src):
        bonus -= 0.15
    return bonus


def _pre_rank(query: str, cands: list, keep_n: int) -> list[str]:
    """⑤ 先筛后抓：按「标题相似度 + 域名权重 + 多引擎共识」排序，
    只把最可能相关的 N 条送去抓正文（省带宽，也避免垃圾页占满名额）。
    同一域名最多 2 条、同一标题只抓一次——跳转链和真实 URL 常指向同一篇。"""
    scored = []
    for u, t, rrf, eng, _snip in cands:
        s = similarity(query, t, "")
        s += _domain_weight(u) * 0.3
        s += min(rrf, 1.5) * 0.15 + min(eng - 1, 2) * 0.1
        scored.append((s, u, t))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked: list[str] = []
    cap: dict[str, int] = {}
    seen: set[str] = set()
    for _, u, t in scored:
        h = _host(u)
        key = re.sub(r"\s+", "", t)[:14]
        if cap.get(h, 0) >= 2 or key in seen:
            continue
        cap[h] = cap.get(h, 0) + 1
        seen.add(key)
        picked.append(u)
        if len(picked) >= keep_n:
            break
    return picked
