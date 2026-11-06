"""roles 包的常量：项目根、profile 名校验、凭据模式。"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

CREDENTIAL_ACCOUNT = "account"
CREDENTIAL_API_KEY = "api_key"
CREDENTIAL_MODES = (CREDENTIAL_ACCOUNT, CREDENTIAL_API_KEY)
