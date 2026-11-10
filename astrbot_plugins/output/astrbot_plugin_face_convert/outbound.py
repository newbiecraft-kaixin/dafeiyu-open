"""出信关口：草稿清理、meta 口癖清扫、文字表情转真实表情段。"""

import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import Face, Plain

from .utils import MAX_REPLACE


class OutboundMixin:
    async def _scrub_drafts_impl(self, event: AstrMessageEvent, response) -> None:
        """发送前的三道橡皮擦：①草稿箱（双层括号）②嘴硬小尾巴 ③meta 口癖句。"""
        try:
            text = getattr(response, "completion_text", None) or ""
            if not text:
                return
            cleaned = text
            # 第〇道：格式壳拆包（被注入带偏时，她会把回复套进程序结构里出壳）
            if ("'type'" in cleaned and "'text'" in cleaned) or (
                '"type"' in cleaned and '"text"' in cleaned
            ):
                pre = cleaned
                cleaned = self._SHELL_BLOCK.sub(
                    lambda m: "".join(
                        p.replace("\\n", "\n")
                        .replace("\\'", "'")
                        .replace('\\"', '"')
                        for p in self._SHELL_TEXT.findall(m.group(0))
                    ),
                    cleaned,
                )
                if cleaned != pre:
                    logger.info("[face_convert] 已拆格式壳")
            if "（（" in cleaned or "((" in cleaned:
                cleaned = self.SCRUB_RE.sub("", cleaned)
                if cleaned != text:
                    logger.info("[face_convert] 已撕掉草稿箱内容")
            # 嘴硬橡皮擦：只擦"回声型小尾巴"。真实识别失败时她会请求重发
            #（"没听清，请再说一次"）——含请求词的整条放行，绝不误删；
            # config.json 里 hedge_eraser=false 可整体关闭。
            _request_words = r"重发|再发|再说|再听|再看|发一遍|发一张|换一张|重新发|发个"
            hedged = (
                self.HEDGE_RE.sub("", cleaned)
                if self.cfg.get("hedge_eraser", True)
                and not event.get_extra("_fy_video_incomplete")
                and not re.search(_request_words, cleaned)
                else cleaned
            )
            if hedged != cleaned:
                cleaned = hedged
                logger.info("[face_convert] 已擦掉嘴硬小尾巴")
            scrubbed = self._scrub_meta_sentences(cleaned, event)
            if scrubbed != cleaned:
                cleaned = scrubbed
                logger.info("[face_convert] 已清扫 meta 口癖")
            owner_fixed = self._fix_owner_address(cleaned, event)
            if owner_fixed != cleaned:
                cleaned = owner_fixed
                logger.info("[face_convert] 已纠正错喊的主人称呼")
            normalized = self._normalize_punctuation(cleaned)
            if normalized != cleaned:
                cleaned = normalized
                logger.info("[face_convert] 已规范中文标点")
            cleaned = cleaned.strip()
            if not cleaned.strip("，。！？、：;；~… \n"):
                # 整条都被擦光 -> 换成鱼的沉默
                cleaned = "……"
            if cleaned != text:
                response.completion_text = cleaned
        except Exception as e:
            logger.warning(f"[face_convert] 草稿清理失败: {e}")

    async def _convert_faces_impl(self, event: AstrMessageEvent) -> None:
        result = event.get_result()
        if result is None or not result.is_llm_result():
            return

        # 思维链防漏：EmotionAI 的 <thought> 块偶尔会漏进消息（甚至被 TTS 念出来）。
        # 在"出门关口"再查一遍，不影响 EmotionAI 自己的情绪提取。
        if any(
            isinstance(c, Plain) and c.text and "<thought" in c.text
            for c in result.chain
        ):
            cleaned_chain = []
            for c in result.chain:
                if isinstance(c, Plain) and c.text:
                    t = re.sub(r"<thought>.*?</thought>", "", c.text, flags=re.DOTALL)
                    # 未闭合的 <thought>：从它一直截到结尾
                    t = re.sub(r"<thought>.*$", "", t, flags=re.DOTALL)
                    t = t.replace("</thought>", "").strip()
                    if not t:
                        continue
                    c.text = t
                cleaned_chain.append(c)
            logger.info("[face_convert] 已摘掉漏出的 <thought> 思维链块")
            if not cleaned_chain:
                result.chain = []
                return
            result.chain = cleaned_chain

        # 暗号：整条消息都是怪东西时，LLM 只输出 [NO_REPLY] -> 整条回复蒸发
        joined = "".join(
            c.text for c in result.chain if isinstance(c, Plain) and c.text
        )
        if "[NO_REPLY]" in joined.upper():
            result.chain = []
            logger.info("[face_convert] 收到 [NO_REPLY] 暗号，已吞掉这条回复")
            return

        # 工具循环（tool_loop_agent_runner）的最终回复不经过 on_llm_response，
        # 在出门关口再擦一遍 meta 口癖和中文标点；只处理 Plain 文本，不碰图片/工具结果。
        meta_changed = False
        meta_chain = []
        for c in result.chain:
            if isinstance(c, Plain) and c.text:
                fixed = self._fix_owner_address(c.text, event)
                if fixed != c.text:
                    logger.info(f"[face_convert] 已纠正错喊的主人称呼（当前发言人不是主人）: {c.text[:40]!r}")
                    c.text = fixed
                new_text = self._normalize_punctuation(
                    self._scrub_meta_sentences(c.text, event)
                )
                if new_text != c.text:
                    c.text = new_text
                    meta_changed = True
                if not c.text.strip():
                    continue
            meta_chain.append(c)
        if meta_changed:
            result.chain = meta_chain or [Plain("……")]
            logger.info("[face_convert] 已清扫 meta 口癖/标点（结果关口）")

        new_chain = []
        changed = 0
        stripped = 0
        dropped_faces = 0
        per_msg = 0
        for comp in result.chain:
            if not isinstance(comp, Plain) or not comp.text:
                new_chain.append(comp)
                continue
            text = comp.text
            pos = 0
            for m in self.pattern.finditer(text):
                if changed >= MAX_REPLACE:
                    break
                name = m.group(1).strip()
                # 模型常把占位符和方括号混写（:[偷笑]: / : [偷笑] :），
                # 冒号一起吃掉，否则会残留成 ": [表情:20] :" 这种怪东西
                s, e = m.start(), m.end()
                if s > pos and text[s - 1] in ":：":
                    s -= 1
                    if e < len(text) and text[e] in ":：":
                        e += 1
                fid = self.face_map.get(name)
                if fid is None:
                    # 兜底：像表情名的短词（1~6个中文/字母）但字典不认 ->
                    # 去掉方括号保留文字，起码不是一堆 [ ] 裸奔
                    if re.fullmatch(r"[A-Za-z一-鿿]{1,6}", name):
                        if s > pos:
                            new_chain.append(Plain(text[pos:s]))
                        new_chain.append(Plain(name))
                        pos = e
                        stripped += 1
                    continue
                # 表情冷却：同一张脸最近 2 次用过就消失；每条最多 2 张
                # （按会话隔离 + 时间过期，避免 A 群用过的脸把 B 群也冷却掉）
                hist = self._face_history.setdefault(event.unified_msg_origin, [])
                _cutoff = time.time() - float(
                    self.cfg.get("face_cooldown_minutes", 10)
                ) * 60
                hist[:] = [t for t in hist if t[1] >= _cutoff]
                if per_msg >= 2 or fid in [t[0] for t in hist[-2:]]:
                    if s > pos:
                        new_chain.append(Plain(text[pos:s]))
                    pos = e
                    dropped_faces += 1
                    continue
                if s > pos:
                    new_chain.append(Plain(text[pos:s]))
                new_chain.append(Face(id=fid))
                hist.append((fid, time.time()))
                del hist[:-8]
                per_msg += 1
                pos = e
                changed += 1
            if pos < len(text):
                new_chain.append(Plain(text[pos:]))

        if changed or stripped or dropped_faces:
            result.chain = new_chain
            logger.info(
                f"[face_convert] 已转换 {changed} 个文字表情 -> 真实表情段"
                + (f"，脱括号 {stripped} 个（字典不认的）" if stripped else "")
                + (f"，冷却丢弃 {dropped_faces} 个" if dropped_faces else "")
            )
