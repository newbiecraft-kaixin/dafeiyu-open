"""联网查证工具：搜索 -> 校验 -> 流式抓取 -> 相似度挑选，一个工具全包。

核心逻辑在 lookup.py（纯函数，可独立测试）；本文件只是 llm_tool 壳。
设计原则：工具在管道侧把"搜索、验链、抓正文、挑最相关"全干完，
模型只拿到一份浓缩好的最相关正文——不让模型自己翻一堆搜索结果。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import time
from difflib import SequenceMatcher

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_NAME = "astrbot_plugin_web_lookup"

# 最近说过的 AI 句子：太像的直接不说，专治「复读机」
_RECENT_AI_LINES: list[str] = []


def _clean_ai_line(s: str) -> str:
    """把 AI 回来的一行收拾成一句：去掉 markdown/引号/换行，限长。"""
    s = re.sub(r"```[\s\S]*?```", "", str(s or ""))
    s = re.sub(r"[*#`>]", "", s)
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip("\"'“”「」『』《》 ")
    return s[:80]


def _line_is_fresh(line: str) -> bool:
    """同一句或八成相似最近说过就不再重复。"""
    key = re.sub(r"\s+", "", line)
    if not key:
        return False
    if any(SequenceMatcher(None, key, old).ratio() > 0.62 for old in _RECENT_AI_LINES):
        return False
    _RECENT_AI_LINES.append(key)
    del _RECENT_AI_LINES[:-8]
    return True


def _bili_pool_path() -> str:
    """片单池固定落在 data/plugin_data/<插件名>/bili_pool.json。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        root = get_astrbot_plugin_data_path()
    except Exception:  # noqa: BLE001
        root = os.path.join(os.path.dirname(os.path.dirname(PLUGIN_DIR)), "plugin_data")
    return os.path.join(root, PLUGIN_NAME, "bili_pool.json")


def _bili_qr_path() -> str:
    """二维码落到 sticker 静态服务目录，这样能直接用 8712 的 URL 发出去。"""
    share = os.path.join(
        os.path.dirname(os.path.dirname(PLUGIN_DIR)), "stickers", "_share"
    )
    os.makedirs(share, exist_ok=True)
    for fn in os.listdir(share):
        if fn.startswith("bili_qr_"):
            try:
                os.remove(os.path.join(share, fn))
            except OSError:
                pass
    return os.path.join(share, f"bili_qr_{int(time.time())}.png")


def _bili_qr_url(path: str) -> str:
    """8712（tools_source_server）的静态地址：127.0.0.1:8712/<相对路径>。"""
    return f"http://127.0.0.1:8712/_share/{os.path.basename(path)}"


def _video_prefs_path() -> str:
    """刷视频偏好落到 data/plugin_data/<插件名>/video_prefs.json。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        root = get_astrbot_plugin_data_path()
    except Exception:  # noqa: BLE001
        root = os.path.join(os.path.dirname(os.path.dirname(PLUGIN_DIR)), "plugin_data")
    return os.path.join(root, PLUGIN_NAME, "video_prefs.json")


def _load_video_prefs() -> dict:
    """读偏好关键词（最多 10 个，去重）。"""
    try:
        with open(_video_prefs_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    rows = data.get("keywords") if isinstance(data, dict) else []
    out: list[str] = []
    for k in rows if isinstance(rows, list) else []:
        k = re.sub(r"\s+", " ", str(k or "")).strip()[:20]
        if k and k not in out:
            out.append(k)
    return {"keywords": out[:10]}


def _save_video_prefs(prefs: dict) -> None:
    path = _video_prefs_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"keywords": prefs.get("keywords") or []}, f, ensure_ascii=False, indent=2)


def _split_keywords(text: str) -> list[str]:
    """把「搞笑 pvz，沙雕」拆成关键词列表。"""
    rows = re.split(r"[\s,，、;；]+", str(text or "").strip())
    return [x.strip()[:20] for x in rows if x.strip()][:10]


def _watch_path() -> str:
    """看视频关注词落到 data/plugin_data/<插件名>/watchlist.json。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        root = get_astrbot_plugin_data_path()
    except Exception:  # noqa: BLE001
        root = os.path.join(os.path.dirname(os.path.dirname(PLUGIN_DIR)), "plugin_data")
    return os.path.join(root, PLUGIN_NAME, "watchlist.json")


