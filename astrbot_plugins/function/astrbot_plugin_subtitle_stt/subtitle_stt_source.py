"""本地字幕服务（services/subtitle）语音转写适配器。

把语音消息的本地音频路径交给本机 subtitle 服务（默认 23405 端口）转写，
底层为 faster-whisper CPU int8，不产生云端费用。
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

from astrbot.api.provider import STTProvider
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.register import register_provider_adapter


@register_provider_adapter(
    "subtitle_stt",
    "本地字幕服务语音转写",
    provider_type=ProviderType.SPEECH_TO_TEXT,
)
class ProviderSubtitleSTT(STTProvider):
    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.api_base = str(
            provider_config.get("api_base") or "http://127.0.0.1:23405"
        ).rstrip("/")
        self.timeout = float(provider_config.get("timeout") or 120)
        self.set_model(str(provider_config.get("model") or "faster-whisper"))

    def _transcribe(self, audio_url: str) -> str:
        is_remote = audio_url.startswith(("http://", "https://"))
        payload = json.dumps(
            {"url" if is_remote else "path": audio_url},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base}/asr",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(str(body.get("error") or "字幕服务转写失败"))
        return str((body.get("data") or {}).get("text") or "").strip()

    async def get_text(self, audio_url: str) -> str:
        return await asyncio.to_thread(self._transcribe, audio_url)
