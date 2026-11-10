"""抓取、跳转壳识别、限流重试与并发抓取。"""
from __future__ import annotations

import asyncio
import random
import re
from html import unescape

import aiohttp
import yarl

from .extract import html_to_text
from .models import _cite_url, _host

_JUMP_REFERER = {
    "www.so.com": "https://www.so.com/",
    "www.sogou.com": "https://www.sogou.com/",
    "www.baidu.com": "https://www.baidu.com/",
}


_JS_REDIRECT = re.compile(
    r'(?:window\.|document\.)?location(?:\.href\s*=|\.replace\s*\(\s*)["\']([^"\']+)["\']'
)


_META_REFRESH = re.compile(
    r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]*content=["\']?\s*\d+\s*;\s*url=([^"\'>]+)',
    re.I,
)


def _redirect_target(raw: bytes) -> str:
    """从「跳转壳」里解出目标 URL；不是壳就返回空串。"""
    if len(raw) > 4000:
        return ""
    s = raw.decode("utf-8", "ignore")
    m = _JS_REDIRECT.search(s) or _META_REFRESH.search(s)
    if not m:
        return ""
    t = unescape(m.group(1)).strip().strip("\"'")
    return t if t.startswith(("http://", "https://")) else ""


def _retry_wait(headers, depth: int) -> float:
    """429/5xx 的等待时间：优先服务端 Retry-After，否则指数退避 + 抖动。"""
    ra = (headers.get("Retry-After") or "").strip()
    if ra.isdigit():
        return min(6.0, float(ra))
    return min(4.0, 1.0 + depth + random.uniform(0, 0.8))


async def _fetch_one(sess: aiohttp.ClientSession, url: str, cfg: dict, depth: int = 0):
    """③ 校验后抓正文。返回 (payload, 失败原因)；payload=(host,title,text,url) 或 None。"""
    cap = int(cfg["page_cap_kb"]) * 1024
    try:
        # 关键：encoded=True。跳转链的 m=/url= 参数是 URL 编码过的 base64，
        # 让 aiohttp 重新规范化会把 %2F 解成 /，服务端直接判链接无效（400/空壳）。
        target = yarl.URL(url, encoded=True)
        hdrs = {}
        ref = _JUMP_REFERER.get(_host(url))
        if ref:
            hdrs["Referer"] = ref
        async with sess.get(target, headers=hdrs or None, max_redirects=6) as r:
            # ---- 状态码校验：400/403/404/429/5xx 全部拦掉并记原因 ----
            if r.status != 200:
                # 429/5xx 是可恢复的：按 Retry-After 或指数退避等一会儿再试一次。
                # 直接放弃会把「只是被限流/服务端抖一下」的页面当成死链丢掉。
                if depth < 2 and (r.status == 429 or 500 <= r.status < 600):
                    await asyncio.sleep(_retry_wait(r.headers, depth))
                    return await _fetch_one(sess, url, cfg, depth + 1)
                return None, f"HTTP {r.status}"
            ct = (r.headers.get("content-type") or "").lower()
            if ct and not any(k in ct for k in ("html", "xml", "text/plain")):
                return None, "非文本"  # 图片/PDF/二进制拦掉
            # ---- 流模式：边读边截，超限即停 ----
            chunks: list[bytes] = []
            size = 0
            async for chunk in r.content.iter_chunked(65536):
                size += len(chunk)
                if size > cap:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            # 跳转壳（200 + 小页面）必须先于「空响应」判定：360/搜狗的跳转壳
            # 只有 200~400 字节的 <script>location.replace(...)</script>，
            # 先做长度检查会把它当空响应丢掉（这正是之前「老爬不到内容」的主因）。
            if depth < 2:
                nxt = _redirect_target(raw)
                if nxt:
                    return await _fetch_one(sess, nxt, cfg, depth + 1)
            if len(raw) < 300:
                return None, "空响应"
            enc = "utf-8"
            m = re.search(rb'charset=["\']?([\w-]+)', raw[:2000])
            if m:
                try:
                    enc = m.group(1).decode("ascii")
                except Exception:
                    pass
            html = raw.decode(enc, "ignore") if enc.lower() != "utf-8" else raw.decode("utf-8", "ignore")
            title, text = html_to_text(html)
            if len(text) < 80:
                return None, "正文过短"
            # 第四个字段是可直接引用的完整链接（302 跟到真页后去掉统计参数），
            # 前三个字段保持「域名+路径」形态，打分逻辑照旧
            real_url = _cite_url(str(r.url))
            return (r.url.host + str(r.url.path)[:40], title or url, text, real_url), ""
    except asyncio.TimeoutError:
        return None, "超时"
    except (aiohttp.ClientError, OSError):
        return None, "连接失败"
    except Exception:
        return None, "解析失败"  # 任何异常都视作校验失败，丢弃


_RETRY_REASONS = ("连接失败", "超时", "空响应")


async def _fetch_all(sess: aiohttp.ClientSession, urls: list[str], cfg: dict):
    """④ 并发抓取，返回 (可用页列表, 失败原因统计)。"""
    sem = asyncio.Semaphore(int(cfg["concurrency"]))

    async def run(u: str):
        async with sem:
            return await _fetch_one(sess, u, cfg)

    results: list = list(await asyncio.gather(*(run(u) for u in urls)))
    # 已经抓到足够多的正文就不重试（重试会把超时页再等一轮，白拖时间）
    got = sum(1 for payload, _ in results if payload)
    retry = [
        u for u, (payload, reason) in zip(urls, results)
        if not payload and reason in _RETRY_REASONS
    ] if got < 3 else []
    if retry:
        again = await asyncio.gather(*(run(u) for u in retry))
        fixed = dict(zip(retry, again))
        results = [
            fixed.get(u, res) if not res[0] else res
            for u, res in zip(urls, results)
        ]
    pages = []
    reasons: dict[str, int] = {}
    for payload, reason in results:
        if payload:
            pages.append(payload)
        elif reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    return pages, reasons
