"""人味处理器：把 LLM 输出的「AI 味」洗成人话（钩子层）。

清洗逻辑在 cleaner.py（纯函数，human_sim 等其他插件也能复用）。
本文件只负责：
- on_llm_response：模型回复出来立刻洗一遍
- on_decorating_result：工具循环的回复不经过上面的钩子，出门前补洗；
  同时做复读拦截（和最近发言太像就改口）与连续自言自语封顶。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import random
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Face, Image, Plain
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))
STATE_DIR = os.path.join(DATA_ROOT, "plugin_data", "astrbot_plugin_humanizer")
STATE_PATH = os.path.join(STATE_DIR, "recent.json")


def _load_cleaner_module():
    path = os.path.join(PLUGIN_DIR, "cleaner.py")
    spec = importlib.util.spec_from_file_location("astrbot_humanizer_cleaner", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_cleaner_mod = _load_cleaner_module()
Cleaner = _cleaner_mod.Cleaner
jaccard = _cleaner_mod.jaccard
SHORT_REPLIES = _cleaner_mod.SHORT_REPLIES
REPEAT_THRESHOLD = _cleaner_mod.REPEAT_THRESHOLD


def _load_send_gate():
    """跨插件出站节流（与 human_sim / sticker_pack 共用同一实例）。"""
    import importlib.util as _ilu
    import sys as _sys

    name = "fy_send_gate"
    if name in _sys.modules:
        try:
            return _sys.modules[name].get_gate()
        except Exception:  # noqa: BLE001
            return None
    path = os.path.join(DATA_ROOT, "plugin_data", "fy_send_gate.py")
    try:
        spec = _ilu.spec_from_file_location(name, path)
        mod = _ilu.module_from_spec(spec)
        _sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod.get_gate()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[humanizer] 发送节流模块加载失败（不节流）: {e}")
        return None


send_gate = _load_send_gate()
# 模型自己写的表情占位符（半角/全角/混用冒号），主流水线没人渲染，一律掐掉
_PLACEHOLDER_RE = re.compile(r"[:：]\s*([\u4e00-\u9fa5A-Za-z]{1,8})\s*[:：]")
_BOUNDARY = set("。！？!?，,、~～… \n\t")


def _drop_placeholders(text: str) -> str:
    """只掐「独立成段」的占位符，避免误伤「他说：明早：八点」这种正常标点。"""
    def _sub(m: re.Match) -> str:
        s, e = m.start(), m.end()
        left_ok = s == 0 or text[s - 1] in _BOUNDARY
        right_ok = e == len(text) or text[e] in _BOUNDARY
        return "" if (left_ok or right_ok) else m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, text)
CONSECUTIVE_CAP = 2
# 连续发言封顶后甩的黄脸池：呲牙/捂脸/偷笑/疑问/睡前/吃瓜/无语/看看你
CAP_FACE_IDS = [28, 86, 21, 32, 9, 66, 78, 81]
# 回复收尾黄脸池：呲牙/惊讶/疑问/白眼/微笑…（比封顶池更日常）
TAIL_FACE_IDS = [13, 14, 21, 22, 28, 32, 66, 78, 81, 86, 101, 271]


@register(
    "astrbot_plugin_humanizer",
    "dafeiyu-local",
    "人味处理器：清洗 markdown/AI 腔/长句排比，防复读与自言自语",
    "v1.0.0",
)
class HumanizerPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = config or {}
        self.cleaner = Cleaner(self.cfg)
        self.state = self._load_state()
        self._gate = send_gate  # 出站节流：同会话两条消息至少隔 1.6~3 秒
        logger.info(
            "[humanizer] 已启动：markdown清洗=%s AI腔清洗=%s 复读拦截=%s 连续上限=%s",
            self.cfg.get("strip_markdown", True),
            self.cfg.get("strip_ai_tone", True),
            self.cfg.get("repeat_guard", True),
            self.cfg.get("consecutive_cap", CONSECUTIVE_CAP),
        )

    # ---------------- 状态 ----------------
    def _load_state(self) -> dict:
        try:
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data.setdefault("recent", {})
                    data.setdefault("sent_ts", {})
                    return data
        except Exception:
            pass
        return {"recent": {}, "sent_ts": {}}

    def _save_state(self) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            cutoff = time.time() - 3 * 3600
            for key in list(self.state["recent"].keys()):
                self.state["recent"][key] = [
                    x for x in self.state["recent"][key] if x[1] > cutoff
                ][-10:]
            for key in list(self.state["sent_ts"].keys()):
                self.state["sent_ts"][key] = [
                    t for t in self.state["sent_ts"][key] if t > cutoff
                ][-10:]
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[humanizer] 状态保存失败: {e}")

    # ---------------- 钩子 ----------------
    @filter.on_llm_response()
    async def humanize_llm(self, event: AstrMessageEvent, response) -> None:
        try:
            text = getattr(response, "completion_text", None) or ""
            if not text.strip():
                return
            new_text, stats = self.cleaner.humanize(text)
            if new_text != text:
                response.completion_text = new_text
                logger.info(
                    "[humanizer] 清洗(md=%s 丢AI句=%s) | %s -> %s",
                    stats["md"],
                    stats["ai"],
                    text[:30].replace("\n", " "),
                    new_text[:30].replace("\n", " "),
                )
        except Exception as e:
            logger.warning(f"[humanizer] on_llm_response 处理失败: {e}")

    @filter.on_decorating_result()
    async def guard_result(self, event: AstrMessageEvent) -> None:
        try:
            result = event.get_result()
            if result is None or not result.is_llm_result():
                return
            umo = event.unified_msg_origin
            now = time.time()

            # ⓪ 先掐掉模型自己写的表情占位符（:害羞:、：害羞：、:害羞：都算）。
            #    主流水线没人渲染这些，漏出去就是聊天窗里一串冒号
            for comp in result.chain:
                if isinstance(comp, Plain) and comp.text:
                    fixed = _drop_placeholders(comp.text)
                    if fixed != comp.text:
                        comp.text = fixed.strip()

            # ① 补洗（工具循环的最终回复不经过 on_llm_response）
            for comp in result.chain:
                if isinstance(comp, Plain) and comp.text and comp.text.strip():
                    new_text, _ = self.cleaner.humanize(comp.text)
                    if new_text != comp.text:
                        comp.text = new_text

            text = "".join(
                c.text for c in result.chain if isinstance(c, Plain) and c.text
            ).strip()
            if not text:
                # 只有 @ / 引用、正文是空的（模型空输出或工具循环漏嘴）：
                # 核心会把整条跳过，用户只看到「戳了一下什么都没说」，补一句人话
                if any(not isinstance(c, Plain) for c in result.chain):
                    result.chain.append(Plain(text=random.choice(SHORT_REPLIES)))
                self._save_state()
                return

            # ② 系统层去重：同一会话 10 秒内一字不差再说一遍，直接清链拦掉。
            #    重复发送是管道层面的事，不该交给模型判断「要不要再说」；
            #    这里硬拦截，连 respond 阶段都不会看到这条 result。
            cutoff = now - 10
            if any(
                old == text and ts >= cutoff
                for old, ts in self.state["recent"].get(umo, [])
            ):
                logger.info("[humanizer] 重复回复拦截: %s", text[:24])
                result.chain.clear()
                event.clear_result()
                event.stop_event()
                self._save_state()
                return

            # ③ 复读拦截：和最近几条太像就改口
            if self.cfg.get("repeat_guard", True):
                for old_text, _ts in self.state["recent"].get(umo, [])[-6:]:
                    if jaccard(text, old_text) > REPEAT_THRESHOLD:
                        alt = random.choice(SHORT_REPLIES)
                        logger.info(
                            "[humanizer] 复读拦截: '%s' ≈ '%s' -> '%s'",
                            text[:20],
                            old_text[:20],
                            alt,
                        )
                        for comp in result.chain:
                            if isinstance(comp, Plain):
                                comp.text = alt
                                alt = ""
                        break

            # ③ 连续自言自语封顶（3 分钟内）：只对「很短的一句」做降级
            #    原来无条件把文字清空、只甩黄脸 —— 用户看到的就是「刚还在说话，突然只剩一个表情」，
            #    正经回答被吃掉。现在只处理一句很短的口水话，长内容一律原样发出去。
            cap = int(self.cfg.get("consecutive_cap", CONSECUTIVE_CAP))
            stamps = [t for t in self.state["sent_ts"].get(umo, []) if t > now - 180]
            if cap > 0 and len(stamps) >= cap and len(text) <= 12:
                logger.info(
                    "[humanizer] 连续发言 %d 条达上限，短句降级为一张黄脸", len(stamps)
                )
                has_non_text = any(not isinstance(c, Plain) for c in result.chain)
                for comp in result.chain:
                    if isinstance(comp, Plain):
                        comp.text = ""
                if not has_non_text:
                    result.chain.append(Face(id=random.choice(CAP_FACE_IDS)))

            final_text = "".join(
                c.text for c in result.chain if isinstance(c, Plain) and c.text
            ).strip()

            # 读一眼再打字：真人不会秒回，按字数缩放一个 0.8~3.2 秒的反应时间。
            # 提前算好，分条任务按这个时间点排队，避免补发气泡抢在主回复前面发出去。
            typing = 0.0
            if final_text and self.cfg.get("typing_delay", True):
                base = random.uniform(0.8, 1.6)
                extra = min(len(final_text) / 14.0, 1.6)
                typing = min(base + extra, 3.2)

            did_split = False

            # ④ 像真人一样分条发：真人聊微信不会把三句话塞进同一条气泡，
            #    而是「想到一句发一句」。这里把一条回复按句拆成 2~3 条，
            #    第一条走正常链路，其余由后台任务按字数延时补发（模拟打完再打下一条）。
            #    参考 OpenSelf 的 Human Mimicry 与 Aki 的 multi-bubble delivery。
            if (
                final_text
                and len(result.chain) == 1
                and isinstance(result.chain[0], Plain)
                and len(final_text) <= 120
                and "\n" not in final_text
                and "http" not in final_text.lower()
                and random.random()
                < float(self.cfg.get("multi_bubble_probability", 0.5))
            ):
                bubbles = self._split_bubbles(
                    final_text, int(self.cfg.get("max_bubbles", 3))
                )
                if len(bubbles) >= 2:
                    result.chain[0].text = bubbles[0]
                    did_split = True
                    # 主回复还要 typing 秒才发：先占住位置，补发气泡排在它后面
                    if self._gate is not None:
                        self._gate.mark(umo, ahead=typing)
                    bot = getattr(event, "bot", None)
                    sid = str(event.get_self_id() or "") if hasattr(event, "get_self_id") else ""
                    asyncio.create_task(
                        self._send_bubbles(umo, bot, sid, bubbles[1:])
                    )
                    logger.info("[humanizer] 分条发送：%s", " | ".join(bubbles))

            # ⑤ 概率性 QQ 黄脸收尾：短文字回复、链里还没有任何表情/图片时，
            #    像真人一样偶尔甩个黄脸（18%）
            #    ⚠ 文本里还有 :情绪词: 占位符时**绝不能**追加黄脸：
            #    那是 sticker_pack 准备单独甩图片表情包的信号，黄脸一进来
            #    就变成「有表情了」，用户看到的就是一条黄脸而不是图片。
            _has_placeholder = any(
                re.search(r":[^:\s]{1,10}:", c.text or "")
                for c in result.chain
                if isinstance(c, Plain)
            )
            if (
                final_text
                and not did_split
                and not _has_placeholder
                and len(final_text) <= 60
                and random.random()
                < float(self.cfg.get("face_tail_probability", 0.08))
                and not any(
                    isinstance(c, (Face, Image))
                    for c in result.chain
                )
            ):
                result.chain.append(Face(id=random.choice(TAIL_FACE_IDS)))
                logger.info(
                    "[humanizer] 黄脸收尾: %s", final_text[:24]
                )

            if final_text:
                if typing:
                    await asyncio.sleep(typing)
                self.state["recent"].setdefault(umo, []).append([final_text, now])
                self.state["sent_ts"].setdefault(umo, []).append(now)
            # 核心马上要把这条发出去：记一笔，后续补发的气泡/主动消息排队避让
            if self._gate is not None:
                self._gate.mark(umo)
            self._save_state()
        except Exception as e:
            logger.warning(f"[humanizer] on_decorating_result 处理失败: {e}")

    # ---------------- 分条发送 ----------------
    @staticmethod
    def _split_bubbles(text: str, max_n: int = 3) -> list[str]:
        """按句末标点切成 1~max_n 条气泡。

        太碎的句子（"嗯。"）会粘到前一条上 —— 真人不会为了一个语气词单独发一条。
        """
        sents = [s.strip() for s in re.split(r"(?<=[。！？!?~～…])", text) if s.strip()]
        if len(sents) < 2:
            return [text]
        merged: list[str] = []
        for s in sents:
            if merged and len(merged[-1]) < 6:
                merged[-1] += s
            else:
                merged.append(s)
        while len(merged) > max_n:
            merged[-2] += merged[-1]
            merged.pop()
        return merged

    async def _send_bubbles(
        self, umo: str, bot, self_id: str, texts: list[str]
    ) -> None:
        """延时补发后续气泡：等「打字时间」再发，像真人一条条敲出来。"""
        gid = umo.rsplit(":", 1)[-1]
        is_group = ":GroupMessage:" in umo
        for text in texts:
            delay = min(0.5 + len(text) * 0.09, 4.0) + random.uniform(0.2, 0.6)
            await asyncio.sleep(delay)
            if self._gate is not None:
                await self._gate.wait(umo)
            try:
                if bot is not None and gid.isdigit():
                    params: dict = {
                        "message": [{"type": "text", "data": {"text": text}}],
                    }
                    params["group_id" if is_group else "user_id"] = int(gid)
                    if self_id.isdigit():
                        params["self_id"] = int(self_id)
                    await bot.api.call_action(
                        "send_group_msg" if is_group else "send_private_msg",
                        **params,
                    )
                    if self._gate is not None:
                        self._gate.mark(umo)
                    continue
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[humanizer] 分条直连失败，回退 context: {type(e).__name__}: {e}"
                )
            try:
                await self.context.send_message(
                    umo, MessageChain(chain=[Plain(text)])
                )
                if self._gate is not None:
                    self._gate.mark(umo)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[humanizer] 分条补发失败: {type(e).__name__}: {e}")

    async def terminate(self) -> None:
        self._save_state()
