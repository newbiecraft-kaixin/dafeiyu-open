"""插件内共用的小工具：路径、URL 识别、文件 URI、受限下载、限时子进程。"""

import asyncio
import os
import re

import aiohttp

from astrbot.api import logger

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_REPLACE = 8  # 单条消息最多替换几个表情，防刷屏

# ---------------- QQ 视频链接识别 ----------------
URL_RE = re.compile(r"https?://[^\s，,。；、）)】”\"'<>]+")
# 分享口令里的链接后面经常直接粘着中文（/复制此链接，打开抖音搜索…），
# 按 URL 字符集截断，否则会把「残链」当成视频地址去解析，回复里也可能带出来
_URL_SAFE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.I)


def _clean_url(u: str) -> str:
    s = str(u or "").strip().replace("\\/", "/")
    m = _URL_SAFE.match(s)
    return (m.group(0) if m else s).rstrip(".,;、)】")

VIDEO_HOST_RE = re.compile(
    r"(b23\.tv|bili2233\.cn|bilibili\.com|douyin\.com|iesdouyin\.com|kuaishou\.com|v\.qq\.com"
    r"|weishi\.qq\.com|iqiyi\.com|youku\.com|xiaohongshu\.com|xhslink\.com"
    r"|haokan\.baidu\.com|weibo\.com|weibo\.cn|b1g1\.cn)",
    re.I,
)
# B站网页纯前端渲染，抓 HTML 只有 <title>；这两个域名走官方 view 接口拿真内容
BILI_HOST_RE = re.compile(r"(b23\.tv|bili2233\.cn|bilibili\.com)", re.I)

def _strip_site_suffix(title: str) -> str:
    """B站等站点的 <title> 带站名尾巴（xxx_哔哩哔哩_bilibili），剥掉。"""
    for suf in ("_哔哩哔哩_bilibili", "_哔哩哔哩 (゜-゜)つロ 干杯~-bilibili", "_高清1080P在线观看平台", "_腾讯视频", "-爱奇艺", "_优酷视频"):
        if title.endswith(suf):
            return title[: -len(suf)].strip()
    return title


def file_uri_to_path(u: str) -> str:
    """把 file:// URI 转成本机路径；http(s) 原样返回。

    兼容三类（Windows / Linux / 裸路径）：
      file:///C:/x/y  -> C:/x/y        （Windows：去掉前导斜杠）
      file:///tmp/y   -> /tmp/y        （Linux：保留开头的斜杠！）
      /tmp/y          -> /tmp/y
    """
    s = str(u).strip()
    if s.lower().startswith(("http://", "https://")):
        return s
    if s.lower().startswith("file://"):
        s = s[7:]  # 去掉 "file://"（三个斜杠时这里留下路径自带的那个 /）
        if re.match(r"^/[A-Za-z]:", s):  # Windows 的 /C:/... -> C:/...
            s = s[1:]
        return s
    return s


async def download_capped(
    session: "aiohttp.ClientSession", url: str, max_bytes: int, timeout_total: int
) -> bytes | None:
    """流式下载并限制大小；超限/超时/非 200 返回 None（调用方走降级分支）。"""
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout_total)
        ) as r:
            if r.status != 200:
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in r.content.iter_chunked(65536):
                size += len(chunk)
                if size > max_bytes:
                    logger.warning(
                        "[face_convert] 媒体超过大小上限 %d 字节，已放弃下载", max_bytes
                    )
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except (asyncio.TimeoutError, OSError, aiohttp.ClientError):
        return None


async def run_bounded(
    proc: asyncio.subprocess.Process, timeout: float
) -> tuple[int, bytes]:
    """超时或外层取消都杀进程、排空管道并回收；外层取消继续传播。"""
    communication = asyncio.create_task(proc.communicate())
    try:
        _, err = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
        return proc.returncode or 0, err or b""
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await communication
        if isinstance(exc, asyncio.CancelledError):
            raise
        logger.warning("[face_convert] 子进程超时（%ss），已强制终止", timeout)
        return -1, b"timeout"
