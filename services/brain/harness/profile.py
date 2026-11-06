"""profile 常量与生成：package.json / cordis.yml / cordis.patch.yml。"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from roles import CREDENTIAL_API_KEY, Role

from .errors import HarnessError

# agent-loop 依赖 tools 服务本身，不能停；把「提供具体工具」的插件逐个关掉即可。
DISABLED_TOOL_IDS = (
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

PROFILE_PACKAGE = """{{
  "name": "dsh-profile-{profile}",
  "private": true,
  "dependencies": {{}},
  "dsh": {{
    "profile": {{
      "bundles": [
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-headless"
      ]
    }}
  }}
}}
"""

PROFILE_CORDIS = """# dsh profile root — 空入口列表，实际组合由 package.json 的 bundles + cordis.patch.yml 决定。
[]
"""


# ---------------------------------------------------------------- 人设
def strip_markdown_header(text: str) -> str:
    """人设文件里 `---` 之后的正文才是提示词；没有分隔线就整段用。"""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[i + 1 :]).strip()
    return "\n".join(lines).strip()


def load_persona(files: Tuple[Path, ...] | List[Path]) -> str:
    parts: List[str] = []
    for path in files:
        p = Path(path)
        if not p.is_file():
            raise HarnessError(f"人设文件不存在: {p}")
        parts.append(strip_markdown_header(p.read_text(encoding="utf-8-sig")))
    return "\n\n".join(x for x in parts if x)


def _yaml_block(text: str, indent: str = " " * 6) -> str:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body = "\n".join((indent + line.rstrip()) if line.strip() else "" for line in lines)
    return "|-\n" + body


def render_patch(role: Role, persona: str) -> str:
    """按角色生成 cordis.patch.yml：凭据 + 模型 + 人设 + 关闭全部工具。"""
    out: List[str] = []
    if role.credential.mode == CREDENTIAL_API_KEY:
        out += [
            "- id: llm-deepseek",
            "  config:",
            f"    apiKeyEnv: {role.credential.env}",
        ]
        provider = "deepseek-official"
    else:
        out += [
            "- id: credentials",
            "  disabled: false",
            "- id: authorization",
            "  disabled: false",
            "- id: deepseek-account",
            "  disabled: false",
        ]
        provider = "deepseek-account"

    out += [
        "- id: agent-default-model",
        "  config:",
        f"    provider: {provider}",
        f"    model: {role.model}",
        f"    reasoningEffort: {role.reasoning_effort}",
        "- id: system-prompt",
        "  config:",
        "    personaPrefix: " + _yaml_block(persona),
    ]
    for tool_id in DISABLED_TOOL_IDS:
        out += [f"- id: {tool_id}", "  disabled: true"]
    return "\n".join(out) + "\n"
