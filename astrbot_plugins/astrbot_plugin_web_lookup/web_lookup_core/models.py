"""共享配置、常量与 URL/数值小工具。"""
from __future__ import annotations

import re

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}


DEFAULT_CFG = {
    "search_timeout_s": 10,   # 搜索请求超时
    "timeout_s": 12,          # 单页抓取超时
    "max_results": 16,        # 搜索名单长度（多引擎聚合后）
    "keep_n": 10,             # 校验名单后最多抓几个正文（登录墙多，多抓才有得挑）
    "page_cap_kb": 512,       # 单页流式读取上限
    "max_chars": 1500,        # 返回给模型的正文上限
    "concurrency": 4,         # 并发抓取数
    "trust_env": False,       # False=忽略系统代理直连（AstrBot 内用）
    # B站字幕：AI 字幕要登录态才能列出来，填 SESSDATA（或整条 cookie）即可
    "bili_cookie": "",
    "bili_subtitle_chars": 4000,  # 字幕原文最多回多少字给模型总结
    # B站刷视频：按标签入库片单池，每次按条数出片
    "bili_tags": ["搞笑", "pvz"],
    "bili_pool_max": 60,        # 片单池最多留多少条（超了裁掉最旧的）
    "bili_browse_default": 5,   # 不写条数时默认刷几条
    "bili_browse_max": 10,      # 一次最多刷几条
    "bili_browse_sub_chars": 400,  # 刷视频时每条带多少字字幕给模型总结
    # 刷B站防风控：每条至少停留视频时长的 1/3，模拟真人在看
    "bili_dwell_ratio": 0.34,   # 停留时长 = 视频时长 × 该比例（1/3）
    "bili_dwell_max": 90,       # 单条最多停留多少秒（半小时的视频别真等十分钟）
    "bili_dwell_total_max": 300,  # 一次刷最多累计停留多少秒
    "bili_browser": True,       # 用 Playwright 真浏览器抓搜索页/视频页（比接口更抗风控）
    "bili_browser_detail": 0,   # 真打开几个视频页取简介（0=自动最多 8 个）
    "bili_like_enabled": True,  # 允许肥鱼给自己喜欢的视频点赞
    # 视频链接解析（抖音/快手/西瓜 + B站兜底）：评论条数、音频转写上限、整体超时
    "video_comment_n": 10,           # 每个视频取前 N 条评论给 AI
    "video_audio_max_s": 300,        # 没字幕时最多转写前多少秒（超出注明「只转写了前5分钟」）
    "video_parse_timeout_s": 180,    # 单条视频解析总超时（含下载+CPU 转写）
    "video_transcript_chars": 4000,  # 转写文本最多回多少字
    "video_audio_max_mb": 60,        # 音频临时文件大小上限
}


_JUNK_HOSTS = (
    "www.baidu.com", "baidu.com", "tieba.baidu.com", "www.bing.com",
    "cn.bing.com", "bing.com", "www.google.com", "m.baidu.com",
    "www.so.com", "so.com", "m.so.com", "image.so.com", "news.so.com",
    "www.sogou.com", "sogou.com", "mp.weixin.qq.com",
)


def _ok_url(u: str) -> bool:
    if not u or not u.startswith(("http://", "https://")):
        return False
    host = re.sub(r"^https?://([^/]+).*", r"\1", u).lower()
    if "image.so.com" in host or host.startswith("image.so.com"):
        return False  # 360 图片搜索页，没有正文
    if host not in _JUNK_HOSTS:
        return True
    # 搜索引擎域名默认拦，但百度 /link?url= / 360 /link?m= 跳转链是真实结果
    # （302 后到目标站），放行
    if host == "www.baidu.com" and "/link?url=" in u:
        return True
    if host == "www.so.com" and "/link?" in u:
        return True
    return host == "www.sogou.com" and "/link?url=" in u


_HOST_RE = re.compile(r"^https?://([^/]+)")


_TRACK_PREFIX = ("utm_", "spm", "from", "share", "fr=", "f=", "isapp")


def _host(u: str) -> str:
    m = _HOST_RE.match(u)
    return (m.group(1) if m else "").lower()


def _cite_url(u: str) -> str:
    """引用用的链接：保留真实 http/https 和原 host，只去掉 fragment 与统计参数。
    不能复用 _norm_url——那个会把 http 强行改成 https，拿去引用会指错站。"""
    u = u.split("#", 1)[0]
    base, _sep, qs = u.partition("?")
    kept = []
    if qs:
        for kv in qs.split("&"):
            k = kv.split("=", 1)[0].lower()
            if not kv or any(k.startswith(p) for p in _TRACK_PREFIX):
                continue
            kept.append(kv)
    return base + ("?" + "&".join(kept) if kept else "")


def _norm_url(u: str) -> str:
    """合并用的键：去 fragment、去统计参数、去尾部斜杠，http/https 视作同一页。"""
    u = u.split("#", 1)[0]
    base, sep, qs = u.partition("?")
    kept = []
    if qs:
        for kv in qs.split("&"):
            k = kv.split("=", 1)[0].lower()
            if not kv or any(k.startswith(p) for p in _TRACK_PREFIX):
                continue
            kept.append(kv)
    base = base.rstrip("/")
    if base.startswith("http://"):
        base = "https://" + base[7:]
    return base + ("?" + "&".join(kept) if kept else "")


_W_HIGH = re.compile(
    r"qq\.com|163\.com|sina(\.com|\.cn)|sohu\.com|ifeng\.com|thepaper\.cn|"
    r"cnbeta\.com|ithome\.com|csdn\.net|juejin\.cn|36kr\.com|huxiu\.com|sspai\.com|"
    r"zhihu\.com|bilibili\.com|douban\.com|weibo\.com|"
    r"op\.gg|wanplus|score\.gg|gamekee|gamersky|17173|ali213|esports|"
    r"news\."
)


_W_OFFICIAL = re.compile(r"\.gov\.|\.edu\.")


_W_LOW = re.compile(
    r"baijiahao|zhidao|wenku|book\.|forum|bbs|tieba|douyin|kuaishou|jie\.|"
    # 单字字典/字源页：长尾中文查询常被搜索引擎拆成单字，「杰」字典页会压过
    # 真正相关的文章（「杰哥」），这类站对绝大多数查询都是噪声
    r"zidian|hgcha|zdic|hanyuguoxue|chazidian|hanzi|ziti|shufa"
)


def _domain_weight(u: str) -> float:
    h = _host(u)
    if _W_HIGH.search(h):
        return 0.8
    if _W_OFFICIAL.search(h):
        return 0.4
    if _W_LOW.search(h):
        return -0.3
    return 0.0


_STOP_WORDS = {
    "的", "了", "是", "在", "和", "有", "我", "你", "他", "她", "它", "这", "那",
    "吗", "呢", "啊", "与", "及", "等", "什么", "怎么", "为什么", "如何", "可以",
    "一个", "没有", "就是", "不是", "还是", "而且", "然后", "因为", "所以",
}


def _dur_sec(v) -> int:
    if isinstance(v, (int, float)):
        return max(0, int(v))
    sec = 0
    for p in re.split(r"[:：]", str(v or "")):
        if p.strip().isdigit():
            sec = sec * 60 + int(p.strip())
    return sec


def _fmt_play(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)
