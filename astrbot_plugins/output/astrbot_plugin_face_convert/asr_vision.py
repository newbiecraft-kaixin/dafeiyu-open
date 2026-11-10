"""语音转写（字幕系统 / 本地 whisper / 云端 ASR）与画面描述（Ollama / 云端视觉）。"""

import asyncio
import base64
import json
import os
import re
import tempfile

import aiohttp

from astrbot.api import logger

from .video_analysis import timestamp

# 中枢「识图模型」：<data>/local_tools/vision.json，选了就优先用它描述画面
_VISION_CFG_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "local_tools", "vision.json"
    )
)
_vision_cfg_cache: tuple[float, str] = (0.0, "")


def _central_vision_provider() -> str:
    """读中枢识图配置（带 mtime 缓存，中枢改完不用重启）。"""
    global _vision_cfg_cache
    try:
        mtime = os.path.getmtime(_VISION_CFG_PATH)
        if mtime != _vision_cfg_cache[0]:
            with open(_VISION_CFG_PATH, encoding="utf-8-sig") as f:
                pid = str((json.load(f) or {}).get("provider_id") or "").strip()
            _vision_cfg_cache = (mtime, pid)
        return _vision_cfg_cache[1]
    except Exception:  # noqa: BLE001
        return ""


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
            # 离线优先：模型已在 HF 缓存时直接本地加载，
            # 避免断网/网络异常时 huggingface 在线校验抛 SSLError。
            try:
                self._whisper = WhisperModel(
                    "small", device="cpu", compute_type="int8", local_files_only=True
                )
            except Exception as e:
                logger.warning(f"[face_convert] 本地 whisper 缓存不可用({e})，回退联网下载")
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

    async def _vl_central(self, paths: list[str], prompt: str) -> str | None:
        """中枢配了识图模型就走它；没配/失败返回 None（不再回落本地 ollama）。"""
        pid = _central_vision_provider()
        if not pid or not paths:
            return None
        try:
            prov = self.context.get_provider_by_id(pid)
            if not prov:
                return None
            resp = await prov.text_chat(prompt=prompt, image_urls=paths)
            text = (getattr(resp, "completion_text", "") or "").strip()
            if text:
                logger.info(f"[face_convert] 中枢识图: {text[:60]}")
            return text or None
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[face_convert] 中枢识图失败: {type(e).__name__}: {str(e)[:80]}"
            )
            return None

    async def _vl_ollama(self, frames: list[tuple[float, str]]) -> str | None:
        """视频抽帧描述：只走中枢「识图模型」。

        识图模型没开启（vision.json 的 provider_id 为空）就不识别，也不连本地 ollama——
        9B 视觉模型一加载就是 6.3G，会把 CosyVoice 挤到十几秒一句。
        """
        if not _central_vision_provider():
            return None
        lines = []
        paths = []
        for at, path in frames:
            lines.append(f"[视频 {timestamp(at)} 的截图]")
            paths.append(path)
        prompt = (
            "这些截图按视频时间排列。请按给定时间戳描述人物、动作、场景变化和关键文字，"
            "每帧不超过80字。只描述截图能确认的事实，不补写帧间剧情，不要客套。"
        )
        return await self._vl_central(paths, prompt + "\n" + "\n".join(lines))

    @staticmethod
    def _vision_frames(path: str, tmpdir: str, limit: int = 3) -> list[str]:
        """动图抽帧转 PNG，静图原样返回。

        Ollama/DashScope 的看图接口不认 gif/webp 动图（直接喂会 400 invalid
        image），而只取首帧又会把「动」的部分丢掉，所以按时间均匀抽几帧一起送。
        """
        ext = os.path.splitext(path or "")[1].lower()
        if ext in (".png", ".jpg", ".jpeg"):
            return [path]
        try:
            from PIL import Image as PILImage
        except ImportError:
            return []
        try:
            with PILImage.open(path) as im:
                total = int(getattr(im, "n_frames", 1) or 1)
                picks = sorted({0, total // 2, total - 1}) if total > 1 else [0]
                frames: list[str] = []
                for i, idx in enumerate(picks[:limit]):
                    im.seek(idx)
                    out = os.path.join(tmpdir, f"vf_{i}.png")
                    im.convert("RGB").save(out, "PNG")
                    frames.append(out)
                return frames
        except Exception as e:  # noqa: BLE001
            logger.info(f"[face_convert] 动图抽帧失败: {type(e).__name__}")
            return []

    async def _vl_image(self, path: str, timeout: int = 25) -> str | None:
        """单张图片/动图 -> 一句话画面描述（只走中枢「识图模型」）。

        引用里的图常常没有文字（头像/自拍/实物），OCR 读不出东西，
        但引用是明确指向「看这张」，得让视觉模型说清画面，不能让模型瞎猜。
        识图模型没开启（vision.json 的 provider_id 为空）就不识别，也不连本地 ollama。
        gif/webp 动图先抽帧转 png，既避开接口 400，也不会只剩首帧。
        """
        if not _central_vision_provider():
            return None
        with tempfile.TemporaryDirectory(prefix="fg_") as ftmp:
            frames = self._vision_frames(path, ftmp)
            if not frames:
                return None
            lead = f"这是一个动图按时间顺序抽的 {len(frames)} 帧。" if len(frames) > 1 else ""
            central = await self._vl_central(
                frames,
                lead
                + "用一句中文说清这张图里是什么（谁/什么东西/什么场景/图上的字），"
                "不超过60字。只写画面里能确认的事实，不猜、不客套。",
            )
            if not central:
                return None
            central = " ".join(central.split())
            central = re.split(r"[。！？\n]", central)[0].strip()[:80]
            return central or None

    async def _vl_describe(self, frames: list[tuple[float, str]]) -> str | None:
        """每批至多四帧，带时间戳一起理解，保留场景变化（只走中枢「识图模型」）。"""
        if not _central_vision_provider():
            return None
        return await self._vl_ollama(frames)
