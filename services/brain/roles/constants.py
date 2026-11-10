"""roles 包的常量：项目根、profile 名校验、凭据模式。"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

CREDENTIAL_ACCOUNT = "account"
CREDENTIAL_API_KEY = "api_key"
CREDENTIAL_MODES = (CREDENTIAL_ACCOUNT, CREDENTIAL_API_KEY)

# 默认关闭的 DSH 工具插件 id。agent-loop 依赖 tools 服务本身，不能停；
# 把「提供具体工具」的插件逐个关掉即可。
# 可在 roles.json 顶层 tools.disabled 覆盖（缺省时用本默认集合）。
DEFAULT_DISABLED_TOOL_IDS = (
    "tool-plugin-manager",
    "tool-working-directory",
    "tool-pwsh",
    "tool-bash",
    "tool-jobs",
    "tool-fs",
    "tool-fs-search",
    "tool-skill",
    "tool-subagent",
    "tool-subagent-fork",
    "tool-subagent-control",
    "tool-subagent-list-agents",
    "tool-workflow",
    "tool-todo",
    "tool-goal",
    "tool-web",
    "mcp-resources",
    "agent-instructions",
    "skill",
    "skill-filesystem",
    "hmr",
)
