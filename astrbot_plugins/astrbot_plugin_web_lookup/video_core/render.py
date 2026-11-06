"""视频解析结果格式化。"""
from __future__ import annotations

from .models import AUTHOR_LABEL, DEFAULT_CFG, NAMES, _clean, _fmt_dur, _fmt_num

def render_text(data: dict, cfg: dict | None = None) -> str:
    """把 parse 结果拼成给 AI 的纯文本（标题/作者/时长/统计/简介/标签/评论/字幕或转写）。"""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    platform = data.get("platform")
    if not platform:
        return "这个链接不是支持的视频站点（B站/抖音/快手/西瓜），先别当视频总结。"
    name = NAMES.get(platform, "视频")
    n = int(cfg.get("video_comment_n", 10))
    lines: list[str] = []
    lines.append("标题：" + (data.get("title") or "没拿到"))
    lines.append(f"{AUTHOR_LABEL.get(platform, '作者')}：" + (data.get("author") or "没拿到"))
    lines.extend(data.get("extra") or [])
    lines.append("时长：" + (_fmt_dur(data.get("duration")) or "没拿到"))
    stat = data.get("stats") or {}
    parts = []
    for key, label in (("view", "播放"), ("like", "点赞"), ("danmaku", "弹幕"), ("comment", "评论")):
        if stat.get(key):
            parts.append(f"{label}：{_fmt_num(stat[key])}")
    lines.append("｜".join(parts) if parts else "播放/点赞：没拿到")
    desc = _clean(data.get("desc"))
    lines.append("简介：" + (desc[:300] if desc else "没拿到"))
    tags = data.get("tags") or []
    lines.append("标签：" + ("、".join(tags[:8]) if tags else "没拿到"))
    imgs = data.get("image_urls") or []
    if imgs:
        lines.append(
            f"【图片】{len(imgs)}张（图文作品；只读了图里的文字，未做画面识别）"
        )
    ocr = data.get("ocr_text") or ""
    if ocr:
        lines.append("【图片文字（OCR）】" + ocr)

    sub = data.get("subtitle") or ""
    trans = data.get("transcript") or ""
    if sub:
        lines.append("【字幕原文】" + sub)
    elif trans:
        note = "（只转写了前5分钟）" if data.get("transcript_truncated") else ""
        lines.append(f"【音频转写{note}】" + trans)
    else:
        why = "；".join(data.get("notes") or []) or "没有可读字幕，也没有可下载的音轨"
        lines.append("【字幕/转写】没拿到——" + why)

    cmts = data.get("comments") or []
    if cmts:
        lines.append(f"【前{min(len(cmts), n)}条评论】" + " ／ ".join(
            f"{i}. {c}" for i, c in enumerate(cmts[:n], 1)
        ))
    else:
        why = "；".join(x for x in (data.get("notes") or []) if "评论" in x) or "接口没返回"
        lines.append("【评论】没拿到——" + why)
    lines.append(
        "说明：以上只包含实际抓到的内容；图文作品的画面没有做识别，"
        "不要描述图里的景物、人物长相或动作，只能依据上面的文字说明。"
    )
    is_note = data.get("is_note") or (data.get("image_urls") and not data.get("media_urls"))
    kind = "图文作品" if is_note else "视频"
    return f"【{name}{kind}】\n来源：{data.get('url')}\n内容：\n" + "\n".join(lines)
