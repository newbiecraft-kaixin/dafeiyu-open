"""多引擎搜索与 RRF 融合。"""
from __future__ import annotations

import asyncio
import random
import re
from html import unescape
from urllib.parse import quote_plus

import aiohttp

from .models import _STOP_WORDS, _norm_url, _ok_url

async def _bing(sess: aiohttp.ClientSession, query: str, cfg: dict) -> list[tuple[str, str, str]]:
    """Bing RSS 通道：真实 URL + 标题 + 摘要，结构稳定、免 key。

    网页版 cn.bing.com 在国内对中文长查询会被拆成单字（「杰哥 阿伟 梗…」返回一堆
    「杰」的字典页），而且 HTML 结构常变、要正则硬抠；RSS 走的是同一套排序，
    但输出是标准 XML，带 description 摘要——抓不到正文时摘要还能兜底。
    """
    url = (
        f"https://www.bing.com/search?q={quote_plus(query)}"
        f"&format=rss&mkt=zh-CN&count={int(cfg['max_results'])}"
    )
    async with sess.get(
        url, timeout=aiohttp.ClientTimeout(total=cfg["search_timeout_s"])
    ) as r:
        if r.status != 200:
            return []
        xml = await r.text(errors="ignore")
    out: list[tuple[str, str, str]] = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        lm = re.search(r"<link>(.*?)</link>", it, re.S)
        tm = re.search(r"<title>(.*?)</title>", it, re.S)
        dm = re.search(r"<description>(.*?)</description>", it, re.S)
        if not lm or not tm:
            continue
        title = unescape(re.sub(r"<[^>]+>", "", tm.group(1))).strip()
        if not title:
            continue
        snippet = unescape(re.sub(r"<[^>]+>", "", dm.group(1))).strip() if dm else ""
        out.append((unescape(lm.group(1)).strip(), title, snippet))
    return out


async def _baidu(sess: aiohttp.ClientSession, query: str, cfg: dict) -> list[tuple[str, str, str]]:
    # 百度结果是 redirect 链接，后续校验抓取时会自动 302 跟到真页。
    # 必须带 Referer，否则百度直接回「百度安全验证」页（0 结果）
    url = f"https://www.baidu.com/s?wd={quote_plus(query)}&rn=12"
    async with sess.get(
        url,
        headers={"Referer": "https://www.baidu.com/"},
        timeout=aiohttp.ClientTimeout(total=cfg["search_timeout_s"]),
    ) as r:
        if r.status != 200:
            return []
        html = await r.text(errors="ignore")
    out: list[tuple[str, str, str]] = []
    for m in re.finditer(
        r'<h3[^>]*>\s*<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', html, re.S
    ):
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if title:
            out.append((m.group(1), title, ""))
    return out


async def _so360(sess: aiohttp.ClientSession, query: str, cfg: dict) -> list[tuple[str, str, str]]:
    # 360 搜索：免 Cookie 可用，结果同样是 /link?m= 跳转链（跟到真页；360 现在
    # 用「200 + 小页面里的 JS location.replace」跳转，_fetch_one 会识别这种壳）
    url = f"https://www.so.com/s?q={quote_plus(query)}"
    async with sess.get(
        url,
        headers={"Referer": "https://www.so.com/"},
        timeout=aiohttp.ClientTimeout(total=cfg["search_timeout_s"]),
    ) as r:
        if r.status != 200:
            return []
        html = await r.text(errors="ignore")
    out: list[tuple[str, str, str]] = []
    for m in re.finditer(
        r'<h3[^>]*>\s*<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', html, re.S
    ):
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if title:
            out.append((m.group(1), title, ""))
    return out


