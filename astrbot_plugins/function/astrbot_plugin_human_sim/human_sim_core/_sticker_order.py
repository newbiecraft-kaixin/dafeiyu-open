"""表情/梗图/超级表情点单与 AI 挑选（从 media.py 按职责拆出）。"""

from __future__ import annotations

import asyncio
import os
import random
import re
import uuid
from datetime import datetime
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Face, Image

from .config import LOCAL_DECIDE_PROVIDER
from .text import _NO_STICKER_REQ_RE, _STICKER_REQUEST_RE, _norm_request_text

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class MediaOrderMixin:

    async def _send_super_series(self, umo: str, picked: list[str]) -> list[str]:
        """一条一条把超级表情发出去：真人不会把三个塞进同一条消息，连排也糊成一片。"""
        faces = self._super_faces()
        pairs = [(n, Face(id=int(faces[n]["id"]))) for n in picked if n in faces]
        await asyncio.sleep(random.uniform(0.6, 1.6))
        sent: list[str] = []
        for i, (name, comp) in enumerate(pairs):
            ok = await self._send(umo, [comp])
            if not ok:
                logger.warning(f"[human_sim] 超级表情第 {i + 1} 条没发出去")
                break
            sent.append(name)
            if i < len(pairs) - 1:
                await asyncio.sleep(random.uniform(0.8, 1.6))
        return sent

    def _emoji_pick_candidates(self, text: str, hist: list[str]) -> list[tuple[str, str]]:
        """给模型看的候选：先留句子里点到的、聊天里刚提到的，不够再随机补。"""
        faces = self._super_faces()
        comics = [n for _, n, _ in self._sticker_meta if len(n) >= 2]
        cap = max(4, int(self.cfg.get("emoji_ai_pick_candidates", 12)))
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for n in faces:
            if n and (n in text or any(n in h for h in hist)) and n not in seen:
                seen.add(n)
                out.append(("超级", n))
        for n in comics:
            if (n in text or any(n in h for h in hist)) and n not in seen:
                seen.add(n)
                out.append(("梗图", n))
        if len(out) < cap:
            pool = [("超级", n) for n in faces] + [("梗图", n) for n in comics]
            random.shuffle(pool)
            for k, n in pool:
                if len(out) >= cap:
                    break
                if n not in seen:
                    seen.add(n)
                    out.append((k, n))
        return out[:cap]

    async def _ai_pick_emoji(self, umo: str, text: str) -> tuple[str, str] | None:
        """让模型按聊天场景挑一个表情：返回 ("super"/"comic", 名字)，挑不出返回 None。"""
        faces = self._super_faces()
        comics = [n for _, n, _ in self._sticker_meta if len(n) >= 2]
        if not faces and not comics:
            return None
        hist = list(self._history.get(umo, []))[
            -max(1, int(self.cfg.get("emoji_ai_pick_history", 6))):
        ]
        ctx = "\n".join(hist) if hist else "（没有聊天记录）"
        cand = self._emoji_pick_candidates(text, hist)
        if not cand:
            return None
        meaning_map = {n: m for _, n, m in self._sticker_meta}
        menu = "\n".join(
            f"{k}:{n}"
            + (
                f"（{meaning_map[n]}）"
                if k == "梗图"
                and meaning_map.get(n)
                and meaning_map[n] != n
                else ""
            )
            for k, n in cand
        )
        prompt = (
            f"最近群里在聊：\n{ctx}\n\n"
            f"有人刚说：{text}\n\n"
            f"可发的表情：\n{menu}\n\n"
            "从上面挑一个最贴合此刻气氛的，直接输出它（照抄「超级:名字」或「梗图:名字」）。\n"
            "都不合适就输出「不发」。只输出一行，不要解释。"
        )
        provider = None
        if umo:
            try:
                provider = self.context.get_using_provider(umo)
            except Exception:  # noqa: BLE001
                provider = None
        provider = (
            provider
            or self.context.get_provider_by_id(LOCAL_DECIDE_PROVIDER)
            or self.context.get_using_provider()
        )
        if not provider:
            return None
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    session_id=uuid.uuid4().hex,
                    contexts=[],
                    system_prompt="你只负责从候选里挑一个表情，只输出一行结果。",
                    persist=False,
                ),
                timeout=float(self.cfg.get("emoji_ai_pick_timeout", 12)),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 表情 AI 挑选失败: {type(e).__name__}: {e}")
            return None
        raw = (resp.completion_text or "").strip()
        logger.info(f"[human_sim] 表情点单 AI 挑选结果: {raw[:40]!r}")
        if not raw or "不发" in raw:
            return None
        comic_set = set(comics)
        m = re.search(r"(超级|梗图)\s*[:：]\s*([^\s，,。;；、\n]+)", raw)
        kind, name = (None, "")
        if m:
            kind = "super" if m.group(1) == "超级" else "comic"
            name = m.group(2)
        else:
            name = re.sub(r"[「」『』“”\"'。，,、\n]", "", raw).strip()
        if not name:
            return None
        if kind == "super" and name in faces:
            return ("super", name)
        if kind == "comic" and name in comic_set:
            return ("comic", name)
        if name in faces:
            return ("super", name)
        if name in comic_set:
            return ("comic", name)
        logger.info(f"[human_sim] 表情 AI 挑选无匹配结果: {raw[:40]!r}")
        return None

    async def _handle_emoji_request(self, event: AstrMessageEvent) -> bool:
        """「发个表情 / 发个打龟 / 发个超级赞」：发什么交给模型按场景挑。

        以前这里按固定优先级抢发（超级表情 > 梗图），同一句话有几种含义时规则永远选同一种。
        现在先让模型看最近聊天和候选挑一个，挑不出来才回落到两个硬规则处理函数。
        """
        if not self.cfg.get("emoji_ai_pick", True):
            return False
        text = _norm_request_text(event.message_str or "")
        if not text or len(text) > 20:
            return False
        if _NO_STICKER_REQ_RE.search(text):
            return False
        if not re.search(r"发|来|整|给|甩|要", text):
            return False
        faces = self._super_faces()
        comics = [n for _, n, _ in self._sticker_meta if len(n) >= 2]
        if not (
            _STICKER_REQUEST_RE.search(text)
            or re.search(r"超级(?:大)?表情|大表情", text)
            or any(n and n in text for n in faces)
            or any(n in text for n in comics)
        ):
            return False
        umo = event.unified_msg_origin
        now = datetime.now().timestamp()
        uid = str(event.get_sender_id() or "")
        key = f"last_emoji_pick:{umo}:{uid}"
        if now - float(self.state.get(key, 0) or 0) < 15:
            return False
        pick = await self._ai_pick_emoji(umo, text)
        if not pick:
            return False
        kind, name = pick
        if kind == "super":
            count = 1
            if re.search(r"几个|几张|一些|多点|多来|再来", text):
                count = 3
            elif re.search(r"两|2", text):
                count = 2
            picked = [name]
            rest = [n for n in faces if n != name]
            if len(picked) < count and rest:
                picked += random.sample(rest, min(count - 1, len(rest)))
            sent = await self._send_super_series(umo, picked)
            if not sent:
                return False
            self._note_act(umo, f"发了超级表情：{'、'.join(sent)}")
            logger.info(
                f"[human_sim] 点单 AI 按场景选超级表情「{name}」→ {'、'.join(sent)}"
            )
        else:
            path = next((p for p, n, _ in self._sticker_meta if n == name), None)
            if not path:
                return False
            await asyncio.sleep(random.uniform(0.6, 1.6))
            try:
                await self._send(umo, [Image.fromFileSystem(path)])
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[human_sim] 点单 AI 选的梗图发送失败: {type(e).__name__}: {e}"
                )
                return False
            self._note_act(umo, f"发了表情包：{self._sticker_desc(path)}")
            logger.info(
                f"[human_sim] 点单 AI 按场景选梗图「{name}」→ {os.path.basename(path)}"
            )
        self.state[key] = now
        return True

    async def _handle_sticker_request(self, event: AstrMessageEvent) -> bool:
        """「发个表情 / 来张图 / 斗图」这种点单：本地直接甩图，不等 AI。

        为什么必须本地做：本地 9B 模型基本不会主动调 send_meme，
        靠它自己发就等于不发（日志里这两个工具一整天都是 0 次）。用户张嘴要了，
        这里就必须真发一张出去。
        """
        if not self.cfg.get("sticker_request_local", True):
            return False
        text = (event.message_str or "").strip()
        # 只接短句点单，避免把「这个表情包好好笑」之类的闲聊也当成点单
        # 先剥 [At:123]/@昵称 占位，否则 @ 一下就把 12 字门槛撑爆
        text = _norm_request_text(text)
        if not text or len(text) > 12:
            return False
        # 只是提到「表情包」、或者「不要/不用」这类否定句，都不是点单
        if _NO_STICKER_REQ_RE.search(text):
            return False
        # 两条入口：
        # ①「发个表情/斗图/来张图」这种泛点单
        # ②「发个打龟/来个打龟」这种点名点单 —— 名字得在 map.txt 里（打龟、干饭、抱抱…）
        named_paths = [
            p for p, name, _ in self._sticker_meta if len(name) >= 2 and name in text
        ]
        named_order = bool(named_paths) and bool(re.search(r"发|来|整|给|甩|要", text))
        if not _STICKER_REQUEST_RE.search(text) and not named_order:
            return False
        # 不卡 watch_groups：只要有人张嘴要，哪个群都得真发出去
        umo = event.unified_msg_origin
        if not self._stickers:
            return False
        now = datetime.now().timestamp()
        # 同一个人 4 秒内只发一张，防连点「再来一张」刷屏；换个人要就照发
        uid = str(event.get_sender_id() or "")
        key = f"last_sticker_req:{umo}:{uid}"
        if now - float(self.state.get(key, 0) or 0) < 4:
            return False
        path = self._match_sticker(text) or random.choice(self._stickers)
        await asyncio.sleep(random.uniform(0.6, 1.6))
        try:
            await self._send(umo, [Image.fromFileSystem(path)])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 点单表情发送失败: {type(e).__name__}: {e}")
            return False
        self.state[key] = now
        self._note_act(umo, f"发了表情包：{self._sticker_desc(path)}")
        logger.info(
            f"[human_sim] 点单表情「{text}」本地发送：{os.path.basename(path)}"
        )
        return True

    async def _handle_super_face_request(self, event: AstrMessageEvent) -> bool:
        """「发个超级表情 / 来个超级赞」：本地直接发，不等 AI。

        超级表情（face id 360/362/364 这类）模型经常嘴上答应了、实际不调
        send_super_face，用户看到的就是「说了发却没发」。这里本地兜住。
        """
        if not self.cfg.get("super_face_request_local", True):
            return False
        raw_text = (event.message_str or "").strip()
        text = _norm_request_text(raw_text)
        if not text or len(text) > 30:
            if "表情" in raw_text:
                logger.info(
                    f"[human_sim] 点单表情未识别（长度）：raw={raw_text[:40]!r} clean={text!r}"
                )
            return False
        faces = self._super_faces()
        if not faces:
            return False
        named = [n for n in faces if n and n in text]
        if not named and not re.search(r"超级(?:大)?表情|大表情", text):
            return False
        # 必须是「要」的动作，避免把「超级表情好好玩」这种闲聊当点单
        if not re.search(r"发|来|整|给|甩|要", text):
            return False
        umo = event.unified_msg_origin
        now = datetime.now().timestamp()
        uid = str(event.get_sender_id() or "")
        key = f"last_super_req:{umo}:{uid}"
        if now - float(self.state.get(key, 0) or 0) < 15:
            return False
        count = 1
        if re.search(r"几个|几张|一些|多点|多来|再来", text):
            count = 3
        elif re.search(r"两|2", text):
            count = 2
        if named:
            picked = named[:count]
            rest = [n for n in faces if n not in picked]
            if len(picked) < count and rest:
                picked += random.sample(rest, min(count - len(picked), len(rest)))
        else:
            picked = random.sample(list(faces), min(count, len(faces)))
        sent = await self._send_super_series(umo, picked)
        if not sent:
            return False
        self.state[key] = now
        self._note_act(umo, f"发了超级表情：{'、'.join(sent)}")
        logger.info(
            f"[human_sim] 点单超级表情「{text}」本地分 {len(sent)} 条发送：{'、'.join(sent)}"
        )
        return True
