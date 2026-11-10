"""B站按标签抓取、片单池、停留/点赞与刷视频入口。"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from html import unescape
from urllib.parse import quote_plus

import aiohttp

try:
    from astrbot.api import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger(__name__)

from .bili_api import (
    _BILI_TAGS, _BILI_VIEW, _bili_headers, _bili_hot_comments,
    _bili_reply_list, _bili_subtitles, _bili_wbi_mixin,
)
from .models import DEFAULT_CFG, HEADERS, UA, _dur_sec
from .render import _bili_line

_BILI_SEARCH = "https://api.bilibili.com/x/web-interface/search/type"


_BILI_POPULAR = "https://api.bilibili.com/x/web-interface/popular"


_BILI_LIKE = "https://api.bilibili.com/x/web-interface/archive/like"


def _norm_bili_item(raw, tag: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    bvid = str(raw.get("bvid") or "").strip()
    title = unescape(re.sub(r"<[^>]+>", "", str(raw.get("title") or ""))).strip()
    if not bvid or not title:
        return None
    owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
    stat = raw.get("stat") if isinstance(raw.get("stat"), dict) else {}
    try:
        play = int(raw.get("play") or stat.get("view") or 0)
    except (TypeError, ValueError):
        play = 0
    return {
        "bvid": bvid,
        "title": title,
        "author": str(raw.get("author") or owner.get("name") or "").strip(),
        "play": play,
        "duration": _dur_sec(raw.get("duration")),
        "tag": str(tag or ""),
        "url": f"https://www.bilibili.com/video/{bvid}",
        "ts": int(time.time()),
        "served": 0,
    }


async def _bili_search_tag(
    sess: aiohttp.ClientSession, tag: str, cfg: dict, limit: int
) -> list[dict]:
    params = {"search_type": "video", "keyword": tag, "page": 1, "order": "totalrank"}
    headers = _bili_headers(
        cfg, "https://search.bilibili.com/video?keyword=" + quote_plus(tag)
    )
    for _ in range(2):
        try:
            async with sess.get(
                _BILI_SEARCH,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                if r.status != 200:
                    return []
                p = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return []
        if isinstance(p, dict) and p.get("code") == 0:
            rows = ((p.get("data") or {}).get("result") or [])
            out = [
                x for x in (_norm_bili_item(it, tag) for it in rows if isinstance(it, dict))
                if x
            ]
            if out:
                return out[:limit]
        await asyncio.sleep(0.5)
    return []


async def _bili_popular(sess: aiohttp.ClientSession, cfg: dict, limit: int) -> list[dict]:
    headers = _bili_headers(cfg, "https://www.bilibili.com/v/popular/all")
    try:
        async with sess.get(
            _BILI_POPULAR,
            params={"ps": max(10, min(50, limit)), "pn": 1},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=12),
        ) as r:
            if r.status != 200:
                return []
            p = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []
    rows = ((p.get("data") or {}).get("list") or []) if isinstance(p, dict) else []
    out = [
        x for x in (_norm_bili_item(it, "热门") for it in rows if isinstance(it, dict)) if x
    ]
    return out[:limit]


_BILI_CARD_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const links = document.querySelectorAll('a[href*="/video/BV"], a[href*="/video/av"]');
  for (const a of links) {
    const href = a.href || a.getAttribute('href') || '';
    const m = href.match(/\/video\/(BV[0-9A-Za-z]{10}|av\d+)/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    const card = a.closest('.bili-video-card, .video-item, li, div') || a;
    const t = card.querySelector('.bili-video-card__info--tit, .title, .bili-video-card__info--tit a');
    const au = card.querySelector('.bili-video-card__info--author, .up-name, .bili-video-card__info--bottom a, [class*="author"], [class*="up-name"]');
    const st = card.querySelectorAll('.bili-video-card__stats--item, .bili-video-card__stats span');
    const du = card.querySelector('.bili-video-card__stats__duration, .duration');
    out.push({
      bvid: m[1],
      title: (t && t.textContent.trim()) || a.getAttribute('title') || '',
      author: (au && au.textContent.trim()) || '',
      play: st.length ? st[0].textContent.trim() : '',
      duration: (du && du.textContent.trim()) || '',
    });
  }
  return out;
}
"""


