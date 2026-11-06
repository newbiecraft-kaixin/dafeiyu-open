"""B站刷视频文本行与 HTML 页面渲染。"""
from __future__ import annotations

import re
import time
from html import escape

from .models import _fmt_play

def _bili_line(i: int, it: dict) -> str:
    d = int(it.get("duration") or 0)
    dur = f"{d // 60:02d}:{d % 60:02d}" if d else "?"
    author = str(it.get("author") or "").strip() or "未知UP"
    tag = str(it.get("tag") or "").strip()
    tname = str(it.get("tname") or "").strip()
    head = f"{i}. 《{str(it.get('title') or '')[:40]}》"
    sub = f"   UP：{author}｜播放：{_fmt_play(it.get('play'))}｜时长：{dur}"
    if tname:
        sub += f"｜分区：{tname}"
    if tag:
        sub += f"｜标签：{tag}"
    parts = int(it.get("parts") or 0)
    if parts > 1:
        sub += f"｜分P：{parts}"
    line = f"{head}\n{sub}\n   {it.get('url')}"
    desc = re.sub(r"\s+", " ", str(it.get("desc") or "")).strip()
    if desc:
        line += f"\n   简介：{desc[:200]}"
    kws = str(it.get("keywords") or "").strip()
    if kws:
        line += f"\n   关键词：{kws}"
    subtxt = str(it.get("sub") or "").strip()
    if subtxt:
        line += f"\n   字幕：{subtxt[:400]}"
    score = float(it.get("match") or 1.0)
    if score < 0.5:
        line += f"\n   标签匹配：低（{score:.0%}，请判断是否真的贴合标签）"
    cmts = [str(c) for c in (it.get("comments") or []) if str(c).strip()][:3]
    for j, c in enumerate(cmts, 1):
        line += f"\n   评论{j}：{c}"
    return line


_BILI_PAGE_CSS = """
*{box-sizing:border-box}
body{
  margin:0;padding:28px 16px 56px;color:#e8edfb;
  font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
  background:
    radial-gradient(1100px 640px at 12% -8%, rgba(86,120,255,.20), transparent 60%),
    radial-gradient(900px 560px at 92% 6%, rgba(255,184,107,.12), transparent 55%),
    linear-gradient(180deg,#080e20 0%,#05080f 100%);
}
main{width:min(980px,100%);margin:0 auto}
header{margin-bottom:26px}
.eyebrow{font-size:12px;letter-spacing:.3em;color:#8f9bbd;text-transform:uppercase}
h1{margin:8px 0 10px;font-size:30px;font-weight:600;letter-spacing:.03em}
h1 b{color:#ffd166;font-weight:600}
.meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center;color:#8f9bbd;font-size:13px}
.chip{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid rgba(140,170,255,.22);background:rgba(255,255,255,.03);color:#c7d2f0}
.chip.on{border-color:rgba(255,209,102,.45);color:#ffd166;background:rgba(255,209,102,.08)}
.thought{margin:18px 0 0;padding:13px 16px;border-left:3px solid #ffd166;background:rgba(255,209,102,.07);border-radius:8px;color:#dbe4ff;font-size:14px;line-height:1.7}
.card{display:flex;gap:14px;margin-top:18px;padding:18px 18px 16px;border-radius:16px;border:1px solid rgba(140,170,255,.18);background:rgba(18,26,48,.72);box-shadow:0 12px 30px rgba(0,0,0,.25)}
.rank{flex:0 0 42px;height:42px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:14px;color:#ffd166;border:1px solid rgba(255,209,102,.35);background:rgba(255,209,102,.08)}
.body{min-width:0;flex:1}
h2{margin:0 0 10px;font-size:17px;line-height:1.5;font-weight:600}
h2 a{color:#e8edfb;text-decoration:none}
h2 a:hover{color:#ffd166}
.rows{display:flex;flex-wrap:wrap;gap:6px 14px;color:#8f9bbd;font-size:12.5px}
.rows b{color:#c7d2f0;font-weight:500}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 0}
.sum{margin-top:12px;padding:10px 12px;border-radius:10px;background:rgba(86,120,255,.09);border:1px solid rgba(140,170,255,.16);font-size:13px;line-height:1.7}
.sum .k{color:#ffd166;font-size:12px;letter-spacing:.06em}
.sum p{margin:2px 0 0}
.text{margin:10px 0 0;font-size:13px;line-height:1.8;color:#b9c5e6;white-space:pre-wrap;word-break:break-word}
.text em{font-style:normal;color:#7f8db0}
.cmts{margin:10px 0 0;padding-left:18px;color:#9faccb;font-size:12.5px;line-height:1.75}
.cmts li{margin:3px 0}
footer{margin-top:26px;color:#6f7b9a;font-size:12px;line-height:1.8}
.none{padding:26px;border-radius:14px;border:1px dashed rgba(140,170,255,.25);color:#8f9bbd;text-align:center}
@media (max-width:560px){
  body{padding:20px 12px 44px}
  h1{font-size:24px}
  .card{padding:14px 14px 12px;gap:10px}
  .rank{flex-basis:34px;height:34px;font-size:12px}
}
"""


