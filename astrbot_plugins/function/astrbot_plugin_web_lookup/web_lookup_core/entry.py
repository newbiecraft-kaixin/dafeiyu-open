"""联网查证主入口与单链接抓取。"""
from __future__ import annotations

import asyncio
import re
import time

import aiohttp

from .bili_summary import _bili_summary
from .extract import (
    _is_dead_page, _looks_nav, _looks_paywall, _page_bonus, _pre_rank,
    similarity,
)
from .http import _fetch_all, _fetch_one
from .loader import _video_parse
from .models import DEFAULT_CFG, HEADERS, _cite_url, _domain_weight
from .search import _search

_GOOD_SCORE = 0.7  # 低于此值视为「没查准」，触发降词重查（0.45 那档放过太多假匹配）


def _reduced_query(query: str) -> str:
    """去掉尾部噪声词：搜索引擎（尤其 Bing）对「xxx 决赛时间 对阵」这种拖泥带水
    的词形会匹配崩。中文查询不能砍到只剩一两个词——「杰哥 阿伟 梗 来源」砍成
    「杰哥 阿伟」会直接掉进「杰」的字典页；保留前三个词语义还在。"""
    terms = query.split()
    if len(terms) <= 2:
        return query
    if re.search(r"[\u4e00-\u9fff]", query):
        return " ".join(terms[:3])
    return terms[0] if len(terms[0]) >= 6 else " ".join(terms[:2])


_CACHE: dict[str, tuple[float, str]] = {}


_CACHE_TTL_S = 1200  # 同一问题 20 分钟内复用结果，避免把同一批页面重复抓一遍


async def lookup(query: str, cfg: dict | None = None) -> str:
    q = (query or "").strip()
    hit = _CACHE.get(q)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    timeout = aiohttp.ClientTimeout(
        total=(int(cfg["timeout_s"]) + int(cfg["search_timeout_s"]) + 4) * 2
    )
    async with aiohttp.ClientSession(
        timeout=timeout, headers=HEADERS, trust_env=bool(cfg.get("trust_env"))
    ) as sess:
        result, score = await _run_once(sess, query, cfg)
        # 首轮没查准 → 降词重查一轮，取更优结果（LLM 自己换词重试不可靠，代码里兜）
        reduced = _reduced_query(query)
        if score < _GOOD_SCORE and reduced != query:
            result2, score2 = await _run_once(sess, reduced, cfg)
            if score2 > score:
                result, score = result2, score2
                result = result.replace(f"「{query}」", f"「{reduced}」", 1)
    if score >= 0.25:  # 只缓存真正查到的结果，网络抖动导致的失败不缓存
        _CACHE[q] = (time.time(), result)
        if len(_CACHE) > 200:
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:60]:
                _CACHE.pop(k, None)
    return result


