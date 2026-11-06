"""一次调度解析出的路由信息。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class Route:
    role: str
    provider: str
    kind: str            # openai | harness
    model: str
    timeout_s: float
    api_key_env: str
    base_url: str = ""
    dsh_root: str = ""
    dsh_home: str = ""

    def as_public(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "provider": self.provider,
            "type": self.kind,
            "model": self.model,
            "base_url": self.base_url,
            "credential_env": self.api_key_env,
            "timeout_s": self.timeout_s,
        }
