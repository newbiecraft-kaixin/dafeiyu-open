"""视频/分享链接识别：抓 <title>、B站接口、QQ 分享卡片、裸链接后台解析。"""

import asyncio
import json
import os
import re
import time

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.core.message.components import At, Plain, Reply
try:  # QQ 链接分享卡片（部分 NapCat 版本才有）
    from astrbot.core.message.components import Json as _JsonComp
except ImportError:  # pragma: no cover
    _JsonComp = None

from .utils import (
    BILI_HOST_RE,
    PLUGIN_DIR,
    URL_RE,
    VIDEO_HOST_RE,
    _clean_url,
    _strip_site_suffix,
)
from .video_analysis import timestamp

_VIDEO_PARSE = None


def _video_parse_module():
    """加载兄弟插件 web_lookup 的 video_parse.py。

    固定 sys.modules 名，保证和 web_lookup 用的是同一模块实例、同一把平台锁。
    """
    global _VIDEO_PARSE
    if _VIDEO_PARSE is not None:
        return _VIDEO_PARSE
    import importlib.util
    import sys

    key = "fy_video_parse"
    mod = sys.modules.get(key)
    if mod is None:
        path = os.path.join(
            os.path.dirname(PLUGIN_DIR), "astrbot_plugin_web_lookup", "video_parse.py"
        )
        spec = importlib.util.spec_from_file_location(key, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    _VIDEO_PARSE = mod
    return mod


class LinkMixin:
    async def _fetch_link_title(self, url: str) -> str | None:
        """抓视频/分享链接的页面 <title>（b23.tv 短链会跟重定向），失败返回 None。"""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12), trust_env=False
            ) as sess:
                async with sess.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                    },
                    allow_redirects=True,
                    max_redirects=6,
                ) as resp:
                    if resp.status != 200:
                        return None
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.content.iter_chunked(8192):
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > 512_000:
                            break
            html = b"".join(chunks).decode("utf-8", "ignore")
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
            if not m:
                return None
            title = _strip_site_suffix(re.sub(r"\s+", " ", m.group(1)).strip())
            return title[:60] or None
        except Exception as e:  # noqa: BLE001
            logger.info(f"[face_convert] 链接标题抓取失败: {str(e)[:80]}")
            return None

    async def _fetch_bili_summary(self, url: str) -> str | None:
        """B站视频/短链 → 走官方 view 接口，拿标题/UP主/时长/播放/简介给 AI。

        b23.tv 短链先跟重定向拿到真实地址，再按 BV 号或 av 号查；拿不到返回 None，
        由调用方退回抓 <title> 的老路。
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                headers=headers,
                trust_env=False,
            ) as sess:
                real = url
                if re.search(r"(b23\.tv|bili2233\.cn)", url, re.I):
                    async with sess.get(url, allow_redirects=True, max_redirects=6) as resp:
                        real = str(resp.url or url)
                m = re.search(r"(BV[0-9A-Za-z]{10})", real) or re.search(
                    r"/video/(av\d+)", real, re.I
                )
                if not m:
                    return None
                g = m.group(1)
                params = {"bvid": g} if g[:2].lower() == "bv" else {"aid": g[2:]}
                async with sess.get(
                    "https://api.bilibili.com/x/web-interface/view", params=params
                ) as resp:
                    if resp.status != 200:
                        return None
                    payload = await resp.json(content_type=None)
            if not isinstance(payload, dict) or payload.get("code") != 0:
                logger.info(
                    f"[face_convert] B站接口返回异常: code={payload.get('code') if isinstance(payload, dict) else '?'}"
                )
                return None
            d = payload.get("data") or {}
            title = str(d.get("title") or "").strip()
            if not title:
                return None
            facts: list[str] = []
            owner = str((d.get("owner") or {}).get("name") or "").strip()
            if owner:
                facts.append(f"UP主：{owner}")
            try:
                dur = float(d.get("duration") or 0)
                if dur:
                    facts.append(f"时长 {timestamp(dur)}")
            except (TypeError, ValueError):
                pass
            if d.get("tname"):
                facts.append(f"分区 {d['tname']}")
            stat = d.get("stat") or {}
            if stat.get("view"):
                facts.append(f"播放 {stat['view']}")
            if stat.get("like"):
                facts.append(f"点赞 {stat['like']}")
            try:
                if d.get("pubdate"):
                    facts.append(
                        "发布 "
                        + time.strftime("%Y-%m-%d", time.localtime(float(d["pubdate"])))
                    )
            except (TypeError, ValueError, OSError):
                pass
            bvid = str(d.get("bvid") or "")
            if bvid:
                link = f"https://www.bilibili.com/video/{bvid}"
            elif d.get("aid"):
                link = f"https://www.bilibili.com/video/av{d['aid']}"
            else:
                link = real
            text = f"[对方分享了一个B站视频：《{title[:60]}》｜链接 {link}"
            if facts:
                text += "｜" + "｜".join(facts)
            text += "]"
            desc = re.sub(r"\s+", " ", str(d.get("desc") or "")).strip()
            if desc and desc not in ("-", "—", "无", "暂无简介"):
                text += f"\n简介：{desc[:240]}"
            pages = d.get("pages") or []
            if isinstance(pages, list) and len(pages) > 1:
                parts = [
                    f"{p.get('page')}. {str(p.get('part') or '')[:18]}"
                    for p in pages[:4]
                    if isinstance(p, dict)
                ]
                if parts:
                    text += "；分P：" + "、".join(parts)
            return text[:900]
        except Exception as e:  # noqa: BLE001
            logger.info(f"[face_convert] B站视频信息抓取失败: {str(e)[:80]}")
            return None

    @staticmethod
    def _card_payload(comp) -> dict:
        """把卡片组件的 payload 统一成 dict。

        AstrBot 的 Json 组件在构造时就已经把字符串 json.loads 成 dict 了，这里
        必须两种形态都认；有些客户端还会再套一层 {"data": "<json 字符串>"}。
        """
        raw = getattr(comp, "data", None)
        if raw is None:
            raw = getattr(comp, "text", None)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return {}
        if not isinstance(raw, dict):
            return {}
        for _ in range(2):
            inner = raw.get("data")
            if isinstance(inner, str) and inner.lstrip().startswith("{"):
                try:
                    parsed = json.loads(inner)
                except ValueError:
                    break
                if isinstance(parsed, dict):
                    raw = parsed
                    continue
            break
        return raw

    @classmethod
    def _card_info(cls, comp) -> tuple[str, str, str]:
        """QQ 分享卡片 -> (标题, 描述, 链接)；解析不出来返回三个空串。"""
        j = cls._card_payload(comp)
        if not j:
            return "", "", ""
        meta = j.get("meta") if isinstance(j.get("meta"), dict) else {}
        news = meta.get("news") if isinstance(meta.get("news"), dict) else {}
        detail = meta.get("detail_1") if isinstance(meta.get("detail_1"), dict) else {}
        title = str(
            j.get("title") or news.get("title") or detail.get("title") or ""
        ).strip()
        desc = str(
            j.get("desc") or news.get("desc") or detail.get("desc") or ""
        ).strip()
        if not (title or desc):
            title = str(j.get("prompt") or "").strip()
        jump = str(
            news.get("jump_url")
            or news.get("jumpUrl")
            or j.get("jump_url")
            or j.get("jumpUrl")
            or detail.get("qqdocurl")
            or detail.get("jumpUrl")
            or detail.get("url")
            or ""
        ).strip()
        if not jump:
            # 不同客户端的字段名不一样，实在找不到就在整段卡片里捞视频域名
            for u in URL_RE.findall(json.dumps(j, ensure_ascii=False)):
                if VIDEO_HOST_RE.search(u):
                    jump = _clean_url(u)
                    break
        return title, desc, _clean_url(jump)

    @staticmethod
    def _cards_in(comps) -> list:
        """收集卡片组件本身，以及「引用一条卡片消息」时藏在 Reply.chain 里的卡片。"""
        cards = []
        for c in comps:
            if _JsonComp is not None and isinstance(c, _JsonComp):
                cards.append(c)
            elif isinstance(c, Reply):
                for rc in (c.chain or []):
                    if _JsonComp is not None and isinstance(rc, _JsonComp):
                        cards.append(rc)
        return cards

    def _addressed_to_bot(self, event: AstrMessageEvent, comps) -> bool:
        """私聊，或群里 @ 了机器人：这条消息是专门发给她看的。"""
        if not event.get_group_id():
            return True
        sid = str(event.get_self_id() or "")
        if not sid:
            return False
        return any(
            isinstance(c, At) and str(getattr(c, "qq", "")) == sid for c in comps
        )

    async def _collect_video_links(self, event: AstrMessageEvent, comps) -> list[str]:
        """识别消息里的视频链接（裸 URL / QQ 分享卡片 Json），把链接内容喂给 AI。

        B站走官方接口拿标题/UP主/简介；其它站点退回抓 <title>，抓不到就用卡片自带标题。
        整条消息就是一条视频链接时，只有 @ 了她/私聊才把链接给她；没人点名的裸链接
        只当普通分享，不自动解析，也不吞消息。
        """
        bare = self._bare_video_link(event, comps)
        if bare:
            if self._addressed_to_bot(event, comps):
                # 直接甩一张卡片再 @ 她：把卡片里的真实链接写进 message_str，
                # 否则 Json 卡片对模型是空的，它只能回「你发个链接」。
                return [f"[对方分享了一个视频链接：{bare}]"]
            return []
        urls: list[str] = []
        card_titles: dict[str, str] = {}
        out: list[str] = []
        for u in URL_RE.findall(event.message_str or ""):
            urls.append(_clean_url(u))
        # QQ 分享卡片是 Json 组件：title/desc/meta.news.jump_url 里藏着链接和标题
        for c in self._cards_in(comps):
            title, desc, url = self._card_info(c)
            if url and VIDEO_HOST_RE.search(url):
                for u in URL_RE.findall(url):
                    u = _clean_url(u)
                    if u not in urls:
                        urls.append(u)
                    if title:
                        card_titles.setdefault(u, title[:60])
                continue
            if title or desc or url:
                # 非视频卡片（文章 / 小程序 / 音乐等）：标题+描述+链接直接给 AI
                bits = [t for t in (title, desc) if t]
                text = "[对方发来一张卡片：" + " / ".join(bits)[:180]
                if url:
                    text += (" " if bits else "") + f"链接：{url}"
                out.append(text + "]")
                logger.info(f"[face_convert] 卡片识别: {title or '-'} {url}")
        for u in dict.fromkeys(urls):
            if not VIDEO_HOST_RE.search(u):
                continue
            if BILI_HOST_RE.search(u):
                note = await self._fetch_bili_summary(u)
                if note:
                    out.append(note)
                    continue
            title = card_titles.get(u) or await self._fetch_link_title(u)
            out.append(
                f"[对方分享了一个视频链接：{'《' + title + '》' if title else ''}{u}]"
            )
        if not out and card_titles:
            # 只有卡片、链接没来得及抓的兜底：至少把标题给 AI
            out.append(f"[对方分享了一个视频链接：《{next(iter(card_titles.values()))}》]")
        return out

    def _bare_video_link(self, event: AstrMessageEvent, comps) -> str | None:
        """消息主体就是一条视频分享（链接/口令/QQ 卡片）时返回它的链接。"""
        raw = event.message_str or ""
        try:
            vp = _video_parse_module()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[face_convert] video_parse 加载失败: {type(e).__name__}: {e}")
            return None
        # 分享口令里偶尔会带两条同一作品的链接，不要求恰好一条，取第一条视频链接
        urls = [_clean_url(u) for u in URL_RE.findall(raw)]
        picked = next((u for u in urls if vp.detect_platform(u)), None)
        cards = self._cards_in(comps)
        if not picked and cards:
            # QQ 转发抖音/快手是 Json 卡片：链接藏在卡片里，取第一张视频卡片
            for c in cards:
                _, _, jump = self._card_info(c)
                if jump and vp.detect_platform(jump):
                    picked = jump
                    break
        if not picked:
            return None
        rest = URL_RE.sub("", raw)
        rest = re.sub(r"[\s，,。；;、:：!！?？·—…\"'“”‘’()（）\[\]【】<>-]", "", rest)
        if len(rest) > 20:
            if cards:
                # 卡片自带的标题/描述也会算进 message_str；只有还写了一大段附言
                # 才当成闲聊，否则就是专门分享给你看的
                if len(rest) > 100:
                    return None
            elif not re.search(
                r"(复制打开抖音|复制打开快手|看看【|的作品】|发布在抖音|抖音短视频|快手作品)",
                raw,
            ):
                return None
            elif len(rest) > 160:
                return None
        return picked

    def _video_cfg(self) -> dict:
        """合并 web_lookup 的配置（B站 cookie 等在里面），供 video_parse 用。"""
        cfg = dict(self.cfg)
        try:
            path = os.path.join(
                os.path.dirname(PLUGIN_DIR), "astrbot_plugin_web_lookup", "config.json"
            )
            with open(path, encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        return cfg

    def _start_video_link_task(self, event: AstrMessageEvent, url: str) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        task = asyncio.create_task(self._video_link_worker(umo, url))
        self._video_tasks.add(task)
        task.add_done_callback(self._video_tasks.discard)
        logger.info(f"[face_convert] 视频链接后台解析: {url[:80]}")

    async def _video_summary(self, umo: str, text: str) -> str:
        """抓完后只调一次 provider：总结内容 + 判断好不好看/值不值得看。"""
        try:
            getter = getattr(self.context, "get_using_provider_async", None)
            prov = await getter(umo) if getter else self.context.get_using_provider(umo)
            if prov is None:
                raise RuntimeError("没有可用模型")
            resp = await prov.text_chat(
                prompt=(
                    "下面是刚从视频/图文作品链接里抓到的资料。其中字幕/音频转写、"
                    "图片文字（OCR）和评论就是它的真实内容；"
                    "图文作品没有画面识别，不能描述图里的景物、人物长相或动作。\n"
                    "请用你自己的口吻给群友讲清楚：这个作品讲了什么、亮点或笑点在哪、"
                    "评论区在聊什么；最后明确判断「好不好看 / 值不值得看」，并给一句理由。\n"
                    "只依据材料，材料没写的内容不要编造画面或剧情；不要复述标题和播放量。\n\n"
                    + text
                ),
                system_prompt=(
                    "你是群聊里的肥鱼，说话口语化、简短，有一说一；"
                    "只依据给定材料作答，材料里没拿到的部分就直说没拿到。"
                ),
            )
            out = (getattr(resp, "completion_text", "") or "").strip()
            return out or text[:800]
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[face_convert] 视频总结失败: {type(e).__name__}: {str(e)[:120]}"
            )
            return "视频资料抓到了，但我这会儿总结不出来。要点：\n" + text[:800]

    async def _video_link_worker(self, umo: str, url: str) -> None:
        """后台：立刻回等待时长 -> 抓元信息/评论/字幕/转写 -> 一次总结 -> 发回同一会话。"""
        vp = _video_parse_module()
        cfg = self._video_cfg()
        sent_wait = False

        async def on_meta(data: dict) -> None:
            nonlocal sent_wait
            sent_wait = True
            try:
                mins = max(1, int(vp.estimate_minutes(data, cfg)))
            except Exception:  # noqa: BLE001
                mins = 1
            await self.context.send_message(
                umo,
                MessageChain(
                    chain=[Plain(f"请等待{mins}分钟，我在看这个作品，看完给你讲讲。")]
                ),
            )

        try:
            data = await vp.parse_video(url, cfg, on_meta=on_meta)
            if not sent_wait:
                await self.context.send_message(
                    umo,
                    MessageChain(
                        chain=[Plain("请等待1分钟，我在看这个作品，看完给你讲讲。")]
                    ),
                )
            if not (
                data.get("title")
                or data.get("subtitle")
                or data.get("transcript")
                or data.get("comments")
            ):
                reason = "；".join(data.get("notes") or []) or "没拿到视频信息"
                await self.context.send_message(
                    umo,
                    MessageChain(
                        chain=[Plain(f"这个视频没看全（原因：{reason[:80]}）")]
                    ),
                )
                return
            material = vp.render_text(data, cfg)
            # 材料里去掉「来源：链接」：模型容易原样贴回群里，总结不该带链接
            material = "\n".join(
                ln for ln in material.splitlines() if not ln.startswith("来源：")
            )
            summary = await self._video_summary(umo, material)
            await self.context.send_message(
                umo, MessageChain(chain=[Plain(summary)])
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[face_convert] 视频链接解析失败: {type(e).__name__}: {str(e)[:100]}"
            )
            try:
                await self.context.send_message(
                    umo,
                    MessageChain(
                        chain=[Plain(f"这个视频没看全（原因：{type(e).__name__}）")]
                    ),
                )
            except Exception:  # noqa: BLE001
                pass
