"""图片收藏主流程：下载 → 合规/查重 → 看图起名 → 入库或待核验。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import time
import urllib.request
import uuid

from astrbot.api import logger

from .fingerprint import _fp_similarity


class CollectMixin:
    async def _ingest_image(self, comp, umo: str, event=None,
                            context_text: str = "") -> str:
        """收一张图：下载 → 算指纹 → 查重 → 存待确认区。返回空串=已就绪，否则是放弃原因。"""
        try:
            path = await comp.convert_to_file_path()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_pack] 图片下载失败: {type(e).__name__}: {e}")
            return ""
        if not path or not os.path.exists(path):
            return ""
        # 质量闸门：硬门槛（尺寸/体积/比例）→ 文本线索 → 视觉粗筛。
        # 拦下就静默放弃，不给用户发任何收藏提示（原因只写日志）。
        why = self._quality_gate(path)
        if why:
            logger.info(f"[sticker_pack] 收藏质量闸门拦下：{why}")
            return why
        bad = self._banned_hint(context_text)
        if bad:
            logger.info(f"[sticker_pack] 收藏命中低俗/违规词「{bad}」，跳过")
            return f"带「{bad}」，跳过"
        ok, vwhy = await self._vision_quality(path, umo)
        if not ok:
            logger.info(f"[sticker_pack] 视觉粗筛拦下：{vwhy}")
            return ""
        fp = self._fingerprint(path)
        dup, sim = self._find_duplicate(fp)
        if not dup:
            # 同一张图在待确认区还没落库时也要拦住（否则同一会话里连发两次会各存一份）
            thr = float(self.config.get("fav_similarity_max", 0.90))
            for pinfo in list(self._pending.values()):
                s = _fp_similarity(fp, pinfo.get("fp") or "")
                if s > thr and s > sim:
                    dup, sim = "刚发的那张", s
        if dup:
            label = self._fp_label(dup)
            logger.info(f"[sticker_pack] 收藏查重命中：{label}（相似度 {sim:.2f}）")
            return ""
        try:
            os.makedirs(self._pending_dir(), exist_ok=True)
            ext = os.path.splitext(path)[1].lower() or ".png"
            fid = uuid.uuid4().hex[:12]
            dst = os.path.join(self._pending_dir(), f"{fid}{ext}")
            shutil.copyfile(path, dst)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_pack] 暂存图片失败: {type(e).__name__}: {e}")
            return ""
        self._pending[umo] = {
            "file": dst, "fp": fp, "dhash": (fp or {}).get("d"), "ts": time.time(),
            "umo": umo, "ctx": (context_text or "")[:80],
        }
        self._save_pending()
        return ""

    async def _collect_if_worthy(self, comp, umo: str, event=None, context_text: str = "",
                                 review: bool = True, quota: bool = True) -> tuple[bool, str]:
        """算法自主收藏：收图 → 质量/合规/查重（_ingest_image）→ 看图评分起名 → 入库。

        返回 (是否入库, 名字或放弃原因)。全程静默：不回复、不追问、不看关键词。
        看图模型认不出来的图直接放弃——认不出就说明它不适合当表情。
        """
        err = await self._ingest_image(comp, umo, event=event, context_text=context_text)
        if err:
            return False, err
        pend = self._pending.pop(umo, None)
        self._save_pending()
        if not pend:
            return False, ""
        # 没有本地识图模型时不丢图：放进待核验区，主人在存储页看图命名、
        # 勾选核验后才入库（与「一键 AI 识别」共用同一套人工流程）。
        if not await self._vision_ready():
            try:
                ok, stored = self._fav_park(
                    pend, "待命名",
                    (context_text or "").strip()[:40] or "待补含义",
                )
            except Exception as e:  # noqa: BLE001
                try:
                    os.remove(pend.get("file", ""))
                except OSError:
                    pass
                return False, f"暂存待命名失败 {type(e).__name__}: {e}"
            if not ok:
                return False, stored
            if quota:
                self._take_quota()
            logger.info(f"[sticker_pack] 无识别模型，已放待核验区等人工命名：{stored}")
            return True, f"待命名 {stored}"
        info = await self._label_for_group(context_text, pend.get("file", ""), umo)
        if not info:
            try:
                os.remove(pend.get("file", ""))
            except OSError:
                pass
            return False, "看图识别失败"
        try:
            if review:
                ok, stored = self._fav_park(pend, info["name"], info["meaning"])
            else:
                ok, stored = self._fav_insert(pend, info["name"], info["meaning"])
        except Exception as e:  # noqa: BLE001
            try:
                os.remove(pend.get("file", ""))
            except OSError:
                pass
            return False, f"入库异常 {type(e).__name__}: {e}"
        if not ok:
            return False, stored
        if quota:
            self._take_quota()
        return True, f"{stored}：{info['meaning']}"

    async def _label_for_group(self, text: str, image_path: str = "",
                               umo: str = "") -> dict | None:
        """给新图起名：让看图模型认图并起名。

        看不了/失败就直接返回 None，交给调用方放弃这张 —— 不再退回「拿聊天文字
        瞎起名」的兜底，那种兜底会把「群友咖波大王」这类跟图无关的词收进库。
        """
        prompt = (
            "这张图是聊天里收到的表情包图片。"
            + (f"发图的人同时说了：{text[:60]}\n" if text else "")
            + f"机器人最近缺这些情绪的表情：{self._deficit_hint() or '（无）'}\n"
            "看图判断：如果它适合当表情包，就起一个不超过 6 个字的中文短名，并说明适合什么场合；"
            "如果它不是表情包（普通照片/截图/广告/糊图），就返回空名字。"
            '只输出 JSON：{"name":"短名或空串","meaning":"场合说明"}'
        )
        sysp = "你只输出 JSON，不输出任何别的字符。"
        if not image_path or not os.path.exists(image_path):
            return None
        vpath = self._vision_path(image_path)
        if not vpath:
            return None
        try:
            # 认图必须用真正带视觉的本地模型。AstrBot 里配的 ollama provider
            # 实际指向 dafeiyu-q14（纯文本），deepseek-flash 也自称支持图片但会 400，
            # 走它们一张都收不进来，所以这里直连 ollama 的 qwen3.5:9b。
            try:
                raw = await asyncio.wait_for(
                    self._vision_chat(vpath, prompt, sysp), timeout=90
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[sticker_pack] 看图识别失败，这张不收: "
                    f"{type(e).__name__}: {e}"
                )
                return None
            info = self._parse_label_json(raw or "")
            if info:
                if not info["meaning"]:
                    info["meaning"] = self._fallback_label(text)["meaning"]
                logger.info(
                    f"[sticker_pack] 看图识别「{info['name']}」："
                    f"{info['meaning']}（{umo.rsplit(':', 1)[-1]}）"
                )
                return info
            return None
        finally:
            if vpath != image_path:
                try:
                    os.remove(vpath)
                except OSError:
                    pass
            await self._unload_vision()

    _VISION_MODEL = "qwen2.5vl:3b"
    _vision_probe = {"ts": 0.0, "model": ""}

    async def _vision_ready(self) -> bool:
        """本地 ollama 有没有能认图的模型；探测结果缓存 60 秒。"""
        now = time.time()
        probe = type(self)._vision_probe
        if now - float(probe.get("ts") or 0.0) < 60:
            return bool(probe.get("model"))

        def _run() -> str:
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:11434/api/tags", timeout=3
                ) as resp:
                    data = json.loads(resp.read().decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                return ""
            names = [str(m.get("name") or "") for m in (data.get("models") or [])]
            base = self._VISION_MODEL.split(":")[0]
            for name in names:
                if name.split(":")[0] == base:
                    return name
            # 指定的模型不在，就找任意带视觉的本地模型兜底
            for name in names:
                low = name.lower()
                if "vl" in low or "llava" in low or "minicpm-v" in low:
                    return name
            return ""

        model = await asyncio.to_thread(_run)
        type(self)._vision_probe = {"ts": now, "model": model}
        if not model:
            logger.info("[sticker_pack] 本地没有识图模型：新图改为待命名，等人工入库")
        return bool(model)

    async def _vision_chat(self, image_path: str, prompt: str, system_prompt: str = "") -> str:
        """直连本地 ollama 认图（模型用探测到的那个视觉模型）。"""
        model = type(self)._vision_probe.get("model") or self._VISION_MODEL

        def _run() -> str:
            with open(image_path, "rb") as f:
                img = base64.b64encode(f.read()).decode("ascii")
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt, "images": [img]})
            body = json.dumps(
                {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "think": False,  # qwen3.5 不关思考会只返回空 content
                    "keep_alive": "20s",
                    "options": {"temperature": 0.2, "num_predict": 160},
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/chat",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=85) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
            return ((data.get("message") or {}).get("content") or "").strip()

        return await asyncio.to_thread(_run)

    @staticmethod
    async def _unload_vision() -> None:
        """认完图就让 ollama 把视觉模型卸下：8G 显存里它和桌宠的 CosyVoice 抢卡。"""
        def _run() -> None:
            try:
                with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=4) as resp:
                    loaded = json.loads(resp.read().decode("utf-8") or "{}").get("models") or []
            except Exception:  # noqa: BLE001
                return
            for item in loaded:
                name = item.get("name") or item.get("model")
                if not name:
                    continue
                body = json.dumps({"model": name, "keep_alive": 0}).encode("utf-8")
                req = urllib.request.Request(
                    "http://127.0.0.1:11434/api/generate",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=6).read()
                except Exception:  # noqa: BLE001
                    pass

        try:
            await asyncio.to_thread(_run)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _parse_label_json(raw: str) -> dict:
        """从模型输出里抠 {"name","meaning"}；抠不出名字就返回空 dict。"""
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return {}
        try:
            info = json.loads(m.group(0))
        except ValueError:
            return {}
        name = str(info.get("name") or "").strip()
        if not name:
            return {}
        return {"name": name[:12], "meaning": str(info.get("meaning") or "").strip()[:60]}

    @staticmethod
    def _fallback_label(text: str) -> dict:
        """模型全失败时的兜底：拿群聊那句话当场合，从中截个短名。"""
        meaning = (text or "").strip()[:40] or "群聊里看到的图"
        name = re.sub(r"[^\w\u4e00-\u9fa5]", "", meaning)[:6] or "新收藏"
        return {"name": name, "meaning": meaning}
