"""富媒体：表情/超级表情/表情包、黄脸池、学习同步与输出渲染。

按职责拆分（纯移动，逻辑不变）：
- _sticker_order.py  表情/梗图/超级表情点单与 AI 挑选
- _face_pool.py      黄脸池 / face_config 读取 / 表情学习与同步
- _render.py         输出渲染（占位符换图）/ 描述 / 人味清洗 / 斗图
"""

from ._sticker_order import MediaOrderMixin
from ._face_pool import MediaFacesMixin
from ._render import MediaRenderMixin


class MediaMixin(
    MediaOrderMixin,
    MediaFacesMixin,
    MediaRenderMixin,
):
    """组合各职责子 Mixin，保持 main.py 的导入路径不变。"""
