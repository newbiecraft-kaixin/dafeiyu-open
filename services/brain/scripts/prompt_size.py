#!/usr/bin/env python
"""只读统计 brain / DSH 每轮固定 prompt 的体积（不调用模型）。

用法（不会进入服务启动路径）：
    python scripts/prompt_size.py
    python scripts/prompt_size.py --brain-dir "C:\\Program Files\\FlatFish\\services\\brain" --dsh-home "%USERPROFILE%\\.dsh"

口径：
  - persona：人设文件 `---` 分隔线之后的正文，按字符计（UTF-8 字节 ≠ 字符）。
  - patch：~/.dsh/profiles/<profile>/cordis.patch.yml 的字符数。
  - tools：profile 组合后仍然启用的「注册工具」的 DSH 插件数；schema 字符为估算，
    由插件源码里的 name/description/参数说明拼出，非运行时序列化值。
  - plan_section：dsh-base 里 plan-mode 注入的静态策略段落字符数。
  - token：估算值，中文按 1.7 字符/token、其余按 4 字符/token。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

BASE_BUNDLE = Path("node_modules/@deepseek-ai/dsh-base/cordis.patch.yml")
PLAN_MODE_SRC = Path("node_modules/@deepseek-ai/dsh-plan-mode/lib/index.js")
HARNESS_IDENTITY = "You are an AI agent powered by DeepSeek Harness."


def est_tokens(text: str) -> float:
    """中文 1.7 字符/token，其余 4 字符/token（估算）。"""
    cjk = 0
    for ch in text:
        code = ord(ch)
        if 0x2E80 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
            cjk += 1
    other = len(text) - cjk
    return cjk / 1.7 + other / 4.0


def strip_header(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[i + 1:]).strip()
    return "\n".join(lines).strip()


def drop_sections(text: str, names: List[str]) -> str:
    """与 harness/profile.py 的 drop_persona_sections 同义（保持脚本可独立运行）。"""
    wanted = {str(x).strip() for x in (names or []) if str(x).strip()}
    if not wanted or not text:
        return text
    out: List[str] = []
    skipping = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("【") and "】" in stripped:
            title = stripped[1:stripped.index("】")]
            skipping = title in wanted
            if skipping:
                continue
        if not skipping:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def persona_stats(files: List[str], project_root: Path, drop: List[str]) -> Tuple[str, int]:
    """返回 (注入的 persona 正文, 原始文件字符数)。"""
    bodies: List[str] = []
    raw_chars = 0
    for item in files:
        path = Path(item)
        if not path.is_absolute():
            path = project_root / item
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        raw_chars += len(raw)
        bodies.append(strip_header(raw))
    return drop_sections("\n\n".join(bodies), drop), raw_chars


def iter_rows(text: str):
    """把 cordis 补丁里的 `- id:` 条目切成 (id, 块文本)。"""
    rows: List[Tuple[str, str]] = []
    current_id = ""
    buf: List[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^\s*-\s+id:\s*(\S+)\s*$", line)
        if m:
            if current_id:
                rows.append((current_id, "\n".join(buf)))
            current_id = m.group(1)
            buf = [line]
        elif current_id:
            buf.append(line)
    if current_id:
        rows.append((current_id, "\n".join(buf)))
    return rows


def row_name(block: str) -> str:
    m = re.search(r"^\s*name:\s*'([^']+)'", block, re.M)
    return m.group(1) if m else ""


def row_disabled(block: str) -> bool:
    if re.search(r"^\s*disabled:\s*true\s*$", block, re.M):
        return True
    if re.search(r"^\s*disabled:\s*!!js\s+process\.platform\s*===?\s*'win32'", block, re.M):
        return sys.platform == "win32"
    if re.search(r"^\s*disabled:\s*!!js\s+process\.platform\s*!==?\s*'win32'", block, re.M):
        return sys.platform != "win32"
    return False


def patch_disabled(patch_text: str) -> set:
    return {rid for rid, block in iter_rows(patch_text) if row_disabled(block)}


def base_rows(dsh_root: Path) -> Dict[str, str]:
    path = dsh_root / BASE_BUNDLE
    if not path.is_file():
        return {}
    return dict(iter_rows(path.read_text(encoding="utf-8-sig")))


def plan_section_chars(base_text: str) -> int:
    m = re.search(r"-\s+id:\s*plan-mode\b.*?^\s*section:\s*\|-?\s*$", base_text, re.M | re.S)
    if not m:
        return 0
    tail = base_text[m.end():]
    lines: List[str] = []
    for line in tail.split("\n")[1:]:
        if re.match(r"^\s*-\s+id:", line):
            break
        lines.append(line)
    indent = min((len(x) - len(x.lstrip()) for x in lines if x.strip()), default=0)
    return len("\n".join(x[indent:].rstrip() for x in lines).strip("\n"))


def exit_plan_mode_schema_chars(dsh_root: Path) -> int:
    path = dsh_root / PLAN_MODE_SRC
    if not path.is_file():
        return 0
    src = path.read_text(encoding="utf-8-sig")
    desc = re.search(r'const EXIT_DESCRIPTION = "([^"]*)"', src)
    param = re.search(r'description:\s*"(The complete plan[^"]*)"', src)
    if not desc or not param:
        return 0
    return len("exit_plan_mode") + len(desc.group(1)) + len(param.group(1)) + 80


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--brain-dir", default=str(here.parent))
    parser.add_argument("--dsh-home", default=os.environ.get("DSH_HOME") or str(Path.home() / ".dsh"))
    parser.add_argument("--no-persona-drop", action="store_true", help="忽略 harness.persona_drop_sections（用于复现改动前口径）")
    parser.add_argument("--patch-suffix", default="", help="读 cordis.patch.yml<后缀>，用于对比备份文件")
    args = parser.parse_args()

    brain = Path(args.brain_dir)
    dsh_home = Path(args.dsh_home)
    import json

    raw = json.loads((brain / "roles.json").read_text(encoding="utf-8-sig"))
    project_root = brain.parents[1]
    dsh_root = Path(raw.get("harness", {}).get("dsh_root") or "dsh")
    if not dsh_root.is_absolute():
        dsh_root = project_root / dsh_root

    base = base_rows(dsh_root)
    base_text = (dsh_root / BASE_BUNDLE).read_text(encoding="utf-8-sig") if (dsh_root / BASE_BUNDLE).is_file() else ""
    plan_chars = plan_section_chars(base_text)
    plan_tool_chars = exit_plan_mode_schema_chars(dsh_root)

    tool_rows = {}
    for rid, block in base.items():
        name = row_name(block)
        if name == "@deepseek-ai/dsh-tool-call-timeout-policy" or name == "@deepseek-ai/dsh-tools":
            continue
        if name.startswith("@deepseek-ai/dsh-tool") or name.endswith("/tools") or name == "@deepseek-ai/dsh-plan-mode":
            tool_rows[rid] = (name, row_disabled(block))

    print(f"brain={brain}")
    print(f"dsh_root={dsh_root}")
    print(f"dsh_home={dsh_home}")
    print("token 为估算：中文 1.7 字符/token，其余 4 字符/token\n")

    header = ("role", "persona字符", "patch字符", "工具数", "工具schema字符", "plan段字符", "固定前缀字符", "估算token")
    print("{:<8}{:>12}{:>12}{:>8}{:>16}{:>12}{:>14}{:>12}".format(*header))
    for role_name, role in raw.get("roles", {}).items():
        files = role.get("persona_files") or []
        drop = [] if args.no_persona_drop else list((raw.get("harness") or {}).get("persona_drop_sections") or [])
        persona_text, _ = persona_stats(files, project_root, drop)
        body_chars = len(persona_text)
        patch_path = dsh_home / "profiles" / role["profile"] / ("cordis.patch.yml" + args.patch_suffix)
        patch_text = patch_path.read_text(encoding="utf-8-sig") if patch_path.is_file() else ""
        disabled = patch_disabled(patch_text)

        enabled: List[str] = []
        tool_chars = 0
        for rid, (name, platform_off) in tool_rows.items():
            if platform_off or rid in disabled:
                continue
            enabled.append(rid)
            if name == "@deepseek-ai/dsh-plan-mode":
                tool_chars += plan_tool_chars
        plan_chars_here = plan_chars if "plan-mode" not in disabled else 0
        fixed = len(HARNESS_IDENTITY) + body_chars + tool_chars + plan_chars_here
        tokens = est_tokens(persona_text) + (len(HARNESS_IDENTITY) + tool_chars + plan_chars_here) / 4.0
        print("{:<8}{:>12}{:>12}{:>8}{:>16}{:>12}{:>14}{:>12.0f}".format(
            role_name, body_chars, len(patch_text), len(enabled), tool_chars,
            plan_chars_here, fixed, tokens,
        ))

    print("\n说明：工具 schema 字符只统计可从插件源码常量取到的部分（当前仅 exit_plan_mode 估算 "
          f"{plan_tool_chars} 字符）；plan 段来自 dsh-base 的 plan-mode 配置 {plan_chars} 字符。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
