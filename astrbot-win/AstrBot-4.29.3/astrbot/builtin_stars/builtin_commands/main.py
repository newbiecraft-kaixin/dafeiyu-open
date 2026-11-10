from astrbot.api import star


class Main(star.Star):
    """内置指令全部下线。

    原来的 /sid /name /reset /stop /new /stats /provider /dashboard_update
    /set /unset 要么被本项目的插件架空（provider_router 每条消息覆盖 Provider、
    speaker_tag 的档案不响应 /reset），要么回复是英文噪音、要么无权限还能被群友
    写会话变量。留着只会误导用户，所以整组删掉。
    """

    def __init__(self, context: star.Context) -> None:
        self.context = context
