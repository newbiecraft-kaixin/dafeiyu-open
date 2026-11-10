"""QQ 空间回评生成：评论去重 key、提示词、模型链。"""

from __future__ import annotations

import hashlib
import re

from astrbot.api import logger

from .qzone_config import DEFAULTS, persona_prompt


class QzoneReplyMixin:
    def _reply_system_prompt(self) -> str:
        """选了中枢人格就用那套人格，再补一句空间回评的格式约束。"""
        persona = persona_prompt(self.cfg.get("persona_id"))
        base = str(self.cfg.get("system_prompt") or DEFAULTS["system_prompt"])
        if not persona:
            return base
        return persona + "\n\n（空间回评要求：只回对方这一条，一两句话、口语，不加名字前缀。）"

    def _comment_key(self, c: dict) -> str:
        cid = str(c.get("tid") or c.get("commentid") or c.get("id") or "")
        if cid:
            return cid
        seed = f"{c.get('uin')}|{c.get('createTime') or c.get('create_time') or c.get('date')}|{c.get('content')}"
        return hashlib.md5(seed.encode("utf-8", "ignore")).hexdigest()[:16]

    async def _reply_one(
        self, tid: str, commenter: str, text: str, feed_text: str,
        replying_me: bool = False,
    ) -> str:
        if replying_me:
            lead = f"{commenter} 在你的评论下面回复你：{text}"
        else:
            lead = f"{commenter} 评论你的说说：{text}"
        prompt = (
            f"这是你发的一条 QQ 空间说说：{feed_text[:200]}\n"
            f"{lead}\n\n"
            "只回这一条评论，一两句话；别替别人回答，也别把别人之间的对话接过来。"
        )
        sys = self._reply_system_prompt()
        # 每条说说是独立的一段对话：只带这条说说下最近几条来回，不串别的评论
        turns = max(0, int(self.cfg.get("history_turns", 4) or 0))
        hist = self.state.setdefault("history", {}).setdefault(tid, [])
        # 同一条说说下可能有好几个人留言：只带当前评论者自己的来回，避免把 B 的
        # 上下文套到 C 身上（角色串线就是这样来的）
        pairs = []
        for i in range(0, len(hist) - 1, 2):
            u, a = hist[i], hist[i + 1]
            if str(u.get("role")) == "user" and str(u.get("content") or "").startswith(
                f"{commenter}："
            ):
                pairs.append((u, a))
        contexts = []
        for u, a in pairs[-turns:]:
            contexts.append({"role": "user", "content": u.get("content") or ""})
            contexts.append({"role": "assistant", "content": a.get("content") or ""})
        for provider in self._provider_chain():
            try:
                resp = await provider.text_chat(
                    prompt=prompt,
                    session_id=f"qzone:{tid}",
                    contexts=contexts,
                    system_prompt=sys,
                    persist=True,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[qzone_channel] 生成回复失败: {type(e).__name__}: {e}")
                continue
            out = (resp.completion_text or "").strip().strip('"“”')
            out = re.sub(r"^[\w\u4e00-\u9fa5]{1,8}[：:]\s*", "", out)  # 去名字前缀
            out = re.sub(r"\s+", " ", out).strip()
            if len(out) >= 2:
                hist.extend([
                    {"role": "user", "content": f"{commenter}：{text}"},
                    {"role": "assistant", "content": out},
                ])
                del hist[: max(0, len(hist) - turns * 2)]
                self._save_state()
                return out[:120]
        return ""

    async def _compose_feed_comment(self, feed_text: str) -> str:
        """给别人的空间说说写一句自然评论；失败返回空串。"""
        prompt = (
            f"这是别人发的一条 QQ 空间说说：{feed_text[:200]}\n\n"
            "写一句自然的评论：一句话，不超过 30 个字，朋友随口回的那种；"
            "不要客套，不要重复原句，不要带名字前缀。"
        )
        sys = self._reply_system_prompt()
        for provider in self._provider_chain():
            try:
                resp = await provider.text_chat(
                    prompt=prompt,
                    session_id="qzone:view-comment",
                    contexts=[],
                    system_prompt=sys,
                    persist=False,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[qzone_channel] 生成空间评论失败: {type(e).__name__}: {e}"
                )
                continue
            out = (resp.completion_text or "").strip().strip('"“”')
            out = re.sub(r"^[\w\u4e00-\u9fa5]{1,8}[：:]\s*", "", out)
            out = re.sub(r"\s+", " ", out).strip()
            if len(out) >= 2:
                return out[:60]
        return ""

    def _provider_chain(self) -> list:
        chain = []
        want = str(self.cfg.get("prefer_provider_id") or "").strip()
        if want:
            try:
                p = self.context.get_provider_by_id(want)
                if p is not None:
                    chain.append(p)
            except Exception:  # noqa: BLE001
                pass
        try:
            p = self.context.get_using_provider()
            if p is not None and p not in chain:
                chain.append(p)
        except Exception:  # noqa: BLE001
            pass
        return chain