async def _sogou(sess: aiohttp.ClientSession, query: str, cfg: dict) -> list[tuple[str, str, str]]:
    # 搜狗：结果链接是 /link?url= 相对跳转链（302 跟到真站），需补全域名
    url = f"https://www.sogou.com/web?query={quote_plus(query)}"
    async with sess.get(
        url,
        headers={"Referer": "https://www.sogou.com/"},
        timeout=aiohttp.ClientTimeout(total=cfg["search_timeout_s"]),
    ) as r:
        if r.status != 200:
            return []
        html = await r.text(errors="ignore")
    out: list[tuple[str, str, str]] = []
    for m in re.finditer(r'<a[^>]+href="(/link\?url=[^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href = "https://www.sogou.com" + unescape(m.group(1))
        # 锚点文本里常混着摘要和裸链接，砍掉链接部分再截断
        title = re.sub(r"<[^>]+>", "", m.group(2))
        title = re.split(r"http", unescape(title))[0]
        title = re.sub(r"\s+", " ", title).strip()[:40]
        if title:
            out.append((href, title, ""))
    return out


def _core_query(query: str) -> str:
    """中文长查询的核心短语：取前两个实词拼成紧凑短语，供 Bing 精确通道用。

    Bing 会把空格分隔的中文长查询拆成单字（「杰哥 阿伟 梗…」→「杰」字典页）；
    先只留前两个实词（「杰哥 阿伟」）并加引号保词序，就能命中真正相关的页面。
    """
    terms = [
        t for t in re.split(r"\s+", query.strip())
        if len(t) >= 2 and t not in _STOP_WORDS
    ]
    if len(terms) < 2 or not re.search(r"[\u4e00-\u9fff]", query):
        return ""
    # 用引号保住词序（去掉引号直接拼成「天气上海」这种非词会有害）
    return '"' + " ".join(terms[:2]) + '"'


async def _search(
    sess: aiohttp.ClientSession, query: str, cfg: dict
) -> list[tuple[str, str, float, int, str]]:
    """① 扫描 ② URL 合并：多引擎并行 → 规范化 URL 去重 → RRF 融合 + 多引擎共识。

    返回 [(url, title, rrf, 命中引擎数, 摘要)]，给后面的标题预排/摘要兜底用。
    百度/360/搜狗反爬是间歇性抖动（同一请求时好时坏，预热/Cookie 都没用），
    所以每个引擎失败自动重试 2 次——多引擎并行下至少一个出结果的概率接近 100%。

    另外必应多开一条「核心短语」通道：Bing 对空格分隔的长中文查询会拆成单字
    （「杰哥 阿伟 梗 来源 …」返回一堆「杰」的字典页），去掉空格只留前两个词
    （「杰哥阿伟」）就正常了。这条通道专门治长尾中文查询查不准。
    """
    async def run(eng):
        for attempt in range(3):
            try:
                items = await eng(sess, query, cfg)
            except Exception:
                items = []
            if items:
                return items
            await asyncio.sleep(0.4 * (attempt + 1) + random.uniform(0, 0.3))
        return []

    core_q = _core_query(query)
    tasks = [run(_bing), run(_baidu), run(_so360), run(_sogou)]
    if core_q:
        tasks.append(run(lambda s, _q, c: _bing(s, core_q, c)))
    engines = await asyncio.gather(*tasks)
    merged: dict[str, dict] = {}
    for items in engines:
        rank = 0
        for u, t, snip in items:
            if not _ok_url(u) or not t:
                continue
            rank += 1
            key = _norm_url(u)
            cell = merged.get(key)
            if cell is None:
                merged[key] = {
                    "url": u, "title": t, "rrf": 1.0 / (rank + 1),
                    "engines": 1, "snippet": snip,
                }
            else:
                cell["rrf"] += 1.0 / (rank + 1)
                cell["engines"] += 1
                if len(t) > len(cell["title"]):
                    cell["title"] = t  # 保留更完整的标题
                if len(snip) > len(cell["snippet"]):
                    cell["snippet"] = snip
    # 排序：RRF 为主，多引擎共识为辅（不能以「出现引擎数」为第一关键字，
    # 否则大众词条的无关页（如节假日通知）会把单引擎命中的精准文章挤出去）
    order = sorted(
        merged.values(), key=lambda c: c["rrf"] + (c["engines"] - 1) * 0.25, reverse=True
    )
    return [
        (c["url"], c["title"], c["rrf"], c["engines"], c["snippet"]) for c in order
    ][: int(cfg["max_results"])]
