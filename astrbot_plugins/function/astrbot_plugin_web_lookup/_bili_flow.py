"""B站刷视频工具体：清单总结、常驻页面投递、扫码登录流程。

从 main.py 拆出来的方法体：第一个参数固定传插件实例（self），
保持同名导出，main.py 的门面方法直接转发。
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

from ._ai_lines import _clean_ai_line
from .store import (
    _bili_pool_path,
    _bili_qr_path,
    _bili_qr_url,
    _load_feed_page,
    _load_video_prefs,
    _merge_feed_page,
    _write_feed_page,
)


async def bili_browse_tool(
    self, event: AstrMessageEvent, count: int = 0, keyword: str = ""
):
    kw = str(keyword or "").strip()
    prefs = _load_video_prefs()
    tags = [kw] if kw else (prefs["keywords"] or None)
    try:
        result = await asyncio.wait_for(
            self.core.bili_browse(
                self.cfg, count=count, keyword=kw, pool_path=_bili_pool_path(),
                with_items=True, tags=tags,
            ),
            timeout=420,
        )
    except asyncio.TimeoutError:
        return "B站刷视频超时了（接口太慢），过一会儿再试。"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 刷B站失败: {type(e).__name__}: {str(e)[:120]}")
        return f"刷B站失败（{type(e).__name__}），先跟用户说没刷出来。"
    text, picked = result if isinstance(result, tuple) else (result, [])
    if not str(text).startswith("【刷B站】"):
        return text
    use_tags = (
        [kw] if kw
        else prefs["keywords"]
        or [str(t) for t in (self.cfg.get("bili_tags") or []) if str(t).strip()]
        or ["热门"]
    )
    lines = await _deliver_feed(self, event, text, picked, use_tags)
    # 清单由代码直接发出；交给模型复述时它常把链接省掉，用户就只剩一堆标题
    try:
        await event.send(MessageChain([Plain(lines)]))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 直发清单失败: {type(e).__name__}: {str(e)[:120]}")
        return (
            lines
            + "\n（上面这份原样整段发出去，别改写、别省略链接、别自己补话。）"
        )
    return "带直链的清单已经直接发给用户了。你只需顺着说一句自己的感受，不要重复清单、不要补链接。"


async def bili_like_tool(self, event: AstrMessageEvent, bvid: str, why: str = ""):
    try:
        ok, msg = await asyncio.wait_for(
            self.core.bili_like(self.cfg, bvid=bvid), timeout=20
        )
    except asyncio.TimeoutError:
        return "点赞超时了，没点上。"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 点赞失败: {type(e).__name__}: {str(e)[:120]}")
        return f"点赞失败（{type(e).__name__}）。"
    return f"{msg}{'（' + why + '）' if why else ''}"


async def _bili_tldr(
    self, event: AstrMessageEvent, picked: list[dict] | None = None
) -> tuple[dict, str]:
    """让模型给每条写「视频总结 / 评论总结」，返回 ({序号: {...}}, 想法)。

    省 token 的做法：
    - 只发标题/UP/简介/字幕节选/前 5 条评论，各自截断（不再重复发全量文本）；
    - 已经有总结的条目直接跳过；
    - 一次最多总结 bili_summary_items 条，剩下的页面里只显示抓到的简介。
    """
    picked = picked or []
    cap = max(1, int(self.cfg.get("bili_summary_items") or 6))

    def clip(s, n: int) -> str:
        return re.sub(r"\s+", " ", str(s or "")).strip()[:n]

    rows: list[str] = []
    for i, it in enumerate(picked[:cap], 1):
        if it.get("sum"):
            continue
        bits = [f"{i}. {clip(it.get('title'), 60)}"]
        if it.get("author"):
            bits.append("UP：" + clip(it.get("author"), 20))
        if it.get("desc"):
            bits.append("简介：" + clip(it.get("desc"), 120))
        if it.get("sub"):
            bits.append("字幕：" + clip(it.get("sub"), 220))
        cmts = [clip(c, 40) for c in (it.get("comments") or []) if str(c).strip()][:5]
        if cmts:
            bits.append("评论：" + "；".join(cmts))
        rows.append("｜".join(bits))
    if not rows:
        return {}, ""
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    try:
        prov = self.context.get_using_provider(umo)
        # 模型就算挂着（连不上、排队），也不能把整条清单拖死
        resp = await asyncio.wait_for(
            prov.text_chat(
                prompt=(
                    "下面是刚刷到的一批 B站视频（标题/UP/简介/字幕节选/评论，均已截断）。\n"
                    "只输出一个 JSON 对象，不要解释、不要 markdown：\n"
                    '{"items":[{"i":1,"video":"这条讲什么、好玩或有用在哪（30字内）",'
                    '"comment":"评论区态度或关注点（20字内）"}],'
                    '"thought":"以你自己（肥鱼）的口吻写1-2句真实想法"}\n'
                    "i 是条目序号；只依据给到的信息，信息不足写「信息太少」，绝不编造。\n\n"
                    + "\n".join(rows)
                ),
                system_prompt="你只输出合法 JSON，不要输出别的任何东西。",
            ),
            timeout=25,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 刷B站总结失败: {type(e).__name__}: {str(e)[:120]}")
        return {}, ""
    raw = (getattr(resp, "completion_text", "") or "").strip()
    if not raw:
        return {}, ""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.M).strip()
    obj = None
    try:
        obj = json.loads(raw)
    except ValueError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                obj = json.loads(m.group(0))
            except ValueError:
                obj = None
    if not isinstance(obj, dict):
        return {}, ""
    out: dict[int, dict] = {}
    rows = obj.get("items")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i") or row.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if i <= 0:
            continue
        v = str(row.get("video") or "").strip()[:80]
        c = str(row.get("comment") or "").strip()[:60]
        if v or c:
            out[i] = {"video": v, "comment": c}
    return out, str(obj.get("thought") or "").strip()[:200]


async def _deliver_feed(
    self, event: AstrMessageEvent, text: str, picked: list[dict], tags: list[str]
) -> str:
    """把刷到的条目并进唯一视频页面，QQ 里只回一句介绍 + 页面链接。

    页面会一直累积（最多 200 条），不再把清单和封面图刷到群里。
    """
    if not picked:
        return text
    # 页面里已经总结过的同一条，直接复用，不再花一次模型
    cached: dict[str, dict] = {}
    for old in (_load_feed_page().get("items") or []):
        u = str(old.get("url") or "").strip()
        if u and old.get("sum"):
            cached[u] = old["sum"]
    for it in picked:
        u = str(it.get("url") or "").strip()
        if not it.get("sum") and u in cached:
            it["sum"] = cached[u]
    sums, thought = await _bili_tldr(self, event, picked)
    rows: list[dict] = []
    for i, it in enumerate(picked, 1):
        row = dict(it)
        s = sums.get(i) or it.get("sum") or {}
        if s:
            row["sum"] = s
        rows.append(row)

    url = ""
    try:
        page = _merge_feed_page(rows, tags)
        html = self.core.bili_page_html(
            page["items"],
            page["tags"],
            thought=thought,
            note="页面按发布时间倒序累积，最多保留最近 200 条；想换口味直接跟我说个词。",
            generated=page["updated"],
        )
        url = _write_feed_page(html, str(self.cfg.get("video_page_url") or ""))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 刷B站页面生成失败: {type(e).__name__}: {str(e)[:120]}")

    label = "、".join(str(t) for t in tags if str(t).strip()) or "默认"
    intro = _clean_ai_line(thought) or _clean_ai_line(str(picked[0].get("title") or ""))
    lines = [f"刷到 {len(picked)} 条（{label}）"]
    if intro:
        lines.append(intro)
    if url:
        lines.append(f"完整页面：{url}")
    return "\n".join(lines)


async def _bili_qr_flow(self, event: AstrMessageEvent):
    """发二维码并等到扫码成功：成功写 cookie，失败给一句提示。"""
    state = await self.core.bili_login_status(self.cfg)
    ticket = await self.core.bili_qr_generate()
    if not ticket:
        yield event.plain_result("二维码拿不到，稍后再试。")
        return
    try:
        png = await asyncio.to_thread(
            self.core.bili_qr_png, ticket["url"], _bili_qr_path()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[web_lookup] 画二维码失败: {type(e).__name__}: {e}")
        yield event.plain_result("二维码画不出来，SESSDATA 手填 config.json。")
        return
    url = _bili_qr_url(png)
    yield event.image_result(url)
    yield event.plain_result(f"B站 {state}。二维码已附在下面。")
    deadline = time.time() + 120
    announced = False
    while time.time() < deadline:
        await asyncio.sleep(2)
        result, cookie = await self.core.bili_qr_poll(ticket["key"])
        if result == "ok":
            if not cookie:
                yield event.plain_result("没抠到 SESSDATA，你再刷一次我重发二维码。")
                return
            self._save_cfg({"bili_cookie": cookie})
            yield event.plain_result(
                f"登录成功：{await self.core.bili_login_status(self.cfg)}"
            )
            return
        if result == "expired":
            yield event.plain_result("二维码过期了，你再刷一次我重发。")
            return
        if result == "scanned" and not announced:
            announced = True
            yield event.plain_result("扫上了，在手机上点一下确认。")
    yield event.plain_result("等太久，这次不登。")
