"""仓库内组件路径发现：先按约定位置找，找不到再按版本号搜索。

AstrBot 在仓库里按 `astrbot-win/AstrBot-<版本>/` 存放，上游升级后版本号会变；
这里统一按数字版本从高到低查找，代码和配置里就不必再写死某个版本。
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
HOME = Path.home()

_VERSION_RE = re.compile(r"\d+")
# 搜索同名目录时跳过大目录，避免扫 node_modules / 虚拟环境时卡住
_SEARCH_SKIP = {
    "node_modules", ".git", "venv", ".venv", "__pycache__", "site-packages", "data", "logs",
}


def version_key(name: str) -> tuple:
    """4.29.10 > 4.29.9：按数字段比较，而不是字符串比较。"""
    return tuple(int(x) for x in _VERSION_RE.findall(str(name))) or (0,)


def first_existing(paths: Sequence[Path]) -> Optional[Path]:
    for path in paths:
        if path.is_file():
            return path
    return None


def find_versioned_dir(parent: Path, prefix: str = "AstrBot") -> Optional[Path]:
    """返回 parent 下 `<prefix>*` 里版本号最大的目录，没有就返回 None。"""
    if not parent.is_dir():
        return None
    hits = [p for p in parent.iterdir() if p.is_dir() and p.name.startswith(prefix)]
    if not hits:
        return None
    return max(hits, key=lambda p: version_key(p.name[len(prefix):]))


def astrbot_dir(root: Path | None = None) -> Optional[Path]:
    """AstrBot 源码目录：仓库内优先，其次复用用户目录里已装的那一份。

    顺序：`<仓库>/third_party` → `<仓库>/astrbot-win` → `~/dafeiyu/{third_party,astrbot-win}`，
    都没有再在 `~/dafeiyu`、`~/workspace`、`~/Desktop` 下浅层搜索 `AstrBot*`，版本高者优先。
    """
    base = Path(root or REPO_ROOT)
    for parent in (
        base / "third_party",
        base / "astrbot-win",
        HOME / "dafeiyu" / "third_party",
        HOME / "dafeiyu" / "astrbot-win",
    ):
        hit = find_versioned_dir(parent)
        if hit and (hit / "main.py").is_file():
            return hit
    for parent in (HOME / "dafeiyu", HOME / "workspace", HOME / "Desktop"):
        for hit in _search_dirs(parent, "AstrBot", depth=2):
            if (hit / "main.py").is_file():
                return hit
    return None


def find_venv_python(root: Path | None = None) -> Optional[Path]:
    """AstrBot 虚拟环境的 python；固定位置没有时搜索任意版本目录下的 venv。"""
    base = Path(root or REPO_ROOT).resolve()
    fixed = first_existing([
        base / "astrbot-win" / "venv" / "Scripts" / "python.exe",
        base / "astrbot-win" / ".venv" / "Scripts" / "python.exe",
    ])
    bot = astrbot_dir(base)
    if bot is not None and base not in bot.resolve().parents:
        # AstrBot 来自仓库外：优先用它旁边的 venv，依赖才齐
        near = first_existing([
            bot.parent / "venv" / "Scripts" / "python.exe",
            bot.parent / ".venv" / "Scripts" / "python.exe",
            bot / "venv" / "Scripts" / "python.exe",
        ])
        if near:
            return near
    if fixed:
        return fixed
    hits = sorted(
        base.glob("astrbot-win/*/venv/Scripts/python.exe"),
        key=lambda p: version_key(p.parent.parent.parent.name),
        reverse=True,
    )
    if not hits:
        hits = sorted(
            base.glob("third_party/*/venv/Scripts/python.exe"),
            key=lambda p: version_key(p.parent.parent.parent.name),
            reverse=True,
        )
    return hits[0] if hits else None


def _search_dirs(parent: Path, name: str, depth: int = 2) -> list[Path]:
    """在 parent 下按目录名找，同名多份时版本号大的在前。"""
    pattern = re.compile(rf"{re.escape(name)}(?:[-_ .].*)?$", re.IGNORECASE)
    hits: list[Path] = []
    if not parent.is_dir():
        return hits
    base = len(parent.resolve().parts)
    for cur, dirs, _ in os.walk(parent):
        cur_path = Path(cur)
        dirs[:] = [d for d in dirs if d.lower() not in _SEARCH_SKIP]
        if len(cur_path.resolve().parts) - base >= depth:
            dirs[:] = []
            continue
        hits += [cur_path / d for d in dirs if pattern.match(d)]
    hits.sort(key=lambda p: version_key(p.name), reverse=True)
    return hits


def find_dir(
    names: Sequence[str], root: Path | None = None, depth: int = 2
) -> Optional[Path]:
    """按目录名查找第三方组件（seed-vc / CosyVoice / sing_ab 之类）。

    顺序：仓库约定位置 → `~/dafeiyu` → `~/workspace` → `~`；
    都没有就在 `~/dafeiyu`、`~/workspace`、`~/Desktop` 下浅层搜索，版本号大的优先。
    """
    base = Path(root or REPO_ROOT)
    for name in names:
        for cand in (
            base / name,
            base / "audio" / name,
            base / "tools" / name,
            base / "third_party" / name,
            HOME / "dafeiyu" / name,
            HOME / "workspace" / name,
            HOME / name,
        ):
            if cand.is_dir():
                return cand
    for name in names:
        for dirname in ("dafeiyu", "workspace", "Desktop"):
            hits = _search_dirs(HOME / dirname, name, depth)
            if hits:
                return hits[0]
    return None


def find_node(root: Path | None = None) -> Optional[str]:
    """NapCat 自带的 node.exe 优先，其次复用 ~/dafeiyu 里的，最后 PATH 里的 node。"""
    base = Path(root or REPO_ROOT)
    local = first_existing([
        base / "napcat-win" / "node.exe",
        base / "tools" / "windows" / "napcat-win" / "node.exe",
        HOME / "dafeiyu" / "napcat-win" / "node.exe",
    ])
    if local:
        return str(local)
    return shutil.which("node")


def resolve_versioned(path: Path, root: Path | None = None) -> Path:
    """把路径里的 `AstrBot*` 段换成实际存在的版本目录；不存在则原样返回。"""
    if path.exists():
        return path
    actual = astrbot_dir(root)
    if actual is None:
        return path
    parts = list(path.parts)
    for i, part in enumerate(parts):
        if part == "AstrBot" or part.startswith("AstrBot-"):
            parts[i] = actual.name
            candidate = Path(*parts)
            if candidate.exists():
                return candidate
    return path