async def _run_once(sess: aiohttp.ClientSession, query: str, cfg: dict) -> tuple[str, float]:
    # ① 扫描 + ② URL 合并（去重/去追踪参数/RRF 融合，见 _search）
    candidates = await _search(sess, query, cfg)
    if not candidates:
        return ("没搜到可用结果（搜索引擎没返回链接名单）。", 0.0)

    # ③ 标题相似度 + 域名权重预排 → 只抓最可能相关的 N 条
    targets = _pre_rank(query, candidates, int(cfg["keep_n"]))
    # ④ 抓取（同一连接里做 400/403/404/429/5xx 校验 + 流式读正文）
    pages, reasons = await _fetch_all(sess, targets, cfg)

    # ⑤ 正文清洗：失效页 / 空壳导航页 / 验证墙
    bad = [
        p for p in pages
        if _is_dead_page(p[1], p[2]) or _looks_nav(p[1], p[2]) or _looks_paywall(p[1], p[2])
    ]
    usable = [p for p in pages if id(p) not in {id(x) for x in bad}]
    clean_note = (
        f"（清洗记录：候选 {len(candidates)} 条 → 抓取 {len(targets)} 条，"
        f"状态码/超时淘汰 {sum(reasons.values())} 条，正文无效剔除 {len(bad)} 条）"
    )
    if not usable:
        detail = "、".join(f"{k}×{v}" for k, v in reasons.items()) or "无"
        # 正文全抓不到时退一步：用搜索结果的摘要兜底（至少给模型一个有来源的依据，
        # 比直接回「没查到」强得多；摘要里往往已经有关键结论）
        snips = sorted(
            ((similarity(query, t, sn), t, sn, u) for u, t, _r, _e, sn in candidates if sn),
            key=lambda x: x[0],
            reverse=True,
        )
        if snips and snips[0][2]:
            s0, t0, sn0, u0 = snips[0]
            return (
                f"{clean_note}\n正文没抓到，只有搜索结果摘要（相似度 {s0:.2f}）：\n"
                f"标题：{t0}\n来源：{_cite_url(u0)}\n"
                f"摘要：{re.sub(r'\\s+', ' ', sn0).strip()[: int(cfg['max_chars'])]}\n"
                "注意：这只是搜索摘要、不是全文。能据此回答就答，细节不够就明说没查到。",
                s0,
            )
        return (
            f"{clean_note}\n扫到 {len(candidates)} 条链接，但清洗后没有可用正文"
            f"（失败类型：{detail}）。换个说法再查一次，或直接说没查到。",
            0.0,
        )

    # ⑥ 最终按「查询 vs 标题+正文」相似度排序，再叠加权重分（大站 / 文章页 / 深度）
    scored = [
        (
            similarity(query, t, b) + _domain_weight(h) * 0.1 + _page_bonus(h),
            h, t, b, u,
        )
        for h, t, b, u in usable
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score = scored[0][0]
    # 多源正文：把相关度够高的前 3 页正文都给模型。原来只给最相关的 1 页，
    # 模型等于「只看了一个网页」，交叉验证不了、答案又窄又容易错。这里要求
    # 追加页既过绝对下限（>=0.4）又不太低于首选（>=best*0.7），只有一页真相关
    # 时就只给一页，不用垃圾页凑数。几页共享同一 token 预算（max_chars）。
    chosen = [scored[0]]
    for cell in scored[1:]:
        if len(chosen) >= 3:
            break
        if cell[0] >= 0.4 and cell[0] >= best_score * 0.7:
            chosen.append(cell)
    per = max(600, int(cfg["max_chars"]) // len(chosen))
    if len(chosen) == 1:
        parts = [f"【联网查证】与「{query}」最相关的资料（相似度 {best_score:.2f}）："]
    else:
        parts = [f"【联网查证】与「{query}」相关的资料，共 {len(chosen)} 个来源"
                 f"（按相关度排序，可交叉印证）："]
    for i, (sc_i, _h, t, b, u) in enumerate(chosen, 1):
        body_i = re.sub(r"\n+", " ", b).strip()[:per]
        if len(chosen) == 1:
            parts += [f"标题：{t}", f"来源：{u}", f"内容：{body_i}"]
        else:
            parts.append(f"\n[{i}] 相似度 {sc_i:.2f} 《{t[:40]}》\n来源：{u}\n内容：{body_i}")
    if best_score < _GOOD_SCORE:
        parts.append(
            "注意：以上匹配度偏低，大概率不是用户要的答案。要么换个说法再查一次，"
            "要么老实说没查到，绝不能基于这些内容编造。"
        )
    if len(scored) > len(chosen):
        others = "；".join(f"《{t[:24]}》{u}" for _, h, t, _, u in scored[len(chosen):5])
        if others:
            parts.append(f"其他候选（需要可再查）：{others}")
    if reasons or bad:
        parts.append(clean_note)
    return "\n".join(parts), best_score


async def fetch_url(url: str, cfg: dict | None = None) -> str:
    """直接抓一个链接的正文（用户发链接过来时用，不经过搜索）。"""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    vp = _video_parse()
    platform = vp.detect_platform(url)
    if platform and platform != "bilibili":
        # 抖音/快手/西瓜：元信息 + 前 N 条评论 + 字幕/本地转写
        data = await vp.parse_video(url, cfg)
        if data.get("title") or data.get("author") or data.get("comments"):
            return vp.render_text(data, cfg)
    if platform == "bilibili" or re.search(
        r"(b23\.tv|bili2233\.cn|bilibili\.com)", url, re.I
    ):
        # B站保留原有 cookie/wbi/字幕/总结逻辑；失败再走 video_parse 兜底
        note = await _bili_summary(url, cfg)
        if note:
            return f"【B站视频】\n来源：{url}\n内容：{note}"
        data = await vp.parse_video(url, cfg)
        if data.get("title") or data.get("author") or data.get("comments"):
            return vp.render_text(data, cfg)
    timeout = aiohttp.ClientTimeout(total=int(cfg["timeout_s"]) + 4)
    async with aiohttp.ClientSession(
        timeout=timeout, headers=HEADERS, trust_env=bool(cfg.get("trust_env"))
    ) as sess:
        page, reason = await _fetch_one(sess, url, cfg)
    if not page:
        return f"这个链接抓不到正文（{reason or '内容为空'}），先别引用。"
    if _is_dead_page(page[1], page[2]) or _looks_paywall(page[1], page[2]):
        return "这个链接是失效页或登录墙，正文不可用，先别引用。"
    _, title, body, real_url = page
    body = re.sub(r"\n+", " ", body).strip()[: int(cfg["max_chars"])]
    return f"【网页正文】{title}\n来源：{real_url or url}\n内容：{body}"
