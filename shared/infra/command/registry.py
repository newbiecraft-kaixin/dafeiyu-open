"""指令注册表：注册、插件目录加载、查询与菜单渲染。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .constants import PLATFORM_ALL
from .context import CommandContext, parse_command
from .spec import Command, Resolution


class CommandRegistry:
    def __init__(self) -> None:
        # name -> {scope: Command}；all/qq/wechat 可以各自有同名指令（如 /禁言）
        self._commands: Dict[str, Dict[str, Command]] = {}
        self._aliases: Dict[str, Dict[str, str]] = {}

    @staticmethod
    def _pick(bucket: Dict[str, Any], platform: str = "", strict: bool = False) -> Any:
        if platform and platform in bucket:
            return bucket[platform]
        if PLATFORM_ALL in bucket:
            return bucket[PLATFORM_ALL]
        if strict:
            return None
        return next(iter(bucket.values()), None)

    # ---- 注册 ----
    def register(self, command: Command) -> Command:
        key = command.name.lower()
        if not key:
            raise ValueError("指令名不能为空")
        bucket = self._commands.setdefault(key, {})
        if command.scope in bucket:
            raise ValueError(f"指令重复注册: {command.name} ({command.scope})")
        bucket[command.scope] = command
        for alias in command.aliases:
            a = alias.lower().lstrip("/")
            if not a:
                continue
            alias_bucket = self._aliases.setdefault(a, {})
            if command.scope in alias_bucket:
                raise ValueError(f"别名重复注册: {alias} ({command.scope})")
            alias_bucket[command.scope] = key
        return command

    def load_directory(self, directory: str | Path) -> Dict[str, Any]:
        """加载目录下所有 .py 插件，调用其 register(reg)。"""
        root = Path(directory)
        loaded: List[str] = []
        failed: List[str] = []
        if not root.is_dir():
            return {"loaded": [], "failed": [f"目录不存在: {root}"]}

        for path in sorted(root.glob("*.py")):
            if path.name.startswith("_"):
                continue
            mod_name = f"cmd_plugin_{root.name}_{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError(f"无法加载 {path}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise AttributeError("插件缺少 register(registry) 函数")
                register(self)
                loaded.append(path.stem)
            except Exception as exc:  # noqa: BLE001 - 一个插件坏了不影响其他
                failed.append(f"{path.name}: {exc}")
        return {"loaded": loaded, "failed": failed}

    # ---- 查询 ----
    def get(self, name: str, platform: str = "") -> Optional[Command]:
        key = (name or "").lower().lstrip("/")
        alias_bucket = self._aliases.get(key)
        if alias_bucket:
            target = self._pick(alias_bucket, platform, strict=True)
            if target:
                key = target
        return self._pick(self._commands.get(key, {}), platform, strict=bool(platform))

    def all(self) -> List[Command]:
        return [cmd for bucket in self._commands.values() for cmd in bucket.values()]

    def visible(self, platform: str, *, menu: str | None = None, is_admin: bool = False) -> List[Command]:
        out = []
        for bucket in self._commands.values():
            cmd = self._pick(bucket, platform, strict=True)
            if cmd is None:
                continue
            allowed, _ = cmd.allows(platform, is_admin)
            if not allowed or cmd.hidden:
                continue
            if menu and cmd.menu != menu:
                continue
            out.append(cmd)
        return sorted(out, key=lambda c: (c.order, c.name))

    def resolve(self, ctx: CommandContext) -> Resolution:
        parsed = parse_command(ctx.text)
        if parsed is None:
            return Resolution(ok=False, reason="不是指令")
        name, args = parsed
        cmd = self.get(name, ctx.platform)
        if cmd is None:
            foreign = self.get(name)
            if foreign is not None and foreign.scope not in (PLATFORM_ALL, ctx.platform):
                return Resolution(ok=False, args=args, reason=f"该指令只在 {foreign.scope} 可用")
            return Resolution(ok=False, args=args, reason=f"未知指令 /{name}")
        allowed, reason = cmd.allows(ctx.platform, ctx.is_admin)
        if not allowed:
            return Resolution(ok=False, args=args, command=cmd, reason=reason)
        return Resolution(ok=True, args=args, command=cmd)

    # ---- 菜单 ----
    def render_menu(self, ctx: CommandContext, menu: str) -> str:
        cmds = self.visible(ctx.platform, menu=menu, is_admin=ctx.is_admin)
        if not cmds:
            return f"{menu}指令：暂无"
        lines = [f"{menu}指令（{ctx.platform}）"]
        for cmd in cmds:
            usage = cmd.usage or f"/{cmd.name}"
            desc = cmd.help or ""
            lines.append(f"{usage}  {desc}".rstrip())
        return "\n".join(lines)

    def stats(self) -> Dict[str, int]:
        return {"total": len(self.all()), "aliases": sum(len(v) for v in self._aliases.values())}
