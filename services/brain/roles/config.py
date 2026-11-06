"""roles.json 的加载、校验与查询。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .constants import (
    CREDENTIAL_ACCOUNT,
    CREDENTIAL_API_KEY,
    CREDENTIAL_MODES,
    PROFILE_RE,
    PROJECT_ROOT,
)
from .errors import RolesError
from .models import Credential, Identity, Role, Route
from .util import _resolve_path, _s, _texts


class RolesConfig:
    def __init__(
        self,
        raw: Dict[str, Any],
        *,
        project_root: Path = PROJECT_ROOT,
        source: Optional[Path] = None,
    ) -> None:
        self.raw = raw
        self.project_root = project_root
        self.source = source
        self.roles: Dict[str, Role] = {}
        self.identities: List[Identity] = []
        self.platform_defaults: Dict[str, str] = {}
        self.admins: Dict[str, List[str]] = {}
        self.default_role = ""
        self.harness: Dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------ 构造
    @classmethod
    def from_file(cls, path: str | Path, project_root: Optional[Path] = None) -> "RolesConfig":
        p = Path(path).expanduser().resolve()
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise RolesError(f"roles 配置不存在: {p}") from exc
        except json.JSONDecodeError as exc:
            raise RolesError(f"roles 配置不是合法 JSON: {p}: {exc}") from exc
        if not isinstance(raw, dict):
            raise RolesError("roles 配置顶层必须是对象")
        root = project_root or p.parent.parent.parent
        return cls(raw, project_root=Path(root), source=p)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], *, project_root: Path = PROJECT_ROOT) -> "RolesConfig":
        return cls(raw, project_root=project_root)

    # ------------------------------------------------------------ 解析
    def _load(self) -> None:
        roles_raw = self.raw.get("roles")
        if not isinstance(roles_raw, dict) or not roles_raw:
            raise RolesError("roles 不能为空")

        for name, item in roles_raw.items():
            if not isinstance(item, dict):
                raise RolesError(f"角色 {name} 必须是对象")
            profile = _s(item.get("profile")) or f"brain-{name}"
            if not PROFILE_RE.match(profile):
                raise RolesError(f"角色 {name} 的 profile 名非法: {profile!r}")

            cred_raw = item.get("credential") or {}
            if isinstance(cred_raw, str):
                cred_raw = {"mode": cred_raw}
            if not isinstance(cred_raw, dict):
                raise RolesError(f"角色 {name} 的 credential 非法")
            mode = _s(cred_raw.get("mode") or CREDENTIAL_ACCOUNT)
            if mode not in CREDENTIAL_MODES:
                raise RolesError(f"角色 {name} 的 credential.mode 非法: {mode!r}")
            env = _s(cred_raw.get("env"))
            if mode == CREDENTIAL_API_KEY and not env:
                raise RolesError(f"角色 {name} 使用 api_key 模式，必须填 credential.env")

            persona_raw = item.get("persona_files", item.get("persona"))
            files = tuple(_resolve_path(x, self.project_root) for x in _texts(persona_raw))

            ws_raw = _s(item.get("workspace"))
            workspace = (
                _resolve_path(ws_raw, self.project_root)
                if ws_raw
                else self.project_root / "services" / "brain" / "workspaces" / profile
            )

            self.roles[name] = Role(
                name=name,
                display=_s(item.get("display")) or name,
                profile=profile,
                credential=Credential(mode=mode, env=env),
                model=_s(item.get("model")) or "deepseek-flash",
                reasoning_effort=_s(item.get("reasoning_effort")) or "high",
                persona_files=files,
                workspace=workspace,
            )

        profiles = [r.profile for r in self.roles.values()]
        if len(profiles) != len(set(profiles)):
            raise RolesError("不同角色不能共用同一个 profile")

        default = _s(self.raw.get("default_role")) or next(iter(self.roles))
        if default not in self.roles:
            raise RolesError(f"default_role 不存在: {default}")
        self.default_role = default

        defaults = self.raw.get("platform_defaults") or {}
        if not isinstance(defaults, dict):
            raise RolesError("platform_defaults 必须是对象")
        for platform, role in defaults.items():
            if _s(role) not in self.roles:
                raise RolesError(f"platform_defaults[{platform}] 指向不存在的角色: {role}")
            self.platform_defaults[_s(platform).lower()] = _s(role)

        admins = self.raw.get("admins") or {}
        if not isinstance(admins, dict):
            raise RolesError("admins 必须是对象")
        for platform, ids in admins.items():
            self.admins[_s(platform).lower()] = [x for x in (_s(v) for v in _texts(ids)) if x]

        for i, item in enumerate(self.raw.get("identities") or []):
            if not isinstance(item, dict):
                raise RolesError(f"identities[{i}] 必须是对象")
            role = _s(item.get("role"))
            if role not in self.roles:
                raise RolesError(f"identities[{i}] 指向不存在的角色: {role}")
            ident = Identity(
                role=role,
                platform=_s(item.get("platform")).lower() or "*",
                user_id=_s(item.get("user_id")),
                name=_s(item.get("name")),
                chat_id=_s(item.get("chat_id")),
                note=_s(item.get("note")),
            )
            if not ident.user_id and not ident.name:
                raise RolesError(f"identities[{i}] 至少要填 user_id 或 name")
            self.identities.append(ident)

        harness = self.raw.get("harness") or {}
        if not isinstance(harness, dict):
            raise RolesError("harness 必须是对象")
        self.harness = harness

    # ------------------------------------------------------------ 查询
    def role(self, name: str) -> Role:
        role = self.roles.get(_s(name))
        if role is None:
            raise RolesError(f"未知角色: {name}")
        return role

    def resolve(
        self,
        *,
        platform: str,
        user_id: Any = "",
        name: Any = "",
        chat_id: Any = "",
    ) -> Route:
        platform = _s(platform).lower()
        uid, uname, cid = _s(user_id), _s(name), _s(chat_id)

        best: Optional[Identity] = None
        best_score = 0
        for ident in self.identities:
            score = ident.matches(platform, uid, uname, cid)
            if score > best_score:
                best, best_score = ident, score

        if best is not None:
            return Route(
                role=self.roles[best.role],
                reason=f"identity:user_id={best.user_id or '-'},name={best.name or '-'}"
                + (f",chat={best.chat_id}" if best.chat_id else ""),
                is_admin=self.is_admin(platform, uid, uname),
                identity=best,
            )

        if platform in self.platform_defaults:
            role_name = self.platform_defaults[platform]
            return Route(
                role=self.roles[role_name],
                reason=f"platform_default:{platform}",
                is_admin=self.is_admin(platform, uid, uname),
            )

        return Route(
            role=self.roles[self.default_role],
            reason="default_role",
            is_admin=self.is_admin(platform, uid, uname),
        )

    def is_admin(self, platform: str, user_id: Any = "", name: Any = "") -> bool:
        keys: Iterable[str] = []
        for p in (_s(platform).lower(), "*"):
            keys = list(keys) + self.admins.get(p, [])
        candidates = [x for x in (_s(user_id), _s(name)) if x]
        return any(c in keys for c in candidates)

    # ------------------------------------------------------------ 展示
    def summary(self) -> Dict[str, Any]:
        return {
            "default_role": self.default_role,
            "platform_defaults": dict(self.platform_defaults),
            "admins": {k: len(v) for k, v in self.admins.items()},
            "roles": [r.as_public(self.project_root) for r in self.roles.values()],
            "identities": [
                {
                    "platform": i.platform,
                    "user_id": i.user_id,
                    "name": i.name,
                    "chat_id": i.chat_id,
                    "role": i.role,
                    "note": i.note,
                }
                for i in self.identities
            ],
            "harness": {
                "dsh_root": _s(self.harness.get("dsh_root")),
                "timeout_s": self.harness.get("timeout_s", 180),
                "max_parallel": self.harness.get("max_parallel", 3),
            },
        }

    # ------------------------------------------------------------ harness 设置
    def harness_setting(self, key: str, default: Any = None) -> Any:
        return self.harness.get(key, default)

    def dsh_root(self) -> Path:
        raw = _s(self.harness.get("dsh_root")) or str(self.project_root / "dsh")
        return _resolve_path(raw, self.project_root)

    def dsh_home(self) -> Path:
        raw = _s(self.harness.get("dsh_home"))
        if raw:
            return _resolve_path(raw, self.project_root)
        env = _s(os.environ.get("DSH_HOME"))
        return Path(env).expanduser() if env else Path.home() / ".dsh"

    def dotenv_paths(self) -> List[Path]:
        return [_resolve_path(x, self.project_root) for x in _texts(self.harness.get("dotenv"))]