_BILI_DETAIL_JS = r"""
() => {
  const s = window.__INITIAL_STATE__;
  if (!s) return null;
  const v = s.videoData || (s.video && s.video.videoData) || null;
  if (!v) return null;
  const tags = (s.tags || []).map(x => x && x.tag_name).filter(Boolean).slice(0, 6);
  return {
    title: v.title || '',
    desc: v.desc || '',
    duration: v.duration || 0,
    play: (v.stat && v.stat.view) || 0,
    tname: v.tname || '',
    pages: (v.pages || []).length,
    tags: tags,
    author: (v.owner && v.owner.name) || '',
    pubdate: v.pubdate || 0,
  };
}
"""


def _play_count(v) -> int:
    """「12.3万」/「1.2亿」/「1234」→ 整数播放量。"""
    s = str(v or "").strip().replace(",", "")
    m = re.match(r"([\d.]+)\s*(万|亿)?", s)
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0
    if m.group(2) == "亿":
        n *= 100000000
    elif m.group(2) == "万":
        n *= 10000
    return int(n)


def _bili_cookie_jar(raw: str) -> list[dict]:
    out: list[dict] = []
    for part in str(raw or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() and v.strip():
            out.append({
                "name": k.strip(), "value": v.strip(),
                "domain": ".bilibili.com", "path": "/",
            })
    return out


async def _human_scroll(page) -> None:
    """像人一样滚两屏再停一下：真浏览器 + 停留，降低被判定为脚本的概率。"""
    try:
        for _ in range(random.randint(1, 3)):
            step = random.randint(320, 900)
            await page.mouse.move(random.randint(200, 1100), random.randint(300, 700))
            await page.mouse.wheel(0, step)
            await asyncio.sleep(random.uniform(0.6, 1.6))
        await asyncio.sleep(random.uniform(0.5, 1.5))
    except Exception:  # noqa: BLE001
        pass


async def _bili_browser_fetch(cfg: dict, tags: list[str], limit: int) -> list[dict]:
    """用 Playwright 真浏览器抓搜索页 + 打开视频页取详情。

    抓不到（没装浏览器/被拦）返回空列表，调用方自动回退 HTTP 接口。
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 浏览器模式不可用: {type(e).__name__}: {e}")
        return []

    want = max(4, int(limit or 8))
    # 0 = 自动：最多真打开 8 个视频页；配了具体数字就按数字来（都是真实停留）
    raw_detail = int(cfg.get("bili_browser_detail") or 0)
    detail_n = min(want, 8) if raw_detail <= 0 else min(want, raw_detail)
    out: list[dict] = []
    seen: set[str] = set()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--mute-audio",
                ],
            )
            try:
                ctx = await browser.new_context(
                    user_agent=UA,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 810},
                    extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
                )
                await ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
                jar = _bili_cookie_jar(cfg.get("bili_cookie") or "")
                if jar:
                    try:
                        await ctx.add_cookies(jar)
                    except Exception as e:  # noqa: BLE001
                        logger.debug(f"[web_lookup] 浏览器 cookie 注入失败: {e}")
                page = await ctx.new_page()
                for tag in tags:
                    if len(out) >= want:
                        break
                    try:
                        await page.goto(
                            "https://search.bilibili.com/all?keyword=" + quote_plus(str(tag)),
                            wait_until="domcontentloaded",
                            timeout=25000,
                        )
                        await _human_scroll(page)
                        rows = await page.evaluate(_BILI_CARD_JS)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            f"[web_lookup] 浏览器搜索「{tag}」失败: {type(e).__name__}: {str(e)[:100]}"
                        )
                        continue
                    for r in (rows or []):
                        if not isinstance(r, dict):
                            continue
                        bvid = str(r.get("bvid") or "").strip()
                        if not bvid.startswith("BV") or bvid in seen:
                            continue
                        title = str(r.get("title") or "").strip()
                        if not title:
                            continue
                        seen.add(bvid)
                        # 卡片底部常把「UP · 10月21日」写在一起，作者只留人名
                        raw_author = re.split(
                            r"\s*[·•]\s*", str(r.get("author") or "").strip()
                        )[0].strip()
                        out.append({
                            "bvid": bvid,
                            "title": title[:120],
                            "author": raw_author[:40],
                            "play": _play_count(r.get("play")),
                            "duration": _dur_sec(r.get("duration")),
                            "tag": str(tag),
                            "url": f"https://www.bilibili.com/video/{bvid}",
                            "ts": int(time.time()),
                            "served": 0,
                            "_browser": 1,
                        })
                        if len(out) >= want:
                            break
                    await asyncio.sleep(random.uniform(1.0, 2.5))

                # 详情：真的打开视频页停一会儿，顺手把简介/分区/分P拿回来
                detail_page = await ctx.new_page()
                for it in out[:detail_n]:
                    try:
                        await detail_page.goto(
                            it["url"], wait_until="domcontentloaded", timeout=25000
                        )
                        await _human_scroll(detail_page)
                        d = await detail_page.evaluate(_BILI_DETAIL_JS)
                        if isinstance(d, dict) and d:
                            if not str(it.get("author") or "").strip() and d.get("author"):
                                it["author"] = str(d["author"]).strip()
                            it["desc"] = re.sub(r"\s+", " ", str(d.get("desc") or "")).strip()
                            if d.get("tname"):
                                it["tname"] = str(d["tname"]).strip()
                            if d.get("tags"):
                                it["keywords"] = "、".join(str(x) for x in d["tags"])
                            if int(d.get("pages") or 0) > 1:
                                it["parts"] = int(d["pages"])
                            if d.get("pubdate"):
                                try:
                                    it["pubdate"] = int(d["pubdate"])
                                except (TypeError, ValueError):
                                    pass
                            if not it.get("duration"):
                                it["duration"] = _dur_sec(d.get("duration"))
                            if not it.get("play"):
                                it["play"] = _play_count(d.get("play"))
                        # 在视频页真实停留一会儿，而不是秒开秒关
                        await asyncio.sleep(random.uniform(2.5, 5.5))
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            f"[web_lookup] 浏览器打开视频页失败 {it.get('bvid')}: "
                            f"{type(e).__name__}: {str(e)[:80]}"
                        )
            finally:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 浏览器抓取失败: {type(e).__name__}: {str(e)[:120]}")
        return []
    return out


def bili_pool_load(path: str) -> list[dict]:
    """读片单池（JSON：{"items": [...]}），坏文件一律当空池。"""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    rows = data.get("items") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for it in rows:
        if isinstance(it, dict) and it.get("bvid") and it.get("url"):
            it.setdefault("served", 0)
            it.setdefault("ts", 0)
            out.append(it)
    return out


def bili_pool_save(path: str, items: list[dict], cap: int) -> list[dict]:
    """按 bvid 去重、保最新、超上限裁掉最旧的，然后落盘。"""
    seen: set[str] = set()
    kept: list[dict] = []
    for it in items:
        b = str(it.get("bvid") or "")
        if not b or b in seen:
            continue
        seen.add(b)
        kept.append(it)
        if len(kept) >= max(1, int(cap)):
            break
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"updated": int(time.time()), "items": kept},
                f, ensure_ascii=False, indent=2,
            )
    except (OSError, TypeError):
        pass
    return kept


def _label_match_score(it: dict, tag: str) -> float:
    """本地标签匹配分（0~1）：先按字面/片段命中，不花钱；低分再交 AI 判断。"""
    from difflib import SequenceMatcher

    tag = str(tag or "").strip().lower()
    if not tag:
        return 1.0
    text = " ".join(
        str(it.get(k) or "") for k in ("title", "desc", "sub", "keywords", "tname", "tag")
    ).lower()
    if tag in text:
        return 1.0
    chars = [c for c in tag if c.strip()]
    hit = sum(1 for c in chars if c in text) / max(1, len(chars))
    ratio = SequenceMatcher(None, tag, text[:2000]).ratio()
    return max(hit, ratio)


def _bili_has_content(it: dict) -> bool:
    """有没有能被总结的内容：字幕够长，或简介够长。两样都没有的视频等于无效。"""
    if len(str(it.get("sub") or "").strip()) >= 80:
        return True
    desc = re.sub(r"\s+", "", str(it.get("desc") or ""))
    return len(desc) >= 40


async def _bili_meta(sess: aiohttp.ClientSession, cfg: dict, bvid: str) -> dict:
    """单条视频详情：简介/分区/分P数——搜索接口不带简介，内容得靠这个接口。"""
    headers = _bili_headers(cfg, f"https://www.bilibili.com/video/{bvid}")
    try:
        async with sess.get(
            _BILI_VIEW,
            params={"bvid": bvid},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                return {}
            p = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return {}
    if not isinstance(p, dict) or p.get("code") != 0:
        return {}
    d = p.get("data") or {}
    if not isinstance(d, dict):
        return {}
    out: dict = {}
    try:
        out["aid"] = int(d.get("aid") or 0)
    except (TypeError, ValueError):
        pass
    owner = d.get("owner") if isinstance(d.get("owner"), dict) else {}
    aname = str(owner.get("name") or "").strip()
    if aname:
        out["author"] = aname
    desc = re.sub(r"\s+", " ", str(d.get("desc") or d.get("dynamic") or "")).strip()
    if desc and desc not in ("-", "—", "无", "暂无简介"):
        out["desc"] = desc[:260]
    tname = str(d.get("tname") or "").strip()
    if tname:
        out["tname"] = tname
    pages = d.get("pages") if isinstance(d.get("pages"), list) else []
    if len(pages) > 1:
        out["parts"] = len(pages)
    try:
        pub = int(d.get("pubdate") or d.get("ctime") or 0)
    except (TypeError, ValueError):
        pub = 0
    if pub > 0:
        out["pubdate"] = pub
    # 标签：标题党视频光看标题说不清内容，标签往往更准
    try:
        async with sess.get(
            _BILI_TAGS,
            params={"bvid": bvid},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status == 200:
                tp = await r.json(content_type=None)
                rows = ((tp.get("data") or []) if isinstance(tp, dict) else [])
                names = [
                    str(x.get("tag_name") or "").strip()
                    for x in rows if isinstance(x, dict)
                ]
                names = [x for x in names if x][:6]
                if names:
                    out["keywords"] = "、".join(names)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        pass
    # 字幕：CC/AI 字幕都要登录态，没 cookie 时这里一般回空，不影响别的字段
    aid = d.get("aid")
    cid = d.get("cid")
    if not cid and pages and isinstance(pages[0], dict):
        cid = pages[0].get("cid")
    try:
        cap = max(120, int(cfg.get("bili_browse_sub_chars") or 400))
        subtxt = await _bili_subtitles(
            sess, aid, cid, str(headers.get("Cookie") or ""), cap,
            bvid=bvid, duration=float(d.get("duration") or 0),
        )
        if subtxt:
            out["sub"] = subtxt
    except Exception:  # noqa: BLE001
        pass
    # 评论：抓前 10 条给 AI 总结（展示层只放前 3 条）
    try:
        cmts = await _bili_reply_list(sess, aid, headers, 10)
        if len(cmts) < 10:
            wbim = await _bili_wbi_mixin(sess, headers)
            for c in await _bili_hot_comments(sess, aid, headers, wbim, 10):
                if c and c not in cmts:
                    cmts.append(c)
        if cmts:
            out["comments"] = cmts[:10]
    except Exception:  # noqa: BLE001
        pass
    return out


async def _bili_dwell(cfg: dict, items: list[dict]) -> int:
    """每条按视频时长 1/3 停留，模拟真人看完再划走，降低风控概率。返回实际停留秒数。"""
    try:
        ratio = float(cfg.get("bili_dwell_ratio") or 0.34)
    except (TypeError, ValueError):
        ratio = 0.34
    per_max = max(1, int(cfg.get("bili_dwell_max") or 180))
    total_max = max(1, int(cfg.get("bili_dwell_total_max") or 240))
    spent = 0
    for it in items:
        try:
            dur = float(it.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        sec = int(round(dur * ratio)) if dur else 10
        sec = min(per_max, max(3, sec))
        sec = min(sec, total_max - spent)
        if sec <= 0:
            break
        await asyncio.sleep(sec)
        spent += sec
    return spent


async def bili_like(cfg: dict | None = None, bvid: str = "", aid: int = 0) -> tuple[bool, str]:
    """给自己喜欢的视频点个赞（要登录态里的 bili_jct 当 csrf）。"""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    bvid = str(bvid or "").strip()
    if not bvid and not aid:
        return False, "没有 bvid，点不了赞"
    cookie = str(cfg.get("bili_cookie") or "").strip()
    m = re.search(r"bili_jct=([^;]+)", cookie)
    csrf = m.group(1).strip() if m else ""
    if not csrf:
        return False, "没登录（cookie 里没有 bili_jct），点不了赞"
    headers = _bili_headers(cfg, f"https://www.bilibili.com/video/{bvid}")
    try:
        async with aiohttp.ClientSession(
            headers=HEADERS, trust_env=bool(cfg.get("trust_env"))
        ) as sess:
            # 点赞接口要 aid，只给了 bvid 就先查一次详情
            if not aid:
                async with sess.get(
                    _BILI_VIEW, params={"bvid": bvid}, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        vp = await r.json(content_type=None)
                        if isinstance(vp, dict) and vp.get("code") == 0:
                            try:
                                aid = int((vp.get("data") or {}).get("aid") or 0)
                            except (TypeError, ValueError):
                                aid = 0
            form = {"like": 1, "csrf": csrf}
            if aid:
                form["aid"] = int(aid)
            else:
                form["bvid"] = bvid
            async with sess.post(
                _BILI_LIKE, data=form, headers=headers,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                if r.status != 200:
                    return False, f"点赞接口 HTTP {r.status}"
                p = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return False, "点赞请求没通"
    if not isinstance(p, dict):
        return False, "点赞接口回了个看不懂的东西"
    code = p.get("code")
    if code == 0:
        return True, "已点赞"
    msg = str(p.get("message") or "")
    if code == 65006 or "已经" in msg:
        return True, "之前就点过赞了"
    return False, f"点赞失败（{code} {msg}）"


async def bili_browse(
    cfg: dict | None = None,
    count: int = 0,
    keyword: str = "",
    pool_path: str = "",
    with_items: bool = False,
    tags: list[str] | None = None,
) -> str | tuple[str, list[dict]]:
    """从标签抓 B站视频入片单池，再按条数返回还没刷过的条目。

    tags 传入多个偏好标签时轮流抓、按这些标签筛片单池；
    keyword 优先于 tags。with_items=True 时额外返回挑中的条目。
    """
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    cap = max(1, int(cfg.get("bili_browse_max") or 10))
    try:
        n = int(count or 0)
    except (TypeError, ValueError):
        n = 0
    n = max(1, min(cap, n or int(cfg.get("bili_browse_default") or 5)))

    kw = str(keyword or "").strip()
    pref = [str(t).strip() for t in (tags or []) if str(t).strip()]
    use_tags = (
        [kw] if kw
        else pref
        or [str(t).strip() for t in (cfg.get("bili_tags") or []) if str(t).strip()]
        or ["热门"]
    )
    want = set(use_tags)

    pool = bili_pool_load(pool_path)

    def unserved(items: list[dict]) -> list[dict]:
        return [
            it for it in items
            if not int(it.get("served") or 0)
            and not int(it.get("invalid") or 0)
            and str(it.get("tag")) in want
        ]

    if len(unserved(pool)) < n:
        fresh: list[dict] = []
        # 优先真浏览器：搜索页 + 逐个打开视频页停留，避免接口被 -412 拦
        if cfg.get("bili_browser", True):
            fresh = await _bili_browser_fetch(cfg, use_tags, max(10, n * 4))
            if fresh:
                logger.info(f"[web_lookup] 浏览器刷到 {len(fresh)} 条（标签：{'、'.join(use_tags)}）")
            else:
                logger.info("[web_lookup] 浏览器没抓到内容，回退接口")
        if not fresh:
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=20),
                    headers=HEADERS,
                    trust_env=bool(cfg.get("trust_env")),
                ) as sess:
                    got = await asyncio.gather(
                        *(_bili_search_tag(sess, t, cfg, max(10, n * 4)) for t in use_tags),
                        return_exceptions=True,
                    )
                    batches = [g for g in got if isinstance(g, list) and g]
                    if not batches:
                        batches = [await _bili_popular(sess, cfg, max(10, n * 2))]
                    # 各标签轮流出片，避免一个标签霸屏
                    idx = 0
                    while any(idx < len(b) for b in batches):
                        for b in batches:
                            if idx < len(b):
                                fresh.append(b[idx])
                        idx += 1
            except Exception:  # noqa: BLE001
                fresh = []
        # 新抓的一律入库（放最前，当最新的），同 bvid 去重；已刷/已判无效的状态要继承，
        # 否则热搜榜稳定，每次重抓都会让同一条视频"复活"再发一遍
        old_map = {str(it.get("bvid")): it for it in pool}
        for it in fresh:
            prev = old_map.get(str(it.get("bvid")))
            if prev:
                it["served"] = prev.get("served", 0)
                it["invalid"] = prev.get("invalid", 0)
        have = {str(it.get("bvid")) for it in fresh}
        pool = fresh + [it for it in pool if str(it.get("bvid")) not in have]

    rest = unserved(pool)
    if not rest:
        if pool:
            return f"「{'、'.join(use_tags)}」这批已经刷完了。换个标签，或者说「刷b站 热门」再刷。"
        return (
            "B站这边没刷到内容：搜索接口被拦（-412）或片单池已空。"
            "先确认网络能访问 api.bilibili.com，或过一会儿再刷一次。"
        )
    # 补简介/字幕后逐条筛：没有内容可总结的判无效，继续往后取，直到凑够条数
    picked: list[dict] = []
    step = max(n * 2, 6)
    while rest and len(picked) < n:
        batch, rest = rest[:step], rest[step:]
        # 浏览器模式已经在视频页拿过简介，只对缺字段的补一次接口，省请求也少触发风控
        lack = [
            it for it in batch
            if not str(it.get("desc") or "").strip()
            or not str(it.get("author") or "").strip()
        ]
        try:
            if lack:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=20),
                    headers=HEADERS,
                    trust_env=bool(cfg.get("trust_env")),
                ) as sess:
                    metas = await asyncio.gather(
                        *(_bili_meta(sess, cfg, str(it.get("bvid") or "")) for it in lack),
                        return_exceptions=True,
                    )
                for it, meta in zip(lack, metas):
                    if isinstance(meta, dict) and meta:
                        it.update(meta)
        except Exception:  # noqa: BLE001
            pass
        for it in batch:
            # 先做本地标签语义匹配；低分不直接丢，留给 AI 判断
            it["match"] = round(
                _label_match_score(it, kw or str(it.get("tag") or "")), 3
            )
            if not _bili_has_content(it):
                it["invalid"] = 1
            elif len(picked) < n:
                it["served"] = 1
                picked.append(it)

    pool = bili_pool_save(pool_path, pool, int(cfg.get("bili_pool_max") or 60))
    if not picked:
        return (
            "这批视频都没有字幕、简介也几乎是空的，总结不出内容，全判无效了。"
            "想让刷出来的都有内容：跟我说一声我就弹二维码，扫码后能抓到 AI/CC 字幕；或者换个标签再刷。"
        )

    left = sum(
        1 for it in pool if not int(it.get("served") or 0) and not int(it.get("invalid") or 0)
    )
    # 风控：每条至少停留视频时长的 1/3（有上限），模拟真人在看
    spent = await _bili_dwell(cfg, picked)
    head = f"【刷B站】本次 {len(picked)} 条（标签：{'、'.join(use_tags)}）"
    body = "\n".join(_bili_line(i + 1, it) for i, it in enumerate(picked))
    tail = f"（片单池还剩 {left} 条未刷）"
    if spent:
        tail += f"\n（每条按 1/3 时长停留，本次共停留 {spent} 秒）"
    text = f"{head}\n{body}\n{tail}"
    return (text, picked) if with_items else text


def bili_pool_text(
    cfg: dict | None = None, n: int = 10, keyword: str = "", pool_path: str = ""
) -> str:
    """看片单池：最近入库的 N 条（可按标签过滤）。"""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    try:
        n = max(1, min(50, int(n or 10)))
    except (TypeError, ValueError):
        n = 10
    kw = str(keyword or "").strip()
    pool = bili_pool_load(pool_path)
    rows = [it for it in pool if not kw or str(it.get("tag")) == kw][:n]
    if not rows:
        return "片单池还是空的，先让我刷几条进来。"
    total = len(pool)
    body = "\n".join(_bili_line(i + 1, it) for i, it in enumerate(rows))
    return f"【B站片单】共 {total} 条，先看最近 {len(rows)} 条：\n{body}"