def _load_watch() -> list[dict]:
    try:
        with open(_watch_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = []
    out: list[dict] = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        kw = re.sub(r"\s+", " ", str(row.get("kw") or "")).strip()[:30]
        if kw:
            out.append({
                "kw": kw,
                "platform": str(row.get("platform") or ""),
                "at": str(row.get("at") or ""),
            })
    return out[:50]


def _save_watch(rows: list[dict]) -> None:
    path = _watch_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


# 每个视频平台同时只跑一个任务，且两次请求至少间隔 60 秒，避免触发平台风控
_PLATFORM_LOCKS: dict[str, asyncio.Lock] = {}
_PLATFORM_LAST: dict[str, float] = {}
_PLATFORM_MIN_GAP = 60.0


def _platform_lock(key: str) -> asyncio.Lock:
    lock = _PLATFORM_LOCKS.get(key)
    if lock is None:
        lock = _PLATFORM_LOCKS[key] = asyncio.Lock()
    return lock



def _share_page_dir() -> str:
    share = os.path.join(
        os.path.dirname(os.path.dirname(PLUGIN_DIR)), "stickers", "_share"
    )
    os.makedirs(share, exist_ok=True)
    return share


def _write_feed_page(html_text: str) -> str:
    """所有刷到的视频只写进同一个常驻页面，返回可发的 URL。"""
    path = os.path.join(_share_page_dir(), "bili_feed.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text)
    return _bili_qr_url(path)


def _feed_page_path() -> str:
    """常驻页面的数据落盘：data/plugin_data/<插件名>/bili_page.json。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        root = get_astrbot_plugin_data_path()
    except Exception:  # noqa: BLE001
        root = os.path.join(os.path.dirname(os.path.dirname(PLUGIN_DIR)), "plugin_data")
    return os.path.join(root, PLUGIN_NAME, "bili_page.json")


_FEED_PAGE_LIMIT = 200


def _load_feed_page() -> dict:
    """读常驻页面数据（条目 + 标签 + 更新时间）。"""
    try:
        with open(_feed_page_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
    tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]
    return {
        "items": items[:_FEED_PAGE_LIMIT],
        "tags": tags[:10],
        "updated": str(data.get("updated") or ""),
    }


def _merge_feed_page(items: list[dict], tags: list[str] | None = None) -> dict:
    """把新条目并进常驻页面：按链接去重，新的在前，最多留 200 条。"""
    page = _load_feed_page()

    def key_of(it: dict) -> str:
        return str(it.get("url") or it.get("bvid") or it.get("title") or "").strip()

    seen = {key_of(it) for it in page["items"]}
    fresh: list[dict] = []
    for it in items:
        k = key_of(it)
        if not k or k in seen:
            continue
        seen.add(k)
        fresh.append(dict(it))
    page["items"] = (fresh + page["items"])[:_FEED_PAGE_LIMIT]

    merged: list[str] = []
    for t in list(tags or []) + page["tags"]:
        t = str(t or "").strip()
        if t and t not in merged:
            merged.append(t)
    page["tags"] = merged[:10]
    page["updated"] = time.strftime("%Y-%m-%d %H:%M")

    path = _feed_page_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(page, f, ensure_ascii=False, indent=2)
    return page


def _load_lookup_module():
    path = os.path.join(PLUGIN_DIR, "lookup.py")
    spec = importlib.util.spec_from_file_location("web_lookup_core", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_QUOTE_RE = re.compile(r"\[引用消息(?:\((.{0,800}?)\))?\]", re.S)
_URL_RE = re.compile(r"https?://[^\s\"'<>（）()\[\]【】「」，,。]+")
_VIDEO_HOST_RE = re.compile(
    r"(b23\.tv|bili2233\.cn|bilibili\.com|douyin\.com|iesdouyin\.com"
    r"|kuaishou\.com|ixigua\.com|v\.qq\.com|weishi\.qq\.com)",
    re.I,
)


def _card_text(comp) -> str:
    """QQ 分享卡片（Json / Share）里捞标题和链接，别只剩下一个空壳。"""
    raw = getattr(comp, "data", None)
    if raw is None:
        raw = getattr(comp, "text", None)
    if isinstance(raw, str):
        blob = raw
    else:
        try:
            blob = json.dumps(raw, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            blob = str(raw or "")
    # 卡片 JSON 里的斜杠和 & 是转义过的，先还原再找链接
    blob = str(blob).replace("\\/", "/").replace("\\u0026", "&")
    title = ""
    for key in ("title", "desc"):
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.){{1,80}})"', blob)
        if m and m.group(1).strip():
            title = m.group(1).strip()
            break
    urls = _URL_RE.findall(blob)
    url = next((u for u in urls if _VIDEO_HOST_RE.search(u)), urls[0] if urls else "")
    if url:
        url = url.rstrip("\\")
        return f"[卡片:{title + ' ' if title else ''}{url}]"
    return f"[卡片:{title}]" if title else "[卡片]"


def _reply_text(event: AstrMessageEvent) -> str:
    """被引用消息里的文字/链接；没有引用返回空串。"""
    for c in getattr(event.message_obj, "message", None) or []:
        cname = type(c).__name__
        if cname != "Reply" and getattr(c, "type", None) != "Reply":
            continue
        parts = [str(getattr(c, "message_str", "") or "")]
        for i in getattr(c, "chain", None) or []:
            text = str(getattr(i, "text", "") or "")
            if text:
                parts.append(text)
            iname = type(i).__name__
            if iname == "Share":
                parts.append(str(getattr(i, "url", "") or ""))
                parts.append(str(getattr(i, "content", "") or ""))
            elif iname == "Json":
                parts.append(_card_text(i))
        return "\n".join(p for p in parts if p)
    m = _QUOTE_RE.search(event.message_str or "")
    return (m.group(1) or "") if m else ""


def _at_self(event: AstrMessageEvent) -> bool:
    """当前消息里是否 @ 了机器人本体。"""
    self_id = str(event.get_self_id() or "")
    if not self_id:
        return False
    for c in getattr(event.message_obj, "message", None) or []:
        if getattr(c, "type", None) != "At" and type(c).__name__ != "At":
            continue
        if str(getattr(c, "qq", "") or "") == self_id:
            return True
    return False


def _video_in_message(event: AstrMessageEvent) -> bool:
    """当前这条消息本身是否带视频链接/卡片（不靠引用）。"""
    if _VIDEO_HOST_RE.search(event.message_str or ""):
        return True
    for c in getattr(event.message_obj, "message", None) or []:
        n = type(c).__name__
        if n == "Json" and _VIDEO_HOST_RE.search(_card_text(c)):
            return True
        if n == "Share" and _VIDEO_HOST_RE.search(str(getattr(c, "url", "") or "")):
            return True
        if n == "Plain" and _VIDEO_HOST_RE.search(str(getattr(c, "text", "") or "")):
            return True
    return False


def _video_quote_ok(event: AstrMessageEvent) -> bool:
    """视频链接必须 @ 我（私聊本身就是找我说），且链接在引用的消息或当前消息里。"""
    if not _at_self(event) and not event.is_private_chat():
        return False
    return bool(_VIDEO_HOST_RE.search(_reply_text(event)) or _video_in_message(event))


def _video_link(event: AstrMessageEvent) -> str:
    """引用消息、当前正文或卡片里的第一个视频链接。"""
    blobs = [_reply_text(event), event.message_str or ""]
    for c in getattr(event.message_obj, "message", None) or []:
        n = type(c).__name__
        if n == "Json":
            blobs.append(_card_text(c))
        elif n == "Share":
            blobs.append(str(getattr(c, "url", "") or ""))
        elif n == "Plain":
            blobs.append(str(getattr(c, "text", "") or ""))
    for blob in blobs:
        for u in _URL_RE.findall(blob or ""):
            if _VIDEO_HOST_RE.search(u):
                return u.rstrip(".,;:!?)]}>")
    return ""


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


@register(
    "astrbot_plugin_web_lookup",
    "dafeiyu-local",
    "联网查证：多引擎聚合搜索→登录墙过滤→流式抓正文→相似度挑最相关页面（免 API key）",
    "v1.5.0",
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
        lines = await self._deliver_feed(event, text, picked, use_tags)
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
        self, event: AstrMessageEvent, text: str, picked: list[dict] | None = None
    ) -> tuple[dict, str]:
        """让模型给每条写「视频总结 / 评论总结」，返回 ({序号: {...}}, 想法)。"""
        extra = ""
        if picked:
            rows: list[str] = []
            for i, it in enumerate(picked, 1):
                cmts = [str(c) for c in (it.get("comments") or []) if str(c).strip()][:10]
                rows.append(f"{i}. " + ("；".join(cmts) if cmts else "没评论"))
            if rows:
                extra = (
                    "\n\n【每条的前10条评论（只用于评论总结，不要整段复述给用户）】\n"
                    + "\n".join(rows)
                )
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            prov = self.context.get_using_provider(umo)
            # 模型就算挂着（连不上、排队），也不能把整条清单拖死
            resp = await asyncio.wait_for(
                prov.text_chat(
                    prompt=(
                        "下面是刚刷到的一批 B站视频，条目里带标题、简介、标签、字幕节选和前 10 条评论。\n"
                        "只输出一个 JSON 对象，不要解释、不要 markdown：\n"
                        '{"items":[{"i":1,"video":"这条讲什么、好玩或有用在哪（40字内）",'
                        '"comment":"评论区态度或关注点（30字内）"}],'
                        '"thought":"以你自己（肥鱼）的口吻写1-2句真实想法"}\n'
                        "i 是条目序号；只依据给到的信息，信息不足写「信息太少」，绝不编造。\n\n"
                        + text
                        + extra
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
        sums, thought = await self._bili_tldr(event, text, picked)
        rows: list[dict] = []
        for i, it in enumerate(picked, 1):
            row = dict(it)
            s = sums.get(i) or {}
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
                note="页面会一直累积，最多保留最近 200 条；想换口味直接跟我说个词。",
                generated=page["updated"],
            )
            url = _write_feed_page(html)
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
        yield event.plain_result(f"B站 {state}。扫码登录：{url}")
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
        yield event.plain_result(await self._video_report(event, data))
        for u in (data.get("image_urls") or [])[:3]:
            yield event.image_result(str(u))

    @filter.command("视频", alias={"刷视频"})
    async def video_cmd(self, event: AstrMessageEvent):
        """/视频 [b站|快手|抖音|西瓜] [关键词] [条数]｜/视频 add 关键词｜/视频 remove｜/视频 clear（也认 /刷视频）"""
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
                text = await self._deliver_feed(event, text, picked, use_tags)
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
            async for chunk in self._bili_qr_flow(event):
                yield chunk

    @filter.command("看视频")
    async def watch_video_cmd(
        self, event: AstrMessageEvent, platform: str = ""
    ):
        """/看视频 [关键词] [b站|快手|抖音|西瓜] —— 引用链接时解析该视频；只给关键词则按内容搜视频并总结"""
        if str(event.get_platform_name() or "").lower() in (
                "wechatauto", "weixin_oc"):
            yield event.plain_result("微信端发不了视频，/看视频 只在 QQ 用。")
            return
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
                        blocks = await self._video_search_texts(
                            event, kw2, p2, deep, 1)
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
            out = await self._video_search_texts(event, kw, p, deep, 2)
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
                async for chunk in self._bili_qr_flow(event):
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
                    yield event.plain_result(f"扫码登录，打不开用：{url}")
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
            report = await self._video_report(event, data)
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
                    note="页面会一直累积，最多保留最近 200 条。",
                    generated=page["updated"],
                )
                url = _write_feed_page(html)
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
                return await self._video_search_impl(event, kw, p, deep, limit)
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
                    out.append(f"[{i}] " + await self._video_report(event, data))
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


