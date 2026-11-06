"""把 LLM 回复中的 [表情名] 文字自动转换成真实 QQ 表情段。

原理：QQ 只在"人手输入"时把 [旺柴] 这类文字渲染成表情，
机器人发出的纯文本接收端只会看到字面量。本插件在消息发送前
把 [名字] 替换为真正的 face 消息段（NapCat / OneBot v11）。
字典来源：NapCat 内置 qface 表 + 社区 315 条 sysface 快照合并（414 条）。
"""

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .asr_vision import AsrVisionMixin
from .inbound import InboundMixin
from .links import LinkMixin
from .media import MediaMixin
from .ocr import OcrMixin
from .outbound import OutboundMixin
from .settings import DEFAULT_CONFIG, SettingsMixin
from .text_clean import TextCleanMixin
from .utils import (  # noqa: F401  保持原有模块级接口可转导出
    BILI_HOST_RE,
    MAX_REPLACE,
    PLUGIN_DIR,
    URL_RE,
    VIDEO_HOST_RE,
    _clean_url,
    _strip_site_suffix,
    download_capped,
    file_uri_to_path,
    run_bounded,
)
from .video_analysis import VideoAnalysis, frame_times, timestamp  # noqa: F401


@register(
    "astrbot_plugin_face_convert",
    "dafeiyu-tools",
    "把 LLM 回复中的 [流泪][doge] 等文字表情自动转换为真实 QQ 表情段",
    "1.2.0",
)
class FaceConvertPlugin(
    SettingsMixin,
    LinkMixin,
    MediaMixin,
    AsrVisionMixin,
    OcrMixin,
    TextCleanMixin,
    InboundMixin,
    OutboundMixin,
    Star,
):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._load_face_dictionaries()
        self._load_runtime_config()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def inbound_face_to_text(self, event: AstrMessageEvent):
        """把收到的 QQ 表情翻译成 [名字] 文本。

        纯表情消息的 message_str 为空，上游会当"空消息"处理导致她装死不回；
        这里把表情名拼进文本，让整条链路正常运转。
        """
        await self._handle_inbound_faces(event)

    @filter.on_llm_request()
    async def inject_owner_rule(self, event: AstrMessageEvent, req) -> None:
        """给非主人的会话追加一条身份规则，说清谁才是主人。"""
        await self._inject_owner_rule(event, req)

    @filter.on_llm_response()
    async def scrub_drafts(self, event: AstrMessageEvent, response) -> None:
        """发送前的三道橡皮擦：①草稿箱（双层括号）②嘴硬小尾巴 ③meta 口癖句。"""
        await self._scrub_drafts_impl(event, response)

    @filter.on_decorating_result()
    async def convert_faces(self, event: AstrMessageEvent):
        await self._convert_faces_impl(event)