def bili_page_html(
    items: list[dict],
    tags: list[str] | None = None,
    summaries: dict | None = None,
    thought: str = "",
    note: str = "",
    generated: str = "",
) -> str:
    """把一批刷到的 B站视频渲染成独立 HTML 页面（深色卡片，手机可看）。"""
    sums = summaries or {}
    tag_rows = [str(t).strip() for t in (tags or []) if str(t).strip()]
    chips = "".join(f'<span class="chip on">{escape(t)}</span>' for t in tag_rows)
    now = generated or time.strftime("%Y-%m-%d %H:%M")

    cards: list[str] = []
    for i, it in enumerate(items, 1):
        d = int(it.get("duration") or 0)
        dur = f"{d // 60:02d}:{d % 60:02d}" if d else "?"
        title = escape(str(it.get("title") or "无标题"))
        url = escape(str(it.get("url") or "#"), quote=True)
        author = escape(str(it.get("author") or "未知UP"))
        tname = escape(str(it.get("tname") or ""))
        kws = str(it.get("keywords") or "").strip()
        desc = re.sub(r"\s+", " ", str(it.get("desc") or "")).strip()
        sub = re.sub(r"\s+", " ", str(it.get("sub") or "")).strip()
        cmts = [str(c).strip() for c in (it.get("comments") or []) if str(c).strip()][:3]
        s = sums.get(i) or {}
        vsum = str(s.get("video") or "").strip()
        csum = str(s.get("comment") or "").strip()

        meta = [
            f"UP：{author}",
            f"播放：{_fmt_play(it.get('play'))}",
            f"时长：{dur}",
        ]
        if tname:
            meta.append(f"分区：{tname}")
        if int(it.get("parts") or 0) > 1:
            meta.append(f"分P：{int(it.get('parts'))}")
        rows_html = "".join(f"<span><b>{m}</b></span>" for m in meta)

        chips_small = ""
        if kws:
            chips_small = '<div class="tags">' + "".join(
                f'<span class="chip">{escape(x)}</span>'
                for x in kws.replace("，", "、").split("、") if x.strip()
            ) + "</div>"

        sum_html = ""
        if vsum or csum:
            bits = []
            if vsum:
                bits.append(f'<div class="k">视频总结</div><p>{escape(vsum)}</p>')
            if csum:
                bits.append(f'<div class="k">评论总结</div><p>{escape(csum)}</p>')
            sum_html = '<div class="sum">' + "".join(bits) + "</div>"

        text_html = ""
        if desc:
            text_html += f'<div class="text"><em>简介：</em>{escape(desc[:220])}</div>'
        if sub:
            text_html += f'<div class="text"><em>字幕：</em>{escape(sub[:300])}</div>'
        cmt_html = ""
        if cmts:
            cmt_html = '<ul class="cmts">' + "".join(
                f"<li>{escape(c[:160])}</li>" for c in cmts
            ) + "</ul>"

        cards.append(
            '<article class="card">'
            f'<div class="rank">#{i}</div>'
            '<div class="body">'
            f'<h2><a href="{url}" target="_blank" rel="noopener">{title}</a></h2>'
            f'<div class="rows">{rows_html}</div>'
            f"{chips_small}{sum_html}{text_html}{cmt_html}"
            "</div></article>"
        )

    thought_html = (
        f'<div class="thought">{escape(thought)}</div>' if thought else ""
    )
    note_html = f"<footer>{escape(note)}</footer>" if note else ""
    body_html = "".join(cards) or '<div class="none">这次没刷到能看的条目。</div>'

    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        "<title>B站 · 刷视频</title>"
        f"<style>{_BILI_PAGE_CSS}</style></head><body><main><header>"
        '<div class="eyebrow">BILIBILI FEED</div>'
        f"<h1>刷到 <b>{len(items)}</b> 条</h1>"
        f'<div class="meta">{chips}<span>{escape(now)}</span></div>'
        f"{thought_html}</header>{body_html}{note_html}</main></body></html>"
    )
