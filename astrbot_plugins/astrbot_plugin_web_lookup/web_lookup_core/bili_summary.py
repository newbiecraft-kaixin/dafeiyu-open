"""B站单条视频元数据、字幕/总结/评论汇总。"""
from __future__ import annotations

import re
from html import unescape

import aiohttp

from .bili_api import (
    _BILI_DETAIL, _BILI_VIEW, _bili_conclusion, _bili_date,
    _bili_headers, _bili_hot_comments, _bili_hot_replies, _bili_reply_list,
    _bili_subtitles, _bili_wbi_mixin,
)
from .loader import _video_parse
from .models import DEFAULT_CFG, UA, _dur_sec, _fmt_play

async def _bili_summary(url: str, cfg: dict | None = None) -> str | None:
    """B站视频/短链：元数据（标题/UP/分区/发布/时长/播放点赞弹幕/标签/简介）+ 内容总结。

    B站网页是前端渲染，抓 HTML 只有 <title>；用户发链接让你读时只有接口通道有用。
    内容按可用性分级：
      1) 官方 AI 视频总结（x/view/conclusion/get，需登录 Cookie + WBI 签名）
      2) UP 的 CC/AI 字幕原文（x/player/v2）
      3) 热门评论前 3 条（x/view/detail 的 Reply）
    都没拿到就明确写「没拿到」，避免模型拿标题硬编剧情。
    """
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    headers = _bili_headers(cfg)
    cookie = str(headers.get("Cookie") or "")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10), headers=headers, trust_env=False
        ) as sess:
            real = url
            if re.search(r"(b23\.tv|bili2233\.cn)", url, re.I):
                async with sess.get(url, allow_redirects=True, max_redirects=6) as r:
                    real = str(r.url or url)
            m = re.search(r"(BV[0-9A-Za-z]{10})", real) or re.search(
                r"/video/(av\d+)", real, re.I
            )
            if not m:
                return None
            g = m.group(1)
            params = {"bvid": g} if g[:2].lower() == "bv" else {"aid": g[2:]}
            async with sess.get(_BILI_VIEW, params=params) as r:
                if r.status != 200:
                    return None
                payload = await r.json(content_type=None)
            if not isinstance(payload, dict) or payload.get("code") != 0:
                return None
            d = payload.get("data") or {}
            title = str(d.get("title") or "").strip()
            if not title:
                return None
            bvid = str(d.get("bvid") or (g if g[:2].lower() == "bv" else "")).strip()
            aid = d.get("aid")
            pages = d.get("pages") or []
            cid = d.get("cid")
            if not cid and isinstance(pages, list) and pages:
                first = pages[0]
                cid = first.get("cid") if isinstance(first, dict) else None
            owner = str((d.get("owner") or {}).get("name") or "").strip()
            up_mid = (d.get("owner") or {}).get("mid")

            # view/detail 才有标签和热门评论
            detail: dict = {}
            if bvid:
                try:
                    async with sess.get(_BILI_DETAIL, params={"bvid": bvid}) as r:
                        if r.status == 200:
                            dp = await r.json(content_type=None)
                            if isinstance(dp, dict) and dp.get("code") == 0:
                                detail = dp.get("data") or {}
                except Exception:  # noqa: BLE001
                    detail = {}

            stat = d.get("stat") or {}
            dur = _dur_sec(d.get("duration"))
            bits = [f"标题：{title}"]
            if owner:
                bits.append(f"UP主：{owner}")
            if d.get("tname"):
                bits.append(f"分区：{d['tname']}")
            pub = _bili_date(d.get("pubdate"))
            if pub:
                bits.append(f"发布：{pub}")
            if dur:
                bits.append(f"时长：{dur // 60:02d}:{dur % 60:02d}")
            nums = []
            for key, label in (("view", "播放"), ("like", "点赞"), ("danmaku", "弹幕")):
                if stat.get(key):
                    nums.append(f"{label}：{_fmt_play(stat[key])}")
            if nums:
                bits.append("｜".join(nums))
            desc = re.sub(r"\s+", " ", str(d.get("desc") or "")).strip()
            if desc and desc not in ("-", "—", "无", "暂无简介"):
                bits.append(f"简介：{desc[:300]}")
            tags = [
                unescape(str(t.get("tag_name") or "")).strip()
                for t in (detail.get("Tags") or [])
                if isinstance(t, dict) and str(t.get("tag_name") or "").strip()
            ][:8]
            if tags:
                bits.append("标签：" + "、".join(tags))
            if isinstance(pages, list) and len(pages) > 1:
                bits.append(
                    "分P："
                    + "、".join(
                        str(p.get("part") or "")[:16]
                        for p in pages[:5]
                        if isinstance(p, dict)
                    )
                )

            cap = max(200, int(cfg["bili_subtitle_chars"]))
            n_cmt = max(1, int(cfg.get("video_comment_n", 10)))
            wbim = ""
            concl = ""
            if cookie and cid:
                wbim = await _bili_wbi_mixin(sess, headers)
                concl = await _bili_conclusion(
                    sess, bvid, aid, cid, up_mid, headers, cap // 2, wbim
                )
            sub = (
                ""
                if concl
                else await _bili_subtitles(
                    sess, aid, cid, cookie, cap, bvid=bvid, duration=float(dur or 0)
                )
            )
            # 评论：每个视频都要前 N 条（以前只在没字幕时拿 3 条）
            hot = await _bili_reply_list(sess, aid, headers, n_cmt)
            if len(hot) < n_cmt:
                for msg in _bili_hot_replies(detail, n_cmt):
                    if msg not in hot:
                        hot.append(msg)
            if len(hot) < n_cmt and aid:
                wbim = wbim or await _bili_wbi_mixin(sess, headers)
                for msg in await _bili_hot_comments(sess, aid, headers, wbim, n_cmt):
                    if msg not in hot:
                        hot.append(msg)
            hot = hot[:n_cmt]

            # 没有官方 AI 总结、也没有字幕 -> 下载音轨用本地 whisper 转写兜底
            trans = ""
            tr_note = ""
            if not (concl or sub) and cid:
                try:
                    vp = _video_parse()
                    urls = await vp.bili_audio_urls(sess, bvid, aid, cid, cfg)
                    trans, truncated, why = await vp.transcribe_urls(
                        urls,
                        headers={
                            "User-Agent": UA,
                            "Referer": (
                                f"https://www.bilibili.com/video/{bvid}"
                                if bvid
                                else f"https://www.bilibili.com/video/av{aid}"
                            ),
                        },
                        cfg=cfg,
                        duration=float(dur or 0),
                    )
                    tr_note = (
                        "（只转写了前5分钟）" if (trans and truncated)
                        else "" if trans
                        else "（" + (why or "转写没成功") + "）"
                    )
                except Exception as e:  # noqa: BLE001
                    trans = ""
                    tr_note = f"（转写失败：{type(e).__name__}）"

            if concl:
                bits.append("【AI 视频总结】" + concl)
            elif sub:
                bits.append("【字幕原文】" + sub)
            elif trans:
                bits.append(f"【音频转写{tr_note}】" + trans)
            else:
                bits.append("字幕/转写：没拿到——这个视频没有可读字幕，音频转写也没成功" + tr_note)
            if hot:
                bits.append(f"【前{len(hot)}条评论（仅供参考）】" + "／".join(hot))
            else:
                bits.append("评论：没拿到——B站评论接口没返回。")
            if not (concl or sub or trans):
                bits.append(
                    "只许基于上面的标题/简介/标签/评论作答，别编造画面细节。"
                )
            return "\n".join(bits)
    except Exception:  # noqa: BLE001
        return None
