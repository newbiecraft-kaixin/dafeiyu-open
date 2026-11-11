"""文件消息只取标题 + 格式，不下载文件本体。

QQ 里有人发文件时，AstrBot 默认会 get_file() 把文件拉到临时目录，再交给文件
解析（PDF / 表格 / 文档抽取）。大肥鱼只需要知道「谁发了什么文件」，所以这里
在进入 LLM 之前把 File 组件摘掉，只往消息里补一行：

    [文件] 季度汇报（格式：pptx）

文件本体不落盘，也不会触发 URL 下载。
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, unquote, urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File as FileComp
from astrbot.api.star import Context, Star, register

DONE_FLAG = "_file_meta_done"


def _from_url(url: str) -> str:
    """从下载链接里猜文件名：优先路径，路径没扩展名再看 query 里的 name/file。"""
    try:
        parsed = urlparse(url)
        base = os.path.basename(unquote(parsed.path))
        if os.path.splitext(base)[1]:
            return base
        for key in ("name", "file", "filename"):
            for value in parse_qs(parsed.query).get(key, []):
                cand = os.path.basename(unquote(value).replace("\\", "/"))
                if os.path.splitext(cand)[1]:
                    return cand
        return base
    except Exception:  # noqa: BLE001
        return ""


def _brief(comp: FileComp) -> str:
    """从 File 组件里取标题与格式（扩展名），缺字段时按顺序兜底。"""
    raw = str(getattr(comp, "name", "") or "").strip()
    if not raw:
        path = str(getattr(comp, "file_", "") or "").strip()
        raw = os.path.basename(path.replace("\\", "/")) if path else ""
    if not raw:
        raw = _from_url(str(getattr(comp, "url", "") or "").strip())
    raw = os.path.basename(raw.replace("\\", "/")).strip() or "未命名文件"
    title, ext = os.path.splitext(raw)
    fmt = ext.lstrip(".").lower() or "未知"
    return f"[文件] {title or raw}（格式：{fmt}）"


@register("file_meta", "kaixin", "文件消息只解析标题与格式，不下载文件本体", "1.0.0")
class FileMeta(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=500)
    async def strip_files(self, event: AstrMessageEvent):
        """摘掉 File 组件，换成一行标题+格式；没有文件消息就直接放行。"""
        if event.get_extra(DONE_FLAG):
            return
        try:
            chain = event.message_obj.message
        except Exception:  # noqa: BLE001
            return
        if not isinstance(chain, list):
            return
        lines = [_brief(comp) for comp in chain if isinstance(comp, FileComp)]
        if not lines:
            return
        event.set_extra(DONE_FLAG, True)
        chain[:] = [comp for comp in chain if not isinstance(comp, FileComp)]
        merged = f"{(event.message_str or '').strip()} {' '.join(lines)}".strip()
        event.message_str = merged
        try:
            event.message_obj.message_str = merged
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[file_meta] 回写 message_str 失败: {e}")
