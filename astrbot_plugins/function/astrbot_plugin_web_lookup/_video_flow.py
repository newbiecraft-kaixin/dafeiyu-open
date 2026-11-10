"""看视频/刷视频命令体：快捷触发、自动解析、/视频、/看视频、站内搜索与总结报告。

从 main.py 拆出来的方法体：第一个参数固定传插件实例（self），
保持同名导出，main.py 的门面方法直接转发。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ._bili_flow import _bili_qr_flow, _deliver_feed
from ._msgparse import (
    _URL_RE,
    _at_self,
    _reply_text,
    _video_link,
    _video_quote_ok,
)
from ._platform_guard import _PLATFORM_LAST, _PLATFORM_MIN_GAP, _platform_lock
from .store import (
    _bili_pool_path,
    _bili_qr_path,
    _bili_qr_url,
    _load_video_prefs,
    _load_watch,
    _merge_feed_page,
    _save_video_prefs,
    _save_watch,
    _split_keywords,
    _write_feed_page,
)

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_login_qr():
    """按文件路径加载扫码模块，避免依赖插件包的相对导入。"""
    try:
        path = os.path.join(PLUGIN_DIR, "login_qr.py")
        spec = importlib.util.spec_from_file_location("web_lookup_login_qr", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 扫码模块加载失败: {type(e).__name__}: {e}")
        return None


def _video_read_delay(data: dict, cfg: dict, elapsed: float) -> float:
    """她要真的"看一会儿"：按视频时长的 1/3 再发解析结果。

    已花掉的解析时间算在内，避免"解析慢 + 等待"叠加成 2/3；
    没有时长（图文/解析失败）就不额外等，免得无意义地卡着。
    """
    ratio = float(cfg.get("video_read_ratio") or 0.34)
    low = max(0, int(cfg.get("video_read_min_s") or 6))
    high = max(low, int(cfg.get("video_read_max_s") or 300))
    dur = float(data.get("duration") or 0)
    if not dur:
        return 0.0
    target = min(high, max(low, dur * ratio))
    return max(0.0, target - max(0.0, elapsed))


async def watch_video_auto(self, event: AstrMessageEvent):
    text = (event.message_str or "").strip()
    if re.match(r"^[\s/！!]*看视频", text, flags=re.I):
        return
    if not _video_quote_ok(event):
        return
    link = _video_link(event)
    if not link:
        return
    try:
        vp = self.core._video_parse()
    except Exception:  # noqa: BLE001
        return
    if vp is None or not vp.detect_platform(link):
        return
    event.stop_event()
    t0 = time.monotonic()
    try:
        data = await asyncio.wait_for(
            vp.parse_video(link, self.cfg),
            timeout=int(self.cfg.get("video_parse_timeout_s", 180)) + 30,
        )
    except asyncio.TimeoutError:
        yield event.plain_result("解析超时。")
        return
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[web_lookup] 自动解析视频失败: {type(e).__name__}: {str(e)[:120]}"
        )
        yield event.plain_result(f"解析失败（{type(e).__name__}）。")
        return
    if not (data.get("title") or data.get("author") or data.get("comments")):
        yield event.plain_result("没解析出内容。")
        return
    # 解析是秒级的，但"人"不会秒回：等够视频时长 1/3 再开口
    wait_s = _video_read_delay(data, self.cfg, time.monotonic() - t0)
    if wait_s > 0:
        logger.info(
            "[web_lookup] 视频 %.0f 秒，按 1/3 再看 %.0f 秒后回复",
            float(data.get("duration") or 0), wait_s,
        )
        await asyncio.sleep(wait_s)
    yield event.plain_result(await _video_report(self, event, data))
    for u in (data.get("image_urls") or [])[:3]:
        yield event.image_result(str(u))


async def video_cmd(self, event: AstrMessageEvent):
    logger.info(
        f"[web_lookup] /视频 触发: raw={str(event.message_str)[:60]!r} "
        f"platform={event.get_platform_name()}")
    if str(event.get_platform_name() or "").lower() in (
            "wechatauto", "weixin_oc"):
        yield event.plain_result("微信端发不了图片/视频，/视频 只在 QQ 用。")
        return
    raw = re.sub(
        r"^[\s/！!]*刷?视频", "", (event.message_str or "").strip(), flags=re.I
    ).strip()
    raw = re.sub(r"@\S+", "", raw).strip()
    aliases = {
        "b站": "bilibili", "bilibili": "bilibili", "bili": "bilibili",
        "抖音": "douyin", "douyin": "douyin",
        "快手": "kuaishou", "kuaishou": "kuaishou",
        "西瓜": "ixigua", "ixigua": "ixigua",
    }
    names = {"bilibili": "B站", "douyin": "抖音",
             "kuaishou": "快手", "ixigua": "西瓜"}
    tokens = raw.split()
    platform = ""
    if tokens and tokens[0].lower() in aliases:
        platform = aliases[tokens.pop(0).lower()]
    count = 0
    words: list[str] = []
    for t in tokens:
        if t.isdigit() and count <= 0:
            count = int(t)
        else:
            words.append(t)
    op = words[0].lower() if words else ""
    rest = " ".join(words[1:]).strip() if words else ""
    prefs = _load_video_prefs()

    if op == "add":
        words = _split_keywords(rest)
        if not words:
            yield event.plain_result("加什么？比如 /视频 add 搞笑")
            return
        for w in words:
            if w not in prefs["keywords"] and len(prefs["keywords"]) < 10:
                prefs["keywords"].append(w)
        _save_video_prefs(prefs)
        yield event.plain_result(
            f"加好了：{'、'.join(prefs['keywords'])}（刷视频就专找这些）"
        )
        return
    if op == "clear":
        _save_video_prefs({"keywords": []})
        yield event.plain_result("清空了，之后按默认标签刷。")
        return
    if op == "remove":
        if not prefs["keywords"]:
            yield event.plain_result("现在是空的，没得删。")
            return
        words = _split_keywords(rest)
        if words:
            gone = [w for w in words if w in prefs["keywords"]]
            prefs["keywords"] = [w for w in prefs["keywords"] if w not in gone]
            _save_video_prefs(prefs)
            if gone:
                yield event.plain_result(
                    f"删掉了：{'、'.join(gone)}。现在还剩："
                    f"{'、'.join(prefs['keywords']) or '空'}"
                )
            else:
                yield event.plain_result(
                    f"没有这些关键词：{'、'.join(words)}。现在是："
                    f"{'、'.join(prefs['keywords']) or '空'}"
                )
            return
        gone = prefs["keywords"].pop()
        _save_video_prefs(prefs)
        yield event.plain_result(
            f"删掉了最后一个：{gone}。现在还剩：{'、'.join(prefs['keywords']) or '空'}"
        )
        return
    # 指定了别的平台：这些站没有匿名搜索接口，只能搜出公开的条目链接
    if platform and platform != "bilibili":
        from urllib.parse import quote
        kw = " ".join(words).strip() or (
            prefs["keywords"][0] if prefs["keywords"] else "热门")
        pat = re.compile({
            "douyin": r"douyin\.com/video/\d+",
            "kuaishou": r"kuaishou\.com/(?:short-video|f)/[\w-]+",
            "ixigua": r"ixigua\.com/(?:video/)?\d+",
        }[platform])
        yield event.plain_result(f"去{names[platform]}搜「{kw}」，稍等…")
        import aiohttp
        hits: list = []
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60)) as sess:
                hits = await self.core._search(
                    sess, f"{names[platform]} {kw} 视频", self.cfg)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[web_lookup] 刷{names[platform]}失败: "
                f"{type(e).__name__}: {str(e)[:120]}")
        picked_links: list[tuple[str, str]] = []
        for h in hits:
            u = str(h[0])
            if pat.search(u) and all(u != x[0] for x in picked_links):
                picked_links.append((u, str(h[1])[:40]))
            if len(picked_links) >= (count or 5):
                break
        if picked_links:
            body = "\n".join(
                f"{i}. {t}\n{u}" for i, (u, t) in enumerate(picked_links, 1))
            yield event.plain_result(
                f"【{names[platform]}｜{kw}】{len(picked_links)} 条\n{body}")
            return
        search_link = {
            "douyin": "https://www.douyin.com/search/{}",
            "kuaishou": "https://www.kuaishou.com/search/video?searchKey={}",
            "ixigua": "https://www.ixigua.com/search/{}/",
        }[platform].format(quote(kw))
        yield event.plain_result(
            f"{names[platform]}这次没搜到能看的条目（它不开放匿名搜索）。\n"
            f"自己搜：{search_link}\n"
            f"把链接发我，/看视频 我解析这一条。")
        return

    # 无参数 / 只给条数：按偏好直接刷（B站未登录也能匿名刷，登录只影响字幕抓取）
    kw = " ".join(words).strip()
    use_tags = ([kw] if kw else prefs["keywords"] or [
        str(t) for t in (self.cfg.get("bili_tags") or []) if str(t).strip()
    ] or ["热门"])
    picked: list[dict] = []
    # 先回一句，别让人干等（B站接口偶尔要十几秒）
    yield event.plain_result(f"刷B站「{'、'.join(use_tags)}」中…")
    try:
        result = await asyncio.wait_for(
            self.core.bili_browse(
                self.cfg, count=count, keyword=kw,
                pool_path=_bili_pool_path(),
                with_items=True, tags=use_tags,
            ),
            timeout=90,
        )
        text, picked = result if isinstance(result, tuple) else (result, [])
    except asyncio.TimeoutError:
        text = "B站刷视频超时了（接口太慢，或停留时间太长），过一会儿再试。"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 刷视频失败: {type(e).__name__}: {str(e)[:120]}")
        text = f"刷视频失败（{type(e).__name__}），没刷出来。"
    if str(text).startswith("【刷B站】"):
        try:
            text = await _deliver_feed(self, event, text, picked, use_tags)
        except Exception as e:  # noqa: BLE001
            # 排版失败也要把带直链的原始清单发出去
            logger.warning(f"[web_lookup] 清单排版失败: {type(e).__name__}: {str(e)[:120]}")
        yield event.plain_result(text)
        return
    yield event.plain_result(text)
    # 匿名刷不出来时才引导扫码登录（登录后能抓 AI/CC 字幕，筛选更准）
    if (
        "没刷到内容" in str(text)
        and not str(self.cfg.get("bili_cookie") or "").strip()
    ):
        async for chunk in _bili_qr_flow(self, event):
            yield chunk


async def watch_video_cmd(
    self, event: AstrMessageEvent, platform: str = ""
):
    raw_all = re.sub(
        r"^[\s/！!]*看视频", "", (event.message_str or "").strip(), flags=re.I
    )
    raw_all = re.sub(r"@\S+", "", raw_all).strip()
    aliases = {
        "b站": "bilibili", "bilibili": "bilibili", "bili": "bilibili",
        "抖音": "douyin", "douyin": "douyin",
        "快手": "kuaishou", "kuaishou": "kuaishou",
        "西瓜": "ixigua", "ixigua": "ixigua",
    }
    names = {"bilibili": "B站", "douyin": "抖音", "kuaishou": "快手", "ixigua": "西瓜"}
    w0 = raw_all.split()
    sub = w0[0] if w0 else ""
    if sub in ("新增", "添加", "移除", "删除", "清空", "列出", "列表"):
        rows = _load_watch()
        if sub in ("新增", "添加"):
            p2 = ""
            keep: list[str] = []
            for w in w0[1:]:
                key = aliases.get(w.lower())
                if key and not p2:
                    p2 = key
                else:
                    keep.append(w)
            kw2 = " ".join(keep).strip()[:30]
            if not kw2:
                yield event.plain_result(
                    "用法：/看视频 新增 关键词 [b站|快手|抖音|西瓜]")
                return
            if any(r.get("kw") == kw2 and r.get("platform", "") == p2
                   for r in rows):
                yield event.plain_result(f"「{kw2}」已经在关注列表里了。")
                return
            rows.append({
                "kw": kw2,
                "platform": p2,
                "at": time.strftime("%Y-%m-%d %H:%M"),
            })
            _save_watch(rows)
            yield event.plain_result(
                f"已新增：{kw2}（{names.get(p2, '默认B站')}）")
            return
        if sub in ("移除", "删除"):
            p2 = ""
            keep: list[str] = []
            for w in w0[1:]:
                key = aliases.get(w.lower())
                if key and not p2:
                    p2 = key
                else:
                    keep.append(w)
            target = " ".join(keep).strip()
            if not target:
                yield event.plain_result("用法：/看视频 移除 关键词 [平台]")
                return
            kept = [
                r for r in rows
                if not (
                    target in str(r.get("kw", ""))
                    and (not p2 or str(r.get("platform") or "") == p2)
                )
            ]
            gone = len(rows) - len(kept)
            if not gone:
                yield event.plain_result(f"没找到包含「{target}」的关注项。")
                return
            _save_watch(kept)
            yield event.plain_result(
                f"已移除 {gone} 项（匹配「{target}」），剩 {len(kept)} 项。")
            return
        if sub == "清空":
            _save_watch([])
            yield event.plain_result("关注列表已清空。")
            return
        if not rows:
            yield event.plain_result(
                "关注列表是空的。发 /看视频 新增 关键词 [平台] 添加。")
            return
        out = [
            f"{i}. {r.get('kw')}（{names.get(r.get('platform') or 'bilibili', 'B站')}）"
            for i, r in enumerate(rows, 1)
        ]
        yield event.plain_result("关注列表：\n" + "\n".join(out))
        return
    url0 = _URL_RE.search(raw_all or "")
    if not url0:
        # 纯关键词模式：按内容去站内搜视频，不需要 @
        p = ""
        words = []
        for w in raw_all.split():
            key = aliases.get(w.lower())
            if key and not p:
                p = key
            else:
                words.append(w)
        kw = " ".join(words).strip()
        deep = dict(self.cfg)
        deep["max_chars"] = 6000
        if not kw and p:
            # 只给了平台（/看视频 b站）：用它自己的热门词搜，而不是打印用法
            kw = "热门"
        if not kw:
            rows = _load_watch()
            if not raw_all and rows:
                picked: list[dict] = []
                seen_platform: set[str] = set()
                for r in rows:
                    key = str(r.get("platform") or "bilibili")
                    if key in seen_platform:
                        continue
                    seen_platform.add(key)
                    picked.append(r)
                    if len(picked) >= 3:
                        break
                yield event.plain_result(
                    f"按关注列表跑 {len(picked)} 个平台，每个平台最多 1 个…")
                out = []
                for r in picked:
                    kw2 = str(r.get("kw") or "")
                    p2 = str(r.get("platform") or "")
                    blocks = await _video_search_texts(
                        self, event, kw2, p2, deep, 1)
                    out.append(
                        f"【{kw2}｜{names.get(p2 or 'bilibili', 'B站')}】\n"
                        + "\n".join(blocks))
                yield event.plain_result("\n\n".join(out))
                return
            yield event.plain_result(
                "用法：/看视频 关键词 [b站|快手|抖音|西瓜]；"
                "或引用视频消息 @我 解析单条链接。")
            return
        yield event.plain_result(
            f"去{names.get(p or 'bilibili', 'B站')}搜「{kw}」，稍等…")
        out = await _video_search_texts(self, event, kw, p, deep, 2)
        yield event.plain_result("\n\n".join(out))
        return
    quoted = _reply_text(event)
    if not quoted or not (_at_self(event) or event.is_private_chat()):
        yield event.plain_result("要引用视频那条消息并 @ 我（私聊直接引用就行）。")
        return
    raw_p = (platform or "").strip()
    if raw_p.startswith("@") or raw_p.lower().startswith(("http://", "https://")):
        raw_p = ""
    m = _URL_RE.search(quoted)
    lk = m.group(0).rstrip(".,;:!?)]}><") if m else ""
    if not lk:
        yield event.plain_result("引用的消息里没链接。")
        return
    aliases = {
        "b站": "bilibili", "bilibili": "bilibili", "bili": "bilibili",
        "抖音": "douyin", "douyin": "douyin",
        "快手": "kuaishou", "kuaishou": "kuaishou",
        "西瓜": "ixigua", "ixigua": "ixigua",
    }
    p = aliases.get(raw_p.lower(), raw_p.lower())
    if not p:
        low = lk.lower()
        if "bilibili.com" in low or "b23.tv" in low:
            p = "bilibili"
        elif "douyin.com" in low:
            p = "douyin"
        elif "kuaishou.com" in low:
            p = "kuaishou"
        elif "ixigua.com" in low:
            p = "ixigua"
    names = {"bilibili": "B站", "douyin": "抖音", "kuaishou": "快手", "ixigua": "西瓜"}
    if p == "bilibili":
        state = await self.core.bili_login_status(self.cfg)
        if not state.startswith("已登录"):
            async for chunk in _bili_qr_flow(self, event):
                yield chunk
            if not str(self.cfg.get("bili_cookie") or "").strip():
                return
    elif p in names:
        key = f"{p}_cookie"
        if not str(self.cfg.get(key) or "").strip():
            login_qr = _load_login_qr()
            if login_qr is None or not login_qr.supported(p):
                note = login_qr.unavailable_reason(p) if login_qr else ""
                yield event.plain_result(f"{names[p]}没登录。{note}填 Cookie 到 {key}。")
            else:
                yield event.plain_result(f"{names[p]}没登录，二维码如下（2 分钟内有效）。")
                qr = await login_qr.start_qr(p)
                if not qr:
                    yield event.plain_result(f"{names[p]}二维码没拿到，填 Cookie 到 {key}。")
                    return
                png, sid = qr
                tmp_dir = os.path.dirname(_bili_qr_path()) or "."
                path = os.path.join(tmp_dir, f"qr_{sid}.png")
                with open(path, "wb") as f:
                    f.write(png)
                url = _bili_qr_url(path)
                yield event.image_result(url)
                yield event.plain_result("扫码登录，二维码已附在下面。")
                cookie = await login_qr.wait_login(sid, timeout=150)
                try:
                    os.remove(path)
                except OSError:
                    pass
                if not cookie:
                    yield event.plain_result("扫码超时了，重新发一次我再出码。")
                    return
                self._save_cfg({key: cookie})
                yield event.plain_result(f"{names[p]}已登录。")
    vp = None
    try:
        vp = self.core._video_parse()
    except Exception:  # noqa: BLE001
        vp = None
    if vp is not None and vp.detect_platform(lk):
        t0 = time.monotonic()
        yield event.plain_result("开始解析这条视频，稍等…")
        try:
            data = await asyncio.wait_for(
                vp.parse_video(lk, self.cfg),
                timeout=int(self.cfg.get("video_parse_timeout_s", 180)) + 30,
            )
        except asyncio.TimeoutError:
            yield event.plain_result("解析超时。")
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[web_lookup] 看视频失败: {type(e).__name__}: {str(e)[:120]}")
            yield event.plain_result(f"解析失败（{type(e).__name__}）。")
            return
        if not (data.get("title") or data.get("author") or data.get("comments")):
            yield event.plain_result("没解析出内容。")
            return
        # 同上：按视频时长 1/3 的节奏再回复
        wait_s = _video_read_delay(data, self.cfg, time.monotonic() - t0)
        if wait_s > 0:
            await asyncio.sleep(wait_s)
        report = await _video_report(self, event, data)
        summary = comment_sum = ""
        for line in report.splitlines():
            line = line.strip()
            if line.startswith("视频总结：") and not summary:
                summary = line[len("视频总结："):].strip()
            elif line.startswith("评论总结：") and not comment_sum:
                comment_sum = line[len("评论总结："):].strip()
        row = {
            "title": str(data.get("title") or "").strip(),
            "url": lk,
            "author": str(data.get("author") or "").strip(),
            "duration": int(data.get("duration") or 0),
            "play": data.get("play"),
            "desc": str(data.get("desc") or "").strip(),
            "sub": str(data.get("subtitle") or data.get("transcript") or "").strip(),
            "comments": [str(c) for c in (data.get("comments") or [])][:3],
            "sum": {"video": summary, "comment": comment_sum},
        }
        url = ""
        try:
            page = _merge_feed_page([row], [])
            html = self.core.bili_page_html(
                page["items"],
                page["tags"],
                note="页面按发布时间倒序累积，最多保留最近 200 条。",
                generated=page["updated"],
            )
            url = _write_feed_page(html, str(self.cfg.get("video_page_url") or ""))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[web_lookup] 看视频页面生成失败: {type(e).__name__}: {str(e)[:120]}")
        head = row["title"] or "这条视频"
        if row["author"]:
            head = f"{head}｜{row['author']}"
        lines = [head]
        if summary:
            lines.append(summary)
        if url:
            lines.append(f"完整页面：{url}")
        yield event.plain_result("\n".join(lines))
        return
    try:
        text = await asyncio.wait_for(
            self.core.fetch_url(lk, self.cfg),
            timeout=int(self.cfg.get("video_parse_timeout_s", 180)) + 30,
        )
    except asyncio.TimeoutError:
        yield event.plain_result("解析超时。")
        return
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 看视频失败: {type(e).__name__}: {str(e)[:120]}")
        yield event.plain_result(f"解析失败（{type(e).__name__}）。")
        return
    yield event.plain_result(text[:4000])


async def _video_search_texts(
    self, event: AstrMessageEvent, kw: str, p: str, deep: dict, limit: int = 2
) -> list[str]:
    """同一平台串行执行，且两次请求间隔至少 60 秒。"""
    key = p or "bilibili"
    async with _platform_lock(key):
        wait = _PLATFORM_MIN_GAP - (time.monotonic() - _PLATFORM_LAST.get(key, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await _video_search_impl(self, event, kw, p, deep, limit)
        finally:
            _PLATFORM_LAST[key] = time.monotonic()


async def _video_search_impl(
    self, event: AstrMessageEvent, kw: str, p: str, deep: dict, limit: int = 2
) -> list[str]:
    """站内按关键词搜视频，取前 limit 条逐个解析并总结。"""
    try:
        vp = self.core._video_parse()
    except Exception:  # noqa: BLE001
        vp = None
    if p in ("", "bilibili"):
        try:
            got = await asyncio.wait_for(
                self.core.bili_browse(
                    self.cfg, count=max(limit, 3), keyword=kw,
                    pool_path=_bili_pool_path(), with_items=True,
                    tags=[kw],
                ),
                timeout=300,
            )
            _, items = got if isinstance(got, tuple) else (got, [])
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[web_lookup] 搜 B站 失败: {type(e).__name__}: {str(e)[:120]}")
            return [f"搜 B站 失败（{type(e).__name__}）。"]
        urls = [
            f"https://www.bilibili.com/video/{str(it.get('bvid') or '').strip()}"
            for it in items if str(it.get("bvid") or "").strip()
        ]
        if not urls:
            return [f"「{kw}」没搜到可总结的视频。"]
        out: list[str] = []
        for i, u in enumerate(urls[:limit], 1):
            data = {}
            if vp is not None and vp.detect_platform(u):
                try:
                    data = await asyncio.wait_for(
                        vp.parse_video(u, deep), timeout=300)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[web_lookup] 搜到的视频解析失败: {type(e).__name__}")
            if data:
                out.append(f"[{i}] " + await _video_report(self, event, data))
        return out or ["搜到了但没解析出内容（可能需要登录抓字幕）。"]

    host = {"kuaishou": "kuaishou.com", "douyin": "douyin.com",
            "ixigua": "ixigua.com"}.get(p, "")
    if not host:
        return [f"不认识的平台：{p}（支持 b站/快手/抖音/西瓜）。"]
    import aiohttp
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)) as sess:
            hits = await self.core._search(sess, f"site:{host} {kw}", self.cfg)
    except Exception as e:  # noqa: BLE001
        return [f"搜索失败（{type(e).__name__}）。"]
    links = [str(h[0]) for h in hits if host in str(h[0])]
    if not links:
        return [f"没搜到相关视频，直接把链接发我 @ 一下也行。"]
    out = []
    for i, u in enumerate(links[:limit], 1):
        try:
            text = await asyncio.wait_for(
                self.core.fetch_url(u, deep), timeout=300)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[web_lookup] 搜到的视频抓取失败: {type(e).__name__}")
            continue
        out.append(f"[{i}] {u}\n{str(text)[:3000]}")
    return out or ["搜到了但没解析出内容。"]


async def _video_report(self, event: AstrMessageEvent, data: dict) -> str:
    """固定结构：标题/时长/作者 + 视频总结 + 评论总结 + 前 3 条评论。"""
    title = str(data.get("title") or "").strip() or "没拿到"
    author = str(data.get("author") or "").strip() or "没拿到"
    sec = int(data.get("duration") or 0)
    dur = f"{sec // 60:02d}:{sec % 60:02d}" if sec else "没拿到"
    cmts = [str(c).strip() for c in (data.get("comments") or []) if str(c).strip()]
    desc = str(data.get("desc") or "").strip()
    sub = str(data.get("subtitle") or data.get("transcript") or "").strip()
    tags = "、".join(str(t) for t in (data.get("tags") or [])[:8])
    material = (
        f"标题：{title}\n作者：{author}\n简介：{desc[:400]}\n标签：{tags}\n"
        f"字幕/转写：{sub[:8000] or '（没拿到）'}\n评论：\n"
        + "\n".join(f"{i}. {c[:150]}" for i, c in enumerate(cmts[:10], 1))
    )
    prompt = (
        "根据下面抓到的完整视频内容写总结，格式严格四行：\n"
        "视频总结：<80字以内，覆盖开头、中段、结论>\n"
        "要点：<3 条，每条不超过 25 字，用「1)…2)…3)…」>\n"
        "评论总结：<30字以内说清评论区态度或关注点>\n"
        "只依据给到的信息，别编造画面细节；信息不足就写「信息太少」。\n\n"
        + material
    )
    vsum = csum = points = ""
    try:
        prov = self.context.get_using_provider(event.unified_msg_origin)
        if prov:
            resp = await prov.text_chat(
                prompt=prompt,
                system_prompt="你只输出四行总结，不解释、不列点、不加引号。",
            )
            text = str(getattr(resp, "completion_text", "") or "")
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("视频总结：") and not vsum:
                    vsum = line[len("视频总结："):].strip()
                elif line.startswith("要点：") and not points:
                    points = line[len("要点："):].strip()
                elif line.startswith("评论总结：") and not csum:
                    csum = line[len("评论总结："):].strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 视频总结失败: {type(e).__name__}: {str(e)[:80]}")
    if not vsum:
        vsum = desc[:120] or "信息太少，讲不清内容。"
    if not csum:
        csum = cmts[0][:80] if cmts else "没抓到评论。"

    url = str(data.get("url") or "").strip()
    lines = [
        f"标题：{title}｜时长：{dur}｜作者：{author}",
    ]
    if url:
        lines.append(f"链接：{url}")
    lines.append(f"视频总结：{vsum}")
    if points:
        lines.append(f"要点：{points}")
    lines.append(f"评论总结：{csum}")
    for i, c in enumerate(cmts[:3], 1):
        lines.append(f"评论{i}：{c[:150]}")
    return "\n".join(lines)
