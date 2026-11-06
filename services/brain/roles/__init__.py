"""角色与身份：一个角色 = 一套 DSH profile + 一份凭据 + 一组人设文件。

roles.json 只写「用哪个凭据」（account 或 api_key 对应的环境变量名），
不写密钥明文；密钥从进程环境或 .env 读取，由 harness 注入子进程。

身份解析优先级（高 → 低）：
    1. chat 绑定 + user_id 精确
    2. user_id 精确
    3. chat 绑定 + 昵称精确
    4. 昵称精确
    5. platform_defaults[platform]
    6. default_role

用法：
    cfg = RolesConfig.from_file("roles.json")
    route = cfg.resolve(platform="qq", user_id="123", name="主人")
"""
from __future__ import annotations

from .constants import (
    CREDENTIAL_ACCOUNT,
    CREDENTIAL_API_KEY,
    CREDENTIAL_MODES,
    PROFILE_RE,
    PROJECT_ROOT,
)
from .config import RolesConfig
from .errors import RolesError
from .models import Credential, Identity, Role, Route

__all__ = [
    "CREDENTIAL_ACCOUNT",
    "CREDENTIAL_API_KEY",
    "CREDENTIAL_MODES",
    "PROFILE_RE",
    "PROJECT_ROOT",
    "Credential",
    "Identity",
    "Role",
    "Route",
    "RolesConfig",
    "RolesError",
]
