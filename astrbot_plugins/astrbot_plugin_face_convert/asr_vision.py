"""语音转写（字幕系统 / 本地 whisper / 云端 ASR）与画面描述（Ollama / 云端视觉）。"""

import asyncio
import base64
import os
import re

import aiohttp

from astrbot.api import logger

from .video_analysis import timestamp


class AsrVisionMixin:
    @staticmethod
    def _extract_text(resp) -> str | None:
        """从 DashScope 多模态返回里抽出文本。"""
        try:
            def get(obj, key):
                return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

            out = get(resp, "output")
            choices = (get(out, "choices") or []) if out else []
            for ch in choices:
                msg = get(ch, "message")
                content = get(msg, "content") if msg else None
                if isinstance(content, str):
                    return content.strip() or None
                if isinstance(content, list):
                    texts = [str(item["text"]).strip() for item in content
                             if isinstance(item, dict) and item.get("text")]
                    if texts:
                        return "\n".join(texts)
        except Exception:
            pass
        return None

    async def _mm_call(self, model: str, content_items: list, timeout: int = 60) -> str | None:
        """可取消的 DashScope HTTP 请求，避免超时后 SDK 工作线程继续上传。"""
        if not self.dashscope_key:
            return None
        try:
            import dashscope
            base_url = dashscope.base_http_api_url
        except ImportError:
            base_url = "https://dashscope.aliyuncs.com/api/v1"
        payload = {
            "model": model,
            "input": {"messages": [{"role": "user", "content": content_items}]},
            "parameters": {"result_format": "message"},
        }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=True
        ) as session:
            async with session.post(
                base_url.rstrip("/") + "/services/aigc/multimodal-generation/generation",
                headers={"Authorization": f"Bearer {self.dashscope_key}"},
                json=payload,
            ) as resp:
                resp.raise_for_status()
                return self._extract_text(await resp.json())

    @staticmethod
    def _media_data_url(path: str, mime: str) -> str:
        # 留出 base64 的 4/3 膨胀空间，编码后低于 ASR 的 10 MB 输入上限。
        with open(path, "rb") as f:
            data = f.read(7_000_001)
        if len(data) > 7_000_000:
            raise ValueError("单段媒体过大，请先切段或压缩")
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    def _asr_local_sync(self, path: str) -> str | None:
        """同步本地转写（faster-whisper int8，放线程池跑）。
        音频解码走 ffmpeg + numpy（av 19 与 faster-whisper 不兼容，绕开）。"""
        import subprocess
        import wave as _wave

        import numpy as np

        if self._whisper is None:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel("small", device="cpu", compute_type="int8")
            logger.info("[face_convert] 本地 faster-whisper(small/int8) 引擎已就绪")
        wav_tmp = path + ".16k.wav"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-ar", "16000", "-ac", "1", wav_tmp],
                capture_output=True, timeout=120,
            )
            with _wave.open(wav_tmp, "rb") as w:
                audio = np.frombuffer(
                    w.readframes(w.getnframes()), dtype=np.int16
                ).astype(np.float32) / 32768.0
        finally:
            try:
                if os.path.exists(wav_tmp):
                    os.remove(wav_tmp)
            except OSError:
                pass
        if audio.size == 0:
            return None
        segments, _info = self._whisper.transcribe(
            audio, language="zh", beam_size=1, vad_filter=True,
            initial_prompt="以下是普通话对话，使用简体中文。",
        )
        text = "".join(s.text for s in segments).strip()
        return text or None

    async def _subtitle_call(self, kind: str, path: str, timeout: int) -> str | None:
        """调音频字幕系统（services/subtitle）的 /asr 或 /vision；服务未开返回 None。"""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
            ) as sess:
                async with sess.post(
                    f"{self.subtitle_url}/{kind}", json={"path": path}
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
            if not data.get("ok"):
                return None
            text = ((data.get("data") or {}).get("text") or "").strip()
            return text or None
        except Exception as e:  # noqa: BLE001
            logger.info(f"[face_convert] 音频字幕系统不可用（{kind}）: {type(e).__name__}")
            return None

    async def _asr_file(self, path: str) -> str | None:
        """本地音频文件 -> 文字：音频字幕系统优先，失败再走插件内本地 whisper，最后云端"""
        text = await self._subtitle_call("asr", path, self.cfg["asr_timeout_s"] + 90)
        if text:
            logger.info(f"[face_convert] 字幕系统 ASR: {text[:60]}")
            return text
        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, self._asr_local_sync, path)
            if text:
                logger.info(f"[face_convert] 本地 ASR: {text[:60]}")
                return text
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[face_convert] 本地 ASR 失败: {type(e).__name__}: {str(e)[:120]}")
        if self.dashscope_key:
            return await self._asr_file_cloud(path)
        return None

    async def _asr_file_cloud(self, path: str) -> str | None:
        """本地音频文件 -> 文字（qwen3-asr-flash，云端）。"""
        return await self._mm_call(
            "qwen3-asr-flash",
            [{"audio": self._media_data_url(
                path, "audio/wav" if path.lower().endswith(".wav") else "audio/mpeg"
            )}],
            timeout=self.cfg["asr_timeout_s"],
        )

    async def _vl_ollama(self, frames: list[tuple[float, str]]) -> str | None:
        """本地视觉：Ollama qwen3.5 原生 vision，帧图 base64 直喂（免费）。"""
        try:
            lines = []
            images = []
            for at, path in frames:
                lines.append(f"[视频 {timestamp(at)} 的截图]")
                with open(path, "rb") as f:
                    images.append(base64.b64encode(f.read()).decode("ascii"))
            prompt = (
                "这些截图按视频时间排列。请按给定时间戳描述人物、动作、场景变化和关键文字，"
                "每帧不超过80字。只描述截图能确认的事实，不补写帧间剧情，不要客套。"
            )
            payload = {
                "model": self.ollama_vision_model,
                # 显存只有 8G：9B 视觉模型占约 6.5G，和桌宠的 CosyVoice 抢卡。
                # 看完 20 秒就让它卸载，别一直压着显存把语音合成挤崩。
                "keep_alive": "20s",
                "messages": [
                    {"role": "user", "content": prompt + "\n" + "\n".join(lines), "images": images}
                ],
                "stream": False,
                "think": False,  # qwen3.5 是思考模型：不关的话 num_predict 全被思考吃掉，content 返回空
                "options": {"temperature": 0.3, "num_predict": 500},
            }
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.cfg["vl_timeout_s"] + 60),
                trust_env=False,
            ) as sess:
                async with sess.post(
                    self.ollama_base + "/api/chat", json=payload
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            text = ((data.get("message") or {}).get("content") or "").strip()
            if text:
                logger.info(f"[face_convert] 本地视觉描述: {text[:60]}")
            return text or None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[face_convert] 本地视觉失败: {type(e).__name__}: {str(e)[:100]}")
            return None

    async def _vl_image(self, path: str, timeout: int = 25) -> str | None:
        """单张图片 -> 一句话画面描述（本地 Ollama vision，免费）。

        引用里的图常常没有文字（头像/自拍/实物），OCR 读不出东西，
        但引用是明确指向「看这张」，得让视觉模型说清画面，不能让模型瞎猜。
        """
        text = await self._subtitle_call("vision", path, timeout + 20)
        if text:
            logger.info(f"[face_convert] 字幕系统画面描述: {text[:70]}")
            return text
        try:
            with open(path, "rb") as f:
                img = base64.b64encode(f.read()).decode("ascii")
            payload = {
                "model": self.ollama_vision_model,
                "keep_alive": "20s",  # 见上：别长占显存，给语音合成留地方
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "用一句中文说清这张图里是什么（谁/什么东西/什么场景/图上的字），"
                            "不超过60字。只写画面里能确认的事实，不猜、不客套。"
                        ),
                        "images": [img],
                    }
                ],
                "stream": False,
                "think": False,
                "options": {"temperature": 0.2, "num_predict": 120},
            }
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                trust_env=False,
            ) as sess:
                async with sess.post(
                    self.ollama_base + "/api/chat", json=payload
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            text = ((data.get("message") or {}).get("content") or "").strip()
            # 小模型爱展开写，只留第一句、压到 80 字内，别把上下文撑爆
            text = " ".join(text.split())
            text = re.split(r"[。！？\n]", text)[0].strip()[:80] if text else ""
            logger.info(f"[face_convert] 单图画面描述: {text[:70]}")
            return text or None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[face_convert] 单图视觉失败: {type(e).__name__}: {str(e)[:80]}")
            return None

    async def _vl_describe(self, frames: list[tuple[float, str]]) -> str | None:
        """每批至多四帧，带时间戳一起理解，保留场景变化。本地 Ollama 视觉优先，云端兜底。"""
        local = await self._vl_ollama(frames)
        if local:
            return local
        content = []
        for at, path in frames:
            content.extend([
                {"text": f"视频 {timestamp(at)} 的截图："},
                {"image": self._media_data_url(path, "image/jpeg")},
            ])
        content.append({"text": (
            "这些截图按视频时间排列。请按给定时间戳描述人物、动作、场景变化和关键文字，"
            "每帧不超过80字。只描述截图能确认的事实，不补写帧间剧情，不要客套。"
        )})
        return await self._mm_call(
            "qwen3-vl-plus", content, timeout=self.cfg["vl_timeout_s"],
        )
