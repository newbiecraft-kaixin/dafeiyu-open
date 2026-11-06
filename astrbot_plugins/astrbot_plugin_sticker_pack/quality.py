"""收藏质量闸门：尺寸/体积/比例、违规词与多模态粗筛。"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import uuid

from PIL import Image as PILImage
from astrbot.api import logger


class QualityMixin:
    # ---------- 收藏质量闸门 ----------
    def _ban_words(self) -> list[str]:
        raw = self.config.get("fav_ban_words") or ""
        items = [str(x) for x in raw] if isinstance(raw, (list, tuple)) else [str(raw)]
        words: list[str] = []
        for it in items:
            words.extend(re.split(r"[,，、\s]+", it))
        return [w.strip() for w in words if w.strip()]

    def _banned_hint(self, *texts) -> str:
        """文本里出现低俗/违规线索词就返回那个词，没有就返回空串。"""
        joined = " ".join(str(t or "") for t in texts)
        for w in self._ban_words():
            if w in joined:
                return w
        return ""

    def _quality_gate(self, path: str) -> str:
        """尺寸/体积/比例硬门槛；返回拒绝原因，空串 = 通过。"""
        if not self.config.get("fav_quality_gate", True):
            return ""
        try:
            nbytes = os.path.getsize(path)
        except OSError:
            return ""
        if nbytes < int(self.config.get("fav_min_bytes", 4096) or 0):
            return "图太小了（像缩略图/小图标），发出来糊"
        if nbytes > int(self.config.get("fav_max_bytes", 10485760) or 0):
            return "图太大了，收进来会把库撑爆"
        try:
            with PILImage.open(path) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001
            return ""
        side = min(w, h)
        ratio = max(w, h) / max(1, side)
        if side < int(self.config.get("fav_min_side", 100) or 0):
            return f"图太小了（短边只有 {side} 像素），发出来糊"
        if ratio > float(self.config.get("fav_max_ratio", 3.5) or 0):
            return "长截图/长条图，不适合当表情"
        return ""

    @staticmethod
    def _vision_path(path: str) -> str:
        """给看图模型用的临时文件：gif/webp 取首帧转 png。

        ollama 那类服务不认动图，gif 直接喂会 400 invalid image input，
        以前就退化成「按聊天文字瞎起名」，收进来的名字全是「群友咖波大王」这种。
        返回空串 = 图打不开，调用方直接放弃这张，别再编。
        """
        ext = os.path.splitext(path or "")[1].lower()
        if ext in (".png", ".jpg", ".jpeg"):
            return path
        try:
            fd, out = tempfile.mkstemp(prefix="fy_sight_", suffix=".png")
            os.close(fd)
            with PILImage.open(path) as im:
                im.seek(0)
                im.convert("RGB").save(out, "PNG")
            return out
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[sticker_pack] 转图失败: {type(e).__name__}: {e}")
            return ""

    async def _vision_quality(self, path: str, umo: str) -> tuple[bool, str]:
        """多模态粗筛：色情低俗、血腥、广告、截图、糊图。调用失败一律放行。"""
        if not self.config.get("fav_vision_check", True):
            return True, ""
        try:
            prov = self.context.get_using_provider(umo) if umo else self.context.get_using_provider()
        except Exception:  # noqa: BLE001
            prov = None
        if prov is None:
            return True, ""
        try:
            mods = (getattr(prov, "provider_config", {}) or {}).get("modalities") or []
            if mods and "image" not in mods:
                return True, ""  # 当前模型看不了图，别硬塞
        except Exception:  # noqa: BLE001
            pass
        prompt = (
            "判断这张聊天里收到的图能不能当日常斗图用的表情包。"
            "不合格：色情低俗、血腥暴力、赌博广告、二维码拉人、聊天/长截图、"
            "普通照片（真人/风景/物品/证件照）、糊得看不清。"
            "合格：正常梗图、表情包。只输出 OK 或 BAD，不要解释。"
        )
        vpath = self._vision_path(path)
        if not vpath:
            return True, ""
        try:
            try:
                resp = await asyncio.wait_for(
                    prov.text_chat(
                        prompt=prompt,
                        session_id=uuid.uuid4().hex,
                        contexts=[],
                        image_urls=[vpath],
                        system_prompt="你只输出 OK 或 BAD。",
                        persist=False,
                    ),
                    timeout=25,
                )
                txt = (resp.completion_text or "").strip().upper()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[sticker_pack] 视觉粗筛跳过: {type(e).__name__}: {e}")
                return True, ""
        finally:
            if vpath != path:
                try:
                    os.remove(vpath)
                except OSError:
                    pass
        if "BAD" in txt or "不合格" in txt:
            logger.info("[sticker_pack] 视觉粗筛拦下：低俗/广告/截图类")
            return False, "这张图看着不太合适（低俗/广告/截图类），不收"
        return True, ""
