"""联网查证工具：搜索 -> 校验 -> 流式抓取 -> 相似度挑选，一个工具全包。

核心逻辑在 lookup.py（纯函数，可独立测试）；本文件只是 llm_tool 壳。
设计原则：工具在管道侧把"搜索、验链、抓正文、挑最相关"全干完，
模型只拿到一份浓缩好的最相关正文——不让模型自己翻一堆搜索结果。

门面层：AstrBot 入口类、llm_tool/命令注册与转发都在这里；重活拆在
_msgparse（消息解析）、_ai_lines（AI 去重）、_platform_guard（平台限速）、
_bili_flow（B站刷视频）、_video_flow（看视频/刷视频）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from . import _bili_flow, _video_flow
from ._msgparse import _VIDEO_HOST_RE, _video_quote_ok
from .store import _bili_pool_path

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_NAME = "astrbot_plugin_web_lookup"


def _load_lookup_module():
    path = os.path.join(PLUGIN_DIR, "lookup.py")
    spec = importlib.util.spec_from_file_location("web_lookup_core", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@register(
    "astrbot_plugin_web_lookup",
    "dafeiyu-local",
    "联网查证：多引擎聚合搜索→登录墙过滤→流式抓正文→相似度挑最相关页面（免 API key）",
    "v1.5.1",
)
class WebLookupPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.core = _load_lookup_module()
        self.cfg = dict(self.core.DEFAULT_CFG)
        try:
            with open(os.path.join(PLUGIN_DIR, "config.json"), encoding="utf-8-sig") as f:
                self.cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        logger.info(
            "[web_lookup] 已启动：四引擎聚合 + 先筛后抓 + 状态码/失效页清洗，并发=%s 单页上限=%sKB 正文截取=%s字",
            self.cfg.get("concurrency"), self.cfg.get("page_cap_kb"), self.cfg.get("max_chars"),
        )

    @filter.llm_tool(name="web_lookup")
    async def web_lookup(self, event: AstrMessageEvent, query: str):
        """搜索互联网，返回与问题最相关的一个网页正文，用于获取你不确定的、或可能已过时的实时信息。

        满足以下任一条件，回答前就应先调用本工具：
        - 答案会随时间变化：新闻、比赛结果、价格、版本号、天气、汇率、榜单、
          政策、发售/上线/停服时间、最近发生的事件
        - 你不确定、没听说过、或训练数据可能已过时的内容
        - 用户明确要求联网、搜索、"查一下"、"最新的"

        不要使用：日常闲聊、稳定不变的历史常识和定义、你非常确定且不会过时的事实。

        query 构造：把用户的问题改写成 2~6 个关键词的搜索短语，去掉口语保留专有名词，
        需要时效时可带年份；「英雄联盟最近打啥比赛了」应写成「英雄联盟 全球总决赛 赛程」。
        一次查不到可以换关键词再调一次（最多两次）；仍查不到就明说查不到，
        绝不编造查到的内容。用户发来链接让你读/总结时，把链接原样填进 query；
        视频链接（B站/抖音/快手等）只有在消息 @ 了你时才允许解析（直接发卡片、
        引用那条消息再 @、私聊直接发都算），群里没人点名的裸链接会拒绝。

        返回结果带「来源：<完整链接>」和「其他候选」链接。只要你的回答用到了返回内容，
        就必须把用到的「来源」链接原样附在回答末尾（用不到的不要凑数）。

        B站/抖音/快手/西瓜视频链接会返回标题/作者/分区/发布时间/播放点赞弹幕/标签/简介，
        并尽量附上「字幕原文」「音频转写」或「前10条评论」用于概括内容
        （视频链接仍需满足上面的「@ 你」条件）。
        用户要你总结/讲视频内容时优先依据这些内容；一条都没拿到时只能基于
        标题/标签/简介作答，并说明看不到正文，不要凭空编造画面细节。

        Args:
            query(string): 搜索关键词短语，或一个 http/https 网页链接
        """
        q = (query or "").strip()
        if not q:
            return "请给出要查的关键词或链接。"
        try:
            if q.startswith(("http://", "https://")):
                if _VIDEO_HOST_RE.search(q) and not _video_quote_ok(event):
                    return "视频链接不自动解析：直接分享卡片或引用那条消息，再 @ 我一下，我才看。"
                # 视频链接可能要走下载 + CPU 转写，给足整体超时
                timeout = max(60, int(self.cfg.get("video_parse_timeout_s", 180)) + 20)
                result = await asyncio.wait_for(
                    self.core.fetch_url(q, self.cfg), timeout=timeout
                )
            else:
                result = await asyncio.wait_for(
                    self.core.lookup(q, self.cfg), timeout=90
                )
            logger.info("[web_lookup] %s -> %s", q[:60], result[:60].replace("\n", " "))
            return result
        except asyncio.TimeoutError:
            return "联网查询超时了（网络或目标站太慢），先别引用这条。"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[web_lookup] 查询失败: {type(e).__name__}: {str(e)[:120]}")
            return f"联网查询失败（{type(e).__name__}），先按你已知的回答并说明没查到。"

    # ---------------- B站刷视频 ----------------

    @filter.llm_tool(name="bili_browse")
    async def bili_browse_tool(
        self, event: AstrMessageEvent, count: int = 0, keyword: str = ""
    ):
        """给用户刷 B站视频：用真浏览器打开搜索页和视频页（有停留，防风控），抓一批视频，
        整理成一个网页，并返回一份「每条都带直链」的清单。

        返回的文本已经把排版和链接都排好了：原样整段发给用户，别改写、别总结、
        别删链接、别自己补话；用户要的是能直接点开的链接。

        当用户说"刷点B站""来点视频""刷几个搞笑视频""看看pvz"时使用。
        用户没要求条数就留 0（用默认条数）；只说了一个标签就填 keyword。
        真浏览器打开和停留会慢一点，别催。

        Args:
            count(int): 要刷几条，默认 5，最多 10；用户没指定就留 0
            keyword(string): 想刷的单个标签/关键词；留空则按之前存下的偏好标签刷
        """
        return await _bili_flow.bili_browse_tool(self, event, count, keyword)

    @filter.llm_tool(name="bili_pool")
    async def bili_pool_tool(
        self, event: AstrMessageEvent, n: int = 0, keyword: str = ""
    ):
        """查看已经存起来的 B站片单（最近入库的视频，含刷没刷过的都列）。

        当用户问"片单里有啥""B站池子里有啥""之前刷的视频"时使用。

        Args:
            n(int): 看最近几条，默认 10，最多 50；用户没指定就留 0
            keyword(string): 只看某个标签；留空看全部
        """
        return self.core.bili_pool_text(
            self.cfg, n=n, keyword=keyword, pool_path=_bili_pool_path()
        )

    @filter.llm_tool(name="bili_like")
    async def bili_like_tool(self, event: AstrMessageEvent, bvid: str, why: str = ""):
        """给一条 B站视频点赞。只有你自己真觉得好看/有意思才点，别为了客气乱点。

        当你说"这条有点意思""给这个点个赞"时使用。

        Args:
            bvid(string): 视频 BV 号，形如 BV1xx411c7mD，从链接里取
            why(string): 为什么想点赞，一句话
        """
        return await _bili_flow.bili_like_tool(self, event, bvid, why)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def watch_video_quick(self, event: AstrMessageEvent):
        """快捷触发：不带 / 直接发「看视频 关键词 平台」"""
        text = (event.message_str or "").strip()
        if not re.match(r"^看(?:一下)?视频[ \t]+\S", text):
            return
        event.stop_event()
        async for chunk in self.watch_video_cmd(event, ""):
            yield chunk

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def watch_video_auto(self, event: AstrMessageEvent):
        """直接发视频卡片/链接：自动解析，输出和 /看视频 一样（群里要 @ 我，私聊随意）。"""
        async for chunk in _video_flow.watch_video_auto(self, event):
            yield chunk

    @filter.command("视频", alias={"刷视频"})
    async def video_cmd(self, event: AstrMessageEvent):
        """/视频 [b站|快手|抖音|西瓜] [关键词] [条数]｜/视频 add 关键词｜/视频 remove｜/视频 clear（也认 /刷视频）"""
        async for chunk in _video_flow.video_cmd(self, event):
            yield chunk

    @filter.command("看视频")
    async def watch_video_cmd(
        self, event: AstrMessageEvent, platform: str = ""
    ):
        """/看视频 [关键词] [b站|快手|抖音|西瓜] —— 引用链接时解析该视频；只给关键词则按内容搜视频并总结"""
        async for chunk in _video_flow.watch_video_cmd(self, event, platform):
            yield chunk

    def _save_cfg(self, updates: dict) -> None:
        """把改动写回插件自己的 config.json，并热更新内存配置（不用重载插件）。"""
        self.cfg.update(updates)
        try:
            with open(
                os.path.join(PLUGIN_DIR, "config.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except (OSError, TypeError) as e:  # noqa: BLE001
            logger.warning(f"[web_lookup] 写 config.json 失败: {type(e).__name__}: {e}")
