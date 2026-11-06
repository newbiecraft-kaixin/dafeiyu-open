"""providers.json 的加载与角色 → provider 解析。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .constants import CONFIG_PATH
from .errors import ModelError
from .route import Route


class Router:
    def __init__(self, path: str | Path = CONFIG_PATH) -> None:
        self.path = Path(path)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise ModelError(f"配置不存在: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise ModelError(f"配置不是合法 JSON: {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ModelError("providers.json 顶层必须是对象")
        self.raw = raw
        self.providers: Dict[str, Dict[str, Any]] = dict(raw.get("providers") or {})
        self.routes: Dict[str, Dict[str, Any]] = dict(raw.get("routes") or {})
        self.default_route: Dict[str, Any] = dict(raw.get("default_route") or {})
        self.fallbacks: List[str] = [str(x) for x in (raw.get("fallbacks") or [])]
        self.default_role = str(raw.get("default_role") or "")
        self.dotenv = [Path(x) for x in (raw.get("dotenv") or [])]

    # ------------------------------------------------------------ 解析
    def resolve(self, role: str, *, provider: str = "", model: str = "") -> Route:
        name = str(role or "").strip() or self.default_role
        rule = dict(self.routes.get(name) or self.default_route)
        provider_name = str(provider or rule.get("provider") or "").strip()
        if not provider_name:
            raise ModelError(f"角色 {name} 没有配置 provider")
        spec = dict(self.providers.get(provider_name) or {})
        if not spec:
            raise ModelError(f"未知 provider: {provider_name}")
        kind = str(spec.get("type") or "openai").strip().lower()
        if kind not in ("openai", "harness"):
            raise ModelError(f"provider {provider_name} 的 type 非法: {kind}")
        return Route(
            role=name,
            provider=provider_name,
            kind=kind,
            model=str(model or rule.get("model") or spec.get("model") or "").strip(),
            timeout_s=float(spec.get("timeout_s") or 120),
            api_key_env=str(rule.get("api_key_env") or spec.get("api_key_env") or "").strip(),
            base_url=str(spec.get("base_url") or "").strip(),
            dsh_root=str(spec.get("dsh_root") or "").strip(),
            dsh_home=str(spec.get("dsh_home") or "").strip(),
        )

    def public_providers(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name, spec in self.providers.items():
            out[name] = {
                "type": spec.get("type") or "openai",
                "base_url": spec.get("base_url") or "",
                "model": spec.get("model") or "",
                "credential_env": spec.get("api_key_env") or "",
            }
        return {"providers": out, "routes": dict(self.routes), "fallbacks": list(self.fallbacks)}
