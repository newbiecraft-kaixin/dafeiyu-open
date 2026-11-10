"""选表情用的轻量 LLM 调用（只走会话当前模型）。"""

from __future__ import annotations

import asyncio
import uuid

from astrbot.api import logger


class LlmMixin:
    async def _ask_llm(self, prompt: str, system: str | None = None, umo: str = "") -> str:
        """选表情用「当前会话正在用的模型」。

        原来这里硬编码成本地 9B，等于让最弱的模型做「5 选 1 且只回名字」这种细活，
        而群里接的明明是 DeepSeek。现在只按会话取 provider，不再回退本地小模型
        （本地选错表情比没表情更伤体验）。
        """
        sysp = system or "你是 QQ 机器人 大肥鱼。严格按指令回复，不要解释，不要引号。"
        chain: list = []
        try:
            cur = self.context.get_using_provider(umo) if umo else self.context.get_using_provider()
            if cur is not None:
                chain.append(cur)
        except Exception:  # noqa: BLE001
            pass
        seen_ids: set[str] = set()
        for provider in chain:
            try:
                pid = provider.meta().id
            except Exception:  # noqa: BLE001
                pid = str(id(provider))
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        session_id=uuid.uuid4().hex,
                        contexts=[],
                        system_prompt=sysp,
                        persist=False,
                    ),
                    timeout=25,
                )
                txt = (resp.completion_text or "").strip()
                if txt:
                    return txt
                logger.warning(f"[sticker_pack] 选表情模型 {pid} 返回空，换下一个")
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[sticker_pack] 选表情模型 {pid} 调用失败: {type(e).__name__}: {e}"
                )
        return ""
