"""占位符归一、文本拆分与 emoji/颜文字预转换。"""

from __future__ import annotations

import random
import re

from astrbot.api.message_components import Face, Plain

from .faces import EMOJI_TO_FACE, KAOMOJI_TO_FACE
from .settings import FW_PLACEHOLDER, PLACEHOLDER


class RenderMixin:
    def _normalize_fw_placeholders(self, text: str) -> str:
        """全角占位符（：西瓜：）归一成半角，只有名字确实在库里才换。"""
        if "：" not in text:
            return text

        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name in self.stickers:
                return f":{name}:"
            # 纯英文名（：doge：）不可能是正文标点，直接抹掉；中文原名留着免得吃掉正文
            return "" if name.isascii() else m.group(0)

        return FW_PLACEHOLDER.sub(repl, text)

    def _split_stickers(self, text: str, umo: str, budget: list[int]) -> tuple[list, list[dict]]:
        """把一个 Plain 文本拆成「内联文本段」+「要单独发出去的表情」。

        对齐真人习惯：
          · 图片表情包 -> 一律从文本里剥离，文字先发、隔一两秒单独甩一条图
          · 黄脸 -> 短回复（正文 ≤ N 字）基本都拎出来单发：单条纯表情 = QQ 超大表情
          · 长回复里的黄脸 -> 多数内联在句子里
        名字命不中的占位符：先按文字相似度从全库抽图，仍不中就直接抹掉，
        绝不让 :xxx: 字面量漏进群里。
        """
        out: list = []
        stickers: list[dict] = []
        text = self._normalize_fw_placeholders(text)
        pos = 0
        solo_face_prob = float(self.config.get("solo_face_probability", 0.5))
        limit = int(self.config.get("short_reply_char_limit", 6))
        short_prob = float(self.config.get("short_reply_solo_probability", 0.85))
        # 去掉占位符之后还剩多少字？很短的话就把表情拎出来，蹭超大表情
        plain_len = len(PLACEHOLDER.sub("", text).strip())
        effective = short_prob if plain_len <= limit else solo_face_prob
        for m in PLACEHOLDER.finditer(text):
            raw = m.group(1)
            exact = self.stickers.get(raw)
            # 一律先走图片池。原来这里的「同名 face 直接返回」是最坑的一刀：
            # _preconvert 把颜文字 (¬‿¬) 映射成 :坏笑:，而库里「坏笑」精确同名的是
            # 黄脸 face:101，于是每次都去发小黄脸，图池里现成的「坏笑图」永远轮不到，
            # 加上 face 曾经全部 retcode 1200，表现就是「表情经常性看不到」。
            # 现在图片优先，只有图池一张都挑不出来、而占位符本身正好是库里的黄脸时才兜底。
            name = self._pick_image(raw, umo) or ""
            item = self.stickers.get(name) if name else None
            if item is None:
                if exact is not None and exact.get("kind") == "super":
                    # 超级表情实名命中直接用它（单发一条纯 face 才是放大的大表情）
                    name, item = raw, exact
                elif exact is not None and exact.get("kind") == "face":
                    # 白名单过滤：:斜眼笑: 这种冷门脸换成近似常用脸，没替身就丢弃
                    canon = self._canon_face(raw)
                    if canon:
                        name, item = canon, self.stickers[canon]
                else:
                    self._note_deficit(raw, umo)
            if item is None:
                # 占位符抹掉，但保留它前面的文字
                if m.start() > pos:
                    out.append(Plain(text[pos : m.start()]))
                pos = m.end()
                continue
            is_image = item.get("kind") == "image"
            # 超级表情一律拎出来单发：混在句子中间只会缩成普通小表情
            solo = is_image or item.get("kind") == "super" or random.random() < effective
            before = text[pos : m.start()]
            pos = m.end()
            if solo:
                if before:
                    out.append(Plain(before))
                if budget[0] <= 0:
                    continue  # 超额表情丢弃，文本照旧
                budget[0] -= 1
                stickers.append({"name": name, **item})
            else:
                if before:
                    out.append(Plain(before))
                out.append(Face(id=item["value"]))
        if pos < len(text):
            out.append(Plain(text[pos:]))
        out = self._tidy_plain_chain(out)
        return out, stickers

    @staticmethod
    def _tidy_plain_chain(out: list) -> list:
        """合并相邻 Plain 并压掉占位符剥离后留下的连续空格。

        「嗯夸完了 :坏笑: 群里…」单发表情后剩下的两段 Plain 拼回消息会变成
        「嗯夸完了  群里…」（原占位符两侧的空格都留下了）——真人打字不会有
        连续空格，群里看到的「词   词」就是这么来的。中文回复里段与段拼回
        只留一个空格；带换行的段落原样拼接，不把换行挤掉。
        """
        tidied: list = []
        for seg in out:
            if isinstance(seg, Plain) and tidied and isinstance(tidied[-1], Plain):
                prev, cur = tidied[-1].text or "", seg.text or ""
                if prev.strip(" \t") and cur.strip(" \t"):
                    if prev.endswith("\n") or cur.startswith("\n"):
                        merged = prev.rstrip(" \t") + cur.lstrip(" \t")
                    else:
                        merged = prev.rstrip(" \t") + " " + cur.lstrip(" \t")
                else:
                    merged = (prev + cur).strip(" \t")
                if merged:
                    tidied[-1].text = merged
                else:
                    tidied.pop()
                continue
            tidied.append(seg)
        return tidied

    def _preconvert(self, text: str) -> str:
        """unicode emoji / 裸情绪词 / ASCII 颜文字 -> :黄脸名: 占位符（名字不在库里则跳过）"""
        # 模型常把两种写法混着用（: [偷笑] : / ：[偷笑]:），先归一成 :名称:，
        # 否则方括号会先被 face_convert 吃掉，外层冒号残留成 ": [表情:20] :"
        text = re.sub(
            r"[:：]\s*[\[【]\s*([^\[\]【】\s]{1,12})\s*[\]】]\s*[:：]?",
            r":\1:",
            text,
        )
        # 占位符内部不再做裸词替换：否则 ":吃瓜:" 会被再包一层成 "::吃瓜::"，
        # 外层冒号残留到群里（就是之前 ": [表情:20] :" 的根因）
        parts = re.split(r"(:[^:\s]{1,12}:)", text)
        for i in range(0, len(parts), 2):  # 偶数下标 = 占位符之外的普通文本
            seg = parts[i]
            for emoji, name in EMOJI_TO_FACE.items():
                if emoji in seg and name in self.stickers:
                    seg = seg.replace(emoji, f":{name}:")
            # 裸情绪词替换已停用：正文里出现「晚安」「可爱」「厉害」「睡觉」这类
            # 常用字会被直接换成表情并从句子中抠掉（「晚安，早点睡」-> 「，早点睡」），
            # 表情没多几成、句子先被切碎。表情只认三种明确信号：
            #   AI 自己写的 :情绪词: 、emoji、颜文字。
            for kao, name in KAOMOJI_TO_FACE.items():
                if kao in seg and name in self.stickers:
                    seg = seg.replace(kao, f":{name}:")
            parts[i] = seg
        # 👍🏻 这类带肤色修饰符的，替换掉主体 emoji 后还剩一个 🏻 挂在占位符后面，
        # 发到群里就是「:赞:🏻」。变体选择符（❤️ 的 ️）同理。这些都是纯修饰符，
        # 删掉不影响任何文字语义。
        return re.sub(r"[\ufe0f\U0001F3FB-\U0001F3FF]", "", "".join(parts))
