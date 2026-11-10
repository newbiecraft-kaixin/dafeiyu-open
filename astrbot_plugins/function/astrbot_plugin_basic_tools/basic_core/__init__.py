"""basic_core：basic_tools 的功能域 mixin 子包。"""

from .reminder import ReminderMixin
from .schedule import ScheduleMixin
from .super_faces import SuperFaceMixin
from .tools import ToolsMixin
from .messaging import MessagingMixin
from .voice import VoiceMixin
from .music import MusicMixin
from .sing import SingMixin
from .cover import CoverMixin
from .avatar import AvatarMixin
from .admin import AdminMixin

__all__ = [
    "ReminderMixin",
    "ScheduleMixin",
    "SuperFaceMixin",
    "ToolsMixin",
    "MessagingMixin",
    "VoiceMixin",
    "MusicMixin",
    "SingMixin",
    "CoverMixin",
    "AvatarMixin",
    "AdminMixin",
]
