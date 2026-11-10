"""profile 常量与生成：package.json / cordis.yml / cordis.patch.yml。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from roles import CREDENTIAL_API_KEY, DEFAULT_DISABLED_TOOL_IDS, Role

from .errors import HarnessError

# agent-loop 依赖 tools 服务本身，不能停；把「提供具体工具」的插件逐个关掉即可。
# 实际生效的列表由 RolesConfig.disabled_tool_ids() 决定（roles.json tools.disabled），
# 这里保留默认集合与旧名字，供未配置时回退和外部导入。
DISABLED_TOOL_IDS = DEFAULT_DISABLED_TOOL_IDS

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


def drop_persona_sections(text: str, names: Optional[Sequence[str]] = None) -> str:
    """删掉以 `【名字】` 开头的整段；names 为空时原样返回（默认不裁剪）。"""
    wanted = {str(x).strip() for x in (names or []) if str(x).strip()}
    if not wanted or not text:
        return text
    out: List[str] = []
    skipping = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("【") and "】" in stripped:
            title = stripped[1 : stripped.index("】")]
            skipping = title in wanted
            if skipping:
                continue
        if not skipping:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _yaml_block(text: str, indent: str = " " * 6) -> str:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body = "\n".join((indent + line.rstrip()) if line.strip() else "" for line in lines)
    return "|-\n" + body


def render_patch(
    role: Role,
    persona: str,
    disabled_tool_ids: Optional[Iterable[str]] = None,
) -> str:
    """按角色生成 cordis.patch.yml：凭据 + 模型 + 人设 + 关闭配置里指定的工具。

    disabled_tool_ids 为 None 时回退到默认集合；显式传入空列表表示一个都不关。
    """
    tool_ids = list(DISABLED_TOOL_IDS if disabled_tool_ids is None else disabled_tool_ids)
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
    for tool_id in tool_ids:
        out += [f"- id: {tool_id}", "  disabled: true"]
    return "\n".join(out) + "\n"
