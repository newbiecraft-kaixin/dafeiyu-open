"""输出渲染：占位符换图、人味清洗、描述与斗图回击（从 media.py 按职责拆出）。"""

from __future__ import annotations

import asyncio
import os
import random
import re
from datetime import datetime
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Face, Image, Plain

from .text import _MIX_TOKEN_RE, _TOKEN_RE
from .runtime import _CLEANER

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class MediaRenderMixin:

    def _norm_fw_token(self, m: re.Match, name_to_path: dict) -> str:
        """全角占位符单个归一：库里有 → 半角；纯英文名一定不是正文 → 抹掉；中文保留。"""
        name = m.group(1)
        if name in name_to_path or name in self._face_by_name:
            return f":{name}:"
        return "" if name.isascii() else m.group(0)

    def _render_placeholders(self, chain: list) -> list:
        """输出层兜底：把 :名称: 换成对应表情图；库里没有的名字直接抹掉。

        主动发言（插话/冷场/拍一拍应答…）不经过 sticker_pack 的
        on_decorating_result，裸占位符会原样发到群里，所以在这里统一收口。
        一条消息最多塞 max_stickers_per_message 张图（默认 1），已有的图也算数。
        """
        if not any(
            isinstance(c, Plain) and _MIX_TOKEN_RE.search(c.text or "") for c in chain
        ):
            return chain
        name_to_path: dict[str, str] = {}
        for path, name, _ in self._sticker_meta:
            name_to_path.setdefault(name, path)
        # 冒号写法五花八门（:吃瓜:、：吃瓜：、:吃瓜：），先统一归一成半角，
        # 库里有这个名字才换；不认识的中文保留原文，纯英文/数字一定不是正文，抹掉
        for c in chain:
            if isinstance(c, Plain) and c.text and (":" in c.text or "：" in c.text):
                c.text = _MIX_TOKEN_RE.sub(
                    lambda m: self._norm_fw_token(m, name_to_path), c.text
                )
        media = sum(1 for c in chain if isinstance(c, (Image, Face)))
        budget = max(0, int(self.cfg.get("max_stickers_per_message", 1)) - media)
        out: list = []
        for comp in chain:
            if not isinstance(comp, Plain):
                out.append(comp)
                continue
            text = comp.text or ""
            pos = 0
            for m in _TOKEN_RE.finditer(text):
                before = text[pos:m.start()]
                if before.strip():
                    out.append(Plain(before))
                token = m.group(1)
                path = name_to_path.get(token)
                fid = self._face_by_name.get(token)
                if path and budget > 0:
                    out.append(Image.fromFileSystem(path))
                    budget -= 1
                elif fid and budget > 0:
                    out.append(Face(id=fid))
                    budget -= 1
                elif path or fid:
                    logger.info(
                        f"[human_sim] 占位符 :{token}: 已到单条图片上限，转为不发"
                    )
                else:
                    # 清洗层的名字表和这里不完全一致（它还认 face_map.json），
                    # 认不出就按语义再找一张，别让模型挑的表情凭空消失
                    fb = self._match_sticker(token) if budget > 0 else None
                    if fb:
                        out.append(Image.fromFileSystem(fb))
                        budget -= 1
                        logger.info(f"[human_sim] 表情名 :{token}: 未收录，按语义挑了 {os.path.basename(fb)}")
                    else:
                        logger.info(f"[human_sim] 丢弃库外占位符 :{token}:")
                pos = m.end()
            tail = text[pos:]
            if tail.strip():
                out.append(Plain(tail))
        if not any(isinstance(c, Plain) and (c.text or "").strip() for c in out) and not any(
            isinstance(c, (Image, Face)) for c in out
        ):
            return []
        return out

    def _humanize(self, text: str) -> str:
        """主动插话的文本不过 on_decorating_result，必须自己洗一遍再发。"""
        if not text:
            return ""
        # 占位符不在这里抹：留给 _send 的输出层统一换成真表情/图，抹掉就少了表情
        cleaned = _CLEANER.humanize(text)[0] if _CLEANER else text.strip()
        cleaned = cleaned.strip()
        # 洗成「……」说明全是套话，这种宁可不发（主动搭话不能没内容）
        if not cleaned.strip("…。， "):
            logger.info("[human_sim] 清洗后只剩沉默词，放弃这条主动消息")
            return ""
        return cleaned

    def _sticker_desc(self, path: str) -> str:
        """把表情文件写成人能看懂的描述：名称（意思）。

        以前历史里只记文件名（n296.gif）或不带意思的名称，模型下一轮
        根本不知道自己刚发的那张图是什么意思。
        """
        p = str(path or "")
        base = os.path.basename(p)
        for sp, name, meaning in self._sticker_meta:
            if sp == p or os.path.basename(sp) == base:
                if meaning and meaning != name:
                    return f"{name}（{meaning}）"
                return name
        return base

    def _face_desc(self, fid) -> str:
        """黄脸 id 反查名字，写回上下文时别只留一个数字。"""
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            return "表情"
        for name, i in self._face_by_name.items():
            if int(i) == fid:
                return name
        return f"表情{fid}"

    def _describe_chain_media(self, chain: list) -> list[str]:
        """把一条消息里的图片/黄脸转成能写进上下文的描述。"""
        out: list[str] = []
        for comp in chain or []:
            if isinstance(comp, Image):
                path = getattr(comp, "path", "") or getattr(comp, "file", "") or ""
                out.append(self._sticker_desc(path))
            elif isinstance(comp, Face):
                out.append(self._face_desc(getattr(comp, "id", None)))
        return out

    def _describe_tokens(self, text: str) -> list[str]:
        """回复里写的 :名称: 实际会被换成表情，取出名称和意思。"""
        if not text:
            return []
        known = {n for _, n, _ in self._sticker_meta}
        meaning = {n: m for _, n, m in self._sticker_meta}
        out: list[str] = []
        for m in _MIX_TOKEN_RE.finditer(text):
            token = m.group(1)
            if token in self._face_by_name:
                out.append(token)
            elif token in known:
                mn = meaning.get(token) or ""
                out.append(f"{token}（{mn}）" if mn and mn != token else token)
        return out

    def _note_tokens(self, umo: str, text: str) -> None:
        """回复里的 :名称: 发送时会被换掉，历史里补一条带意思的说明。"""
        descs = self._describe_tokens(text)
        if descs and umo:
            self._note_act(umo, f"发了表情包：{'、'.join(descs[:2])}")

    def _match_sticker(self, text: str) -> str | None:
        """按消息内容智能挑梗图：先匹配名字/意思关键词，再按字符重叠度，都不行随机"""
        if not self._sticker_meta:
            return None
        text = text or ""
        # 1) 消息里正好提到某个表情名 → 用它
        for path, name, _ in self._sticker_meta:
            if len(name) >= 2 and name in text:
                return path
        # 2) 字符重叠度门槛提到 3，防「聊鱼蹦鱼片」式误配；配不上就纯随机
        chars = set(text)
        best, best_score = None, 2
        for path, name, meaning in self._sticker_meta:
            score = len((set(name) | set(meaning)) & chars)
            if score > best_score:
                best, best_score = path, score
        return best or random.choice(self._stickers)

    def _maybe_append_emoji(
        self, chain: list, text: str = "", force: bool = False, umo: str = ""
    ) -> None:
        # 长文（故事/干货）后面甩个表情很怪，直接不加；对方明着要表情则无视长度
        if not force and len(text or "") > 120:
            return
        r = random.random()
        note = ""
        if self._stickers and (force or r < float(self.cfg["sticker_probability"])):
            path = self._match_sticker(text)
            if path:
                chain.append(Image.fromFileSystem(path))
                note = f"回复里带了个表情包：{self._sticker_desc(path)}"
                if force:
                    logger.info(f"[human_sim] 按要求补发表情: {os.path.basename(path)}")
        elif not force and r < float(self.cfg["sticker_probability"]) + float(
            self.cfg["face_probability"]
        ):
            pool = self._map_faces or self._face_pool()
            fid = random.choice(pool)
            chain.append(Face(id=fid))
            note = f"回复里带了个表情：{self._face_desc(fid)}"
        if note and umo:
            self._note_act(umo, note)

    async def _maybe_sticker_back(self, event: AstrMessageEvent, umo: str) -> bool:
        """对方只甩了个表情包：不写字，回一个表情（斗图）。

        主人或被点名的必回；群友按概率回。同群冷却内不重复，省得两个人对着刷图。
        """
        try:
            cd = float(self.cfg.get("sticker_back_cooldown_seconds", 240) or 240)
        except (TypeError, ValueError):
            cd = 240.0
        now = datetime.now().timestamp()
        if now - float(self.state.get(f"last_sticker_back:{umo}", 0) or 0) < cd:
            return False
        uid = str(event.get_sender_id() or "")
        directed = uid in self._owner_uids() or any(
            n and n in (event.message_str or "") for n in (self.cfg.get("bot_names") or [])
        )
        try:
            p = 1.0 if directed else float(self.cfg.get("sticker_back_probability", 0.35))
        except (TypeError, ValueError):
            p = 0.35
        if random.random() > p:
            return False
        if not self._global_gate(umo):
            return False
        if self._stickers and random.random() < 0.7:
            path = random.choice(self._stickers)
            chain: list = [Image.fromFileSystem(path)]
            desc = self._sticker_desc(path)
        else:
            fid = random.choice(self._map_faces or self._face_pool())
            chain = [Face(id=fid)]
            desc = self._face_desc(fid)
        await asyncio.sleep(random.uniform(0.6, 1.6))
        try:
            await self._send(umo, chain)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 斗图回表情失败: {type(e).__name__}: {e}")
            return False
        self.state[f"last_sticker_back:{umo}"] = now
        self._note_act(
            umo,
            f"回了{'个表情包' if isinstance(chain[0], Image) else '个表情'}：{desc}",
        )
        logger.info(
            f"[human_sim] 斗图回了{'个表情包' if isinstance(chain[0], Image) else '个黄脸'}"
            f"（{umo.rsplit(':', 1)[-1]}，{'主人/点名' if directed else '群友'}）"
        )
        return True
