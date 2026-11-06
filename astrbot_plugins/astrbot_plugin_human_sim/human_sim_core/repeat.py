"""复读与 +1 接龙：素材指纹、回跟与去重。"""

from __future__ import annotations

import asyncio
import os
import random
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import (
    At,
    Face,
    File,
    Forward,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)

from .config import CHAIN_FACE_BASE, _chain_family, _is_plus1
from .prompts import VULGAR_JOKE_RE
from .text import _FY_CONTENT_RE, _PAREN_DESC_RE
from .runtime import filters

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class RepeatMixin:

    # ---------- 复读：对感兴趣的东西跟着来一遍 ----------
    async def _maybe_repeat(
        self, event: AstrMessageEvent, umo: str, text: str, img_url: str,
        typed: str | None = None,
    ) -> bool:
        """群友集体复读 / 刷同一张图 / 撞上我的兴趣点 → 跟着来一遍。

        全本地判断，返回 True 表示已经发过东西，调用方应 stop_event。
        typed 是消息链里真人手打的字；传了就只用它做文本复读，
        避免把别的插件写进 message_str 的「[对方发来…]」说明原样发出去。
        """
        if typed is not None:
            text = typed
        # 括号说明（「（喊了你一声）」「[对方发来…]」）不是群友手打的字，
        # 文本复读直接当没说话；图片复读仍然走下面的 img_url
        if self._is_system_desc(text):
            text = ""
        if not self.cfg.get("repeat_enabled", True) or filters is None:
            return False
        # 夜间（活动时段外）不主动复读/跟图：这不是点名回复，真人在睡觉
        if self._night_scale() <= 0:
            return False
        # 低俗/恶俗内容不跟：复读或 +1 等于自己在群里把脏梗再讲一遍
        if VULGAR_JOKE_RE.search(text):
            return False
        # 指令不当复读素材（"/表情 吃瓜" 这种重复发也没意义）
        if text.startswith(("/", "#", "！", "!")):
            return False
        # 媒体描述也不当复读素材：多个人刷同一张带字图时，text 是
        # 「[对方发来一张图片，上面写着：…]」，照抄会把这句打进群里
        if text.startswith("[对方发来") or _FY_CONTENT_RE.search(text):
            return False
        uid = str(event.get_sender_id() or "")
        sid = str(event.get_self_id() or "")
        if uid and uid == sid:
            return False  # 自己发的，不复读自己
        now = datetime.now().timestamp()
        buf = self._repeat_buf.setdefault(umo, deque(maxlen=16))
        snap = list(buf)
        hit = filters.detect_repeat(snap, text, uid, now)
        ihit = filters.detect_image_repeat(snap, img_url, uid, now)
        # 先记账再决策：下一次别人的复读才看得见这一条
        buf.append((filters.norm(text), uid, now, img_url, text))
        # 被 @ / 点名的时候别复读，人家在正经找你说话
        # （不用 event.is_at_or_wake_command：它对群消息不可靠，自己看 chain 更准）
        at_me = False
        try:
            for c in event.message_obj.message or []:
                if isinstance(c, At) and str(getattr(c, "qq", "")) == sid:
                    at_me = True
                    break
        except Exception:  # noqa: BLE001
            pass
        named = any(n and n in text for n in (self.cfg.get("bot_names") or []))
        if at_me or named:
            return False
        last = float(self.state.get(f"last_repeat:{umo}", 0) or 0)
        if now - last < float(self.cfg.get("repeat_cooldown_seconds", 180)):
            return False

        async def _go(chain: list, tag: str, key: str = "") -> bool:
            # 同一句话/同一张图刚跟过一次就不再跟（+1 那条路也往这里记账）
            send_ts = datetime.now().timestamp()
            if self._echo_dup(umo, key, send_ts):
                logger.info(
                    f"[human_sim] 复读跳过重复跟队（群 {umo.rsplit(':', 1)[-1]}）：{key[:24]}"
                )
                return False
            try:
                await asyncio.sleep(random.uniform(0.6, 2.2))
                await self._send(umo, chain)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[human_sim] 复读失败: {type(e).__name__}: {e}")
                return False
            self.state[f"last_repeat:{umo}"] = now
            self._echo_mark(umo, key, send_ts)
            self._save_state()
            self._verdict_stats["repeat"] = self._verdict_stats.get("repeat", 0) + 1
            logger.info(f"[human_sim] 复读 {tag}（群 {umo.rsplit(':', 1)[-1]}）")
            return True

        # ① 图片复读：同一张图被不同的人刷 → 这张图火了，跟着发
        if ihit.image and random.random() < float(
            self.cfg.get("image_repeat_probability", 0.4)
        ):
            if await _go([Image(file=ihit.image)], "图片", f"img:{ihit.image}"):
                return True
        # ② 文字复读：同一句话被不同的人说过
        if hit.text and random.random() < float(
            self.cfg.get("repeat_probability", 0.55)
        ):
            echo = hit.text[:40]
            # "+1/1/+10086" 这类复读暗号只有 1~2 字，是跟风不是噪声，放行
            if len(echo) < 2 and not filters._REPEAT_TOKEN_RE.match(echo):
                return False
            # 复读必须是纯文字：原话后面再挂个表情，群里看到的就是「文字+表情」
            # 两段不同类的消息，跟原话根本不是一回事（旧版这里会 append 一个表情）
            chain = [Plain(echo)]
            if await _go(chain, f"「{echo[:12]}」", filters.norm(echo)):
                return True
            return False
        # ③ +1 模式：这话题我感兴趣（以前接过），就把它原话复制一遍发出去
        #    —— 复读机那种「+1」，不是回「关键词？」。太长就截前 N 字，问句不跟。
        if self.cfg.get("interest_learn", True) and 2 <= len(text) <= 200:
            echo = re.sub(r"\[At:[^\]]+\]|\[引用消息[^\]]*\]", "", text).strip()
            if echo.endswith(("？", "?")) or len(echo) < 2:
                return False
            interest = self.state.get(f"interest:{umo}") or {}
            wins = {w: interest.get(w, 0) for w in set(filters.tokens(echo))}
            if wins:
                word, weight = max(wins.items(), key=lambda kv: kv[1])
                min_w = int(self.cfg.get("interest_repeat_min_weight", 4))
                if weight >= min_w and len(word) >= 2 and random.random() < float(
                    self.cfg.get("interest_echo_probability", 0.3)
                ):
                    echo = echo[: int(self.cfg.get("interest_repeat_max_chars", 40))]
                    if await _go(
                        [Plain(echo)], f"+1「{echo[:12]}」", filters.norm(echo)
                    ):
                        return True
        return False

    # ---------- +1 接龙：输入层机械拦截，不走 AI 决策 ----------
    def _plus1_state(self, umo: str, now: float | None = None) -> dict | None:
        st = self.state.get(f"plus1:{umo}") or {}
        if not st.get("key"):
            return None
        now = datetime.now().timestamp() if now is None else now
        if now - float(st.get("ts", 0) or 0) > float(
            self.cfg.get("plus1_window_seconds", 300)
        ):
            return None
        return st

    def _echo_dup(self, umo: str, key: str, now: float) -> bool:
        """这条内容刚跟过一次（复读或 +1），窗口内不再跟，免得连发两条一样的"""
        if not key:
            return False
        prev = self._last_echo.get(umo)
        if not prev:
            return False
        pkey, pts = prev
        window = float(self.cfg.get("echo_dedupe_seconds", 300))
        return pkey == key and now - float(pts) < window

    def _echo_mark(self, umo: str, key: str, now: float) -> None:
        if key:
            self._last_echo[umo] = (str(key), float(now))

    @filter.llm_tool(name="plus1")
    async def tool_plus1(self, event: AstrMessageEvent, text: str = ""):
        """跟着群里正在刷屏的那句话 +1（复读机，原样照抄、多一个字都别加）。

        人类真正会 +1 的情形（符合任意一条才调用）：
        1. 从众排队：群里已经有两三个人原样刷同一句话/同一个表情，你也想参与，
           但又不想多打字 —— 这是最常见的 +1；
        2. 情绪共鸣：那句话本身就是「笑死 / 离谱 / 绷不住 / 好家伙 / 典 / 确实 /
           真相了」这类反应，你认同它，跟着刷一条；
        3. 表态不占版面：对方说了件值得认同的事（抱怨、吐槽、好消息），你想说
           「我也是 / 我也是这样 / 同感」但不想写一整句；
        4. 抢梗玩梗：某个梗刚起来，大家正复读，你插一条同一句，表示「我也懂」。
        别 +1 的情形：有人问的是需要答案的具体问题；群里在吵架/站队；
        复读已经被别人打断、话题过去了；自己刚刚已经说过别的话；
        内容涉及隐私、地址、联系方式、脏话攻击（这种别复制扩散）。
        拿不准就别调，正常说话或干脆不说话。

        Args:
            text(string): 要复读的原话；留空就自动取群里最近一句正在被复读的话
        """
        umo = event.unified_msg_origin
        st = self._plus1_state(umo) or {}
        echo = (text or "").strip() or str(st.get("text") or "")
        if not echo:
            echo = (self._plus1_prev.get(umo) or ("", "", ""))[2]
        if not echo:
            return "群里现在没有正在被复读的话，别硬 +1。"
        key = filters.norm(echo) if filters is not None else echo
        if self._echo_dup(umo, key, datetime.now().timestamp()):
            return "刚才已经跟过这句了，别连着刷。"
        try:
            await self._send(umo, [Plain(echo)])
        except Exception as e:  # noqa: BLE001
            return f"+1 没发出去：{type(e).__name__}：{e}"
        now = datetime.now().timestamp()
        self._echo_mark(umo, key, now)
        if st:
            st["spoke"] = True
            st["ts"] = now
            self.state[f"plus1:{umo}"] = st
        self.state[f"last_plus1:{umo}"] = now
        self._history.setdefault(umo, deque(maxlen=30)).append(f"大肥鱼：{echo[:60]}")
        self._save_state()
        self._verdict_stats["repeat"] = self._verdict_stats.get("repeat", 0) + 1
        return f"已 +1：{echo[:20]}"

    def _is_system_desc(self, text: str) -> bool:
        """是不是别的插件替图片/表情/拍一拍写的系统说明（不是群友手打的话）。

        这类文本在输入层就被清空，接龙、复读、决策、AI 都不该看见。
        """
        if not text:
            return False
        return bool(
            "对方发来" in text
            or _FY_CONTENT_RE.search(text)
            or _PAREN_DESC_RE.match(text)
            or re.match(r"^[\[（(]\s*(?:表情|图片|视频|语音|文件|引用消息)", text)
        )

    def _media_fingerprint(self, chain: list, raw=None) -> str:
        """同一条消息的媒体指纹：QQ 表情用专属 face id，图片用原文件/链接。

        群里连发同一个表情、同一张图，本质和连发同一句话一样，都是「+1」。
        """
        segs: list = []
        if isinstance(raw, dict) and isinstance(raw.get("message"), list):
            segs = [s for s in raw["message"] if isinstance(s, dict)]
        face_data = deque(s.get("data") or {} for s in segs if s.get("type") == "face")
        # 超级表情在新版协议里不一定报成 image：有时是 mface / marketface 段，
        # 统一收进图片队列，按段里的 summary（表情名）兜底，不再死盯 emoji_id
        image_data = deque(
            (s.get("data") or {}, str(s.get("type")) in ("mface", "marketface"))
            for s in segs
            if str(s.get("type")) in ("image", "mface", "marketface")
        )
        parts: list = []
        for c in chain:
            if isinstance(c, Face):
                data = face_data.popleft() if face_data else {}
                fid = str(data.get("id") or getattr(c, "id", "") or "").strip()
                rawinfo = data.get("raw") or {}
                fname = str(
                    rawinfo.get("faceText") or data.get("faceText") or ""
                ).strip()
                stype = rawinfo.get("stickerType")
                # 接龙表情先归族：火车 / 中火车 / 大火车 是同一个接龙，id 各不相同
                family = _chain_family(fname, stype, fid)
                # 内容指纹优先：QQ 客户端本来就是按 md5 存表情，表情/超级表情
                # 随版本更新换 id、换链接时，md5 对同一张脸不会变
                md5 = self._pick_md5(data)
                if family:
                    parts.append(f"chain:{family}")
                elif md5:
                    parts.append(f"md5:{md5}")
                elif fid:
                    parts.append(f"face:{fid}")
                elif data.get("summary"):
                    parts.append(f"fs:{data['summary']}")
            elif isinstance(c, Image):
                data, is_mface = image_data.popleft() if image_data else ({}, False)
                md5 = self._pick_md5(data)
                name = self._file_name(data.get("file") or getattr(c, "file", ""))
                summary = str(data.get("summary") or "").strip()
                eid = str(data.get("emoji_id") or "").strip()
                # 稳定性顺序：md5 内容 > 文件名 > 表情名 > emoji_id > url 文件名。
                # 超级表情改版常常换 emoji_id，但同一张图的 md5/文件名和表情名不变。
                if md5:
                    parts.append(f"md5:{md5}")
                elif name:
                    parts.append(f"img:{name}")
                elif is_mface and summary:
                    parts.append(f"ms:{summary}")
                elif eid:
                    parts.append(f"emoji:{eid}")
                else:
                    u = self._file_name(getattr(c, "url", "") or "")
                    if u:
                        parts.append(f"url:{u}")
        # 一条消息里带好几个表情包也照样比：按顺序拼成一串，顺序一致才算同一坨
        return "+".join(p for p in parts if p)

    @staticmethod
    def _file_name(value) -> str:
        """取文件名：同图可能落在不同缓存目录、链接还带随机 query，文件名最稳"""
        s = str(value or "").split("?")[0].replace("\\", "/").rstrip("/")
        return s.rsplit("/", 1)[-1] if s else ""

    @staticmethod
    def _pick_md5(data: dict) -> str:
        """从原始段里挖 md5：显式 md5 字段，或 file 名本身就是 md5（xxx.image）"""
        md5 = str(data.get("md5") or data.get("file_md5") or "").strip()
        if md5:
            return md5.lower()
        base = str(data.get("file") or "").split("?")[0].replace("\\", "/")
        base = base.rsplit("/", 1)[-1]
        if "." in base:
            stem = base.rsplit(".", 1)[0]
            if len(stem) >= 16 and all(c in "0123456789abcdefABCDEF" for c in stem):
                return stem.lower()
        return ""

    def _plus1_touch(
        self,
        umo: str,
        key: str,
        text: str,
        now: float,
        uid: str = "",
        prev: tuple | None = None,
        material: list | None = None,
    ) -> str:
        """接龙状态机（输入层）。join=该跟一条；break=接龙被打断；none=无关。

        key：这条消息的指纹（归一化文本 / face:id / img:文件 / 组合指纹）；
        prev：同群上一条别人的 (key, 发送者, 原文)，用来判断「已经有人 +1 了」；
        material：这条消息的完整组件链（文字+表情+图），接龙时原样重发。
        """
        if not self.cfg.get("plus1_enabled", True):
            return "none"
        st = self._plus1_state(umo, now)
        pk, puid, praw = (prev or ("", "", ""))
        # 「+1 / 1 / +10086」这类暗号：接龙还在就继续跟；没起头就拿上一条原话起头
        if text and _is_plus1(text):
            if st:
                st["count"] = int(st.get("count", 2)) + 1
                st["ts"] = now
                self.state[f"plus1:{umo}"] = st
                return "join"
            if pk:
                self.state[f"plus1:{umo}"] = {
                    "key": pk,
                    "text": praw or text,
                    "count": 2,
                    "ts": now,
                    "spoke": False,
                }
                return "join"
            return "none"
        if not key:
            if st:
                self.state.pop(f"plus1:{umo}", None)
                logger.info(f"[human_sim] +1 接龙被打断（群 {umo.rsplit(':', 1)[-1]}）")
                return "break"
            return "none"
        if st and st.get("key") == key:
            st["count"] = int(st.get("count", 2)) + 1
            st["ts"] = now
            if text:
                st["text"] = text
            if material:
                self._plus1_material[umo] = (key, material)
            self.state[f"plus1:{umo}"] = st
            return "join"
        if st:
            self.state.pop(f"plus1:{umo}", None)
            logger.info(f"[human_sim] +1 接龙被打断（群 {umo.rsplit(':', 1)[-1]}）")
            return "break"
        # 没在接龙：上一条消息和这条一模一样，就算已经有人 +1 了。
        # 同一个人连发两遍也认（自己号连发测试、手抖复读都能跟得上）；
        # 一条接龙里只 echo 一次（spoke 标记），不会跟着一起刷屏。
        if pk and key == pk:
            self.state[f"plus1:{umo}"] = {
                "key": key,
                "text": text or praw,
                "count": 2,
                "ts": now,
                "spoke": False,
            }
            if material:
                self._plus1_material[umo] = (key, material)
            return "join"
        return "none"

    async def _plus1_echo(self, event: AstrMessageEvent, umo: str) -> None:
        """跟着 +1：原样复制一次（文字复制文字，表情发同一个表情 id，图发同一张图）。"""
        st = self._plus1_state(umo)
        if not st or st.get("spoke"):
            return  # 同一条接龙里已经跟过一次，剩下只围观、不插话
        now = datetime.now().timestamp()
        gap = float(self.cfg.get("plus1_gap_seconds", 3))
        if now - float(self.state.get(f"last_plus1:{umo}", 0) or 0) < gap:
            return
        key = str(st.get("key") or "")
        # 类型要对得上：文字 +1 不跟表情/图，表情 +1 不跟文字。
        # 否则就会冒出「两边都在打字，机器人却甩了个表情」这种两段不是一类东西的回复
        chain_now = [
            c
            for c in (event.message_obj.message or [])
            if isinstance(c, (Plain, Face, Image))
        ]
        now_has_media = any(isinstance(c, (Face, Image)) for c in chain_now)
        now_has_text = any(
            isinstance(c, Plain) and (c.text or "").strip() for c in chain_now
        )
        key_is_media = key.startswith(
            ("md5:", "face:", "img:", "url:", "emoji:", "ms:", "fs:", "chain:")
        )
        if key_is_media and now_has_text and not now_has_media:
            return
        if not key_is_media and now_has_media and not now_has_text:
            return
        # 复读那条路可能已经跟过同一句了（别人过一会儿再发一遍又会走到这里），
        # 窗口内不跟第二次，避免连着出现两条一模一样的
        if self._echo_dup(umo, key, now):
            st["spoke"] = True
            self.state[f"plus1:{umo}"] = st
            logger.info(
                f"[human_sim] +1 跳过重复跟队（群 {umo.rsplit(':', 1)[-1]}）：{key[:24]}"
            )
            return
        payload: list | None = None
        # 优先把被 +1 的那条整条原样重发（文字、表情、图各是各的位置）。
        # 素材必须属于当前接龙的 key，否则宁可不跟：混用上一次的表情就会出现
        # 「文字接龙却甩了个表情」这种两段不是一类东西的回复
        mat = self._plus1_material.get(umo)
        if mat and str(mat[0]) == key and mat[1]:
            keep = [
                c
                for c in mat[1][:8]
                if isinstance(c, (Face, Image))
                or (isinstance(c, Plain) and (c.text or "").strip())
            ]
            payload = keep or None
        if payload is None:
            if key.startswith("md5:"):
                return  # 只有内容指纹、没有组件时没法重发，宁可不跟
            if key.startswith("face:"):
                fid = key[5:]
                if fid.isdigit():
                    payload = [Face(id=int(fid))]
            elif key.startswith("chain:"):
                # 接龙素材丢了（重启/超时），用同族基础款补一棒
                base = CHAIN_FACE_BASE.get(key[6:])
                if base:
                    payload = [Face(id=int(base))]
            elif key.startswith(("img:", "url:", "emoji:")):
                val = key.split(":", 1)[1]
                try:
                    payload = (
                        [Image.fromFileSystem(val)]
                        if os.path.exists(val)
                        else [Image(file=val)]
                    )
                except Exception:  # noqa: BLE001
                    payload = None
            else:
                echo = (st.get("text") or "").strip()
                payload = [Plain(echo)] if echo else None
        if not payload:
            return
        await asyncio.sleep(random.uniform(0.4, 1.2))
        try:
            await self._send(umo, payload)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] +1 发送失败: {type(e).__name__}: {e}")
            return
        st["spoke"] = True
        st["ts"] = now
        self.state[f"plus1:{umo}"] = st
        self.state[f"last_plus1:{umo}"] = now
        self._echo_mark(umo, key, now)
        self._save_state()
        self._history.setdefault(umo, deque(maxlen=30)).append("大肥鱼：+1")
        self._verdict_stats["repeat"] = self._verdict_stats.get("repeat", 0) + 1
        logger.info(f"[human_sim] +1 跟队（群 {umo.rsplit(':', 1)[-1]}）：{key[:24]}")
