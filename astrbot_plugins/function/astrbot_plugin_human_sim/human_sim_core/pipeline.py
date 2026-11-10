"""消息流水线：记录、去重、守卫、决策与生成。

按职责拆分（纯移动，逻辑不变）：
- _listen.py  私聊记录 / 监听窗口 / 拉黑共享存储
- _record.py  群消息收集 / 指令识别 / +1 接龙 / 复读分发
- _guards.py  出站守卫（去重 / 过时丢弃 / 禁言额度 / 认人纠正 / 指令排队 / 回复入史）
- _decide.py  AI 自主决策与生成（决策 prompt / 认人块 / REACT 表情 / 沉默逻辑）
"""

from ._listen import PipelineListenMixin
from ._record import PipelineRecordMixin
from ._guards import PipelineGuardsMixin
from ._decide import PipelineDecideMixin


class PipelineMixin(
    PipelineListenMixin,
    PipelineRecordMixin,
    PipelineGuardsMixin,
    PipelineDecideMixin,
):
    """组合各职责子 Mixin，保持 main.py 的导入路径不变。"""
