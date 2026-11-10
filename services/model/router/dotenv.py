"""读取 .env 与凭据环境变量。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List


def read_dotenv(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, OSError):
        return out
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def resolve_key(env_name: str, dotenv_paths: List[Path]) -> str:
    name = str(env_name or "").strip()
    if not name:
        return ""
    value = os.environ.get(name, "")
    if value:
        return value
    for path in dotenv_paths:
        value = read_dotenv(Path(path)).get(name, "")
        if value:
            return value
    return ""
