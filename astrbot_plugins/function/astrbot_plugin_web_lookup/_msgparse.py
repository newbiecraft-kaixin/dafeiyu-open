"""消息解析：分享卡片、引用消息、@ 我识别、视频链接提取。

从 main.py 拆出来的纯函数，保持同名导出，main.py 直接 import 使用。
"""

from __future__ import annotations

import json
import re

from astrbot.api.event import AstrMessageEvent

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
