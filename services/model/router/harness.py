"""DSH headless harness 调用。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

from .dotenv import resolve_key
from .errors import ModelError
from .route import Route


def _parse_stream(lines: List[str]) -> Tuple[str, str, str, bool]:
    """返回 (session_id, final, error, saw_turn)。"""
    sid, texts, final, error, saw_turn = "", [], "", "", False
    for raw in lines:
        line = str(raw or "").strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "session":
            sid = str(event.get("sessionId") or "")
        elif kind == "text":
            value = str(event.get("text") or "")
            if value:
                texts.append(value)
        elif kind == "final":
            final = str(event.get("text") or "")
            saw_turn = True
        elif kind == "status" and event.get("phase") in ("turn_start", "step_start"):
            saw_turn = True
        elif kind == "error":
            error = str(event.get("message") or "") or "DSH 执行出错"
    return sid, (final if final else (texts[-1] if texts else "")), error, saw_turn


_REPO_ROOT = Path(__file__).resolve().parents[3]


def _dsh_candidates(route: Route) -> List[Path]:
    raw = (route.dsh_root or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return [path if path.is_absolute() else (_REPO_ROOT / path)]
    home = Path.home()
    return [home / "dafeiyu" / "dsh", home / "dsh", _REPO_ROOT / "dsh"]


def _dsh_root(route: Route) -> Path:
    """dsh_root 留空时在常见安装位置里挑一个真正装好的。"""
    candidates = _dsh_candidates(route)
    for cand in candidates:
        if (cand / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js").is_file():
            return cand
    return candidates[0]


def _node_bin(route: Route) -> Tuple[str, str]:
    node = shutil.which(os.environ.get("DSH_NODE", "node"))
    if not node:
        raise ModelError("找不到 node，可设置环境变量 DSH_NODE 指向 node.exe")
    root = _dsh_root(route)
    bin_js = root / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    if not bin_js.is_file():
        raise ModelError(f"DSH 未安装或路径不对: {bin_js}")
    return node, str(bin_js)


def _run_harness(
    route: Route,
    *,
    profile: str,
    task: str,
    session_id: str,
    dotenv: List[Path],
) -> Tuple[str, str]:
    """跑一次 DSH headless，返回 (reply, session_id)。"""
    if not profile:
        raise ModelError("harness 模式需要 profile")
    node, bin_js = _node_bin(route)
    env = dict(os.environ)
    home = route.dsh_home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    env["DSH_HOME"] = home
    env.setdefault("NODE_NO_WARNINGS", "1")
    key = resolve_key(route.api_key_env, dotenv)
    if key:
        env[route.api_key_env] = key

    def _invoke(sid: str) -> Tuple[str, str, str, bool]:
        cmd = [node, bin_js, "--profile", profile, "--json"]
        if sid:
            cmd += ["--session-id", sid]
        cmd.append(task)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(_dsh_root(route)),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=route.timeout_s,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelError(f"DSH 超时（{route.timeout_s:.0f}s）") from exc
        sid_out, reply, error, saw_turn = _parse_stream(proc.stdout.splitlines())
        return sid_out, reply, (error or (proc.stderr or "").strip()), saw_turn

    sid_out, reply, error, saw_turn = _invoke(session_id)
    if session_id and not saw_turn and not reply and "session" in error.lower():
        sid_out, reply, error, saw_turn = _invoke("")
    if not reply:
        detail = error or "DSH 返回空回复"
        raise ModelError(f"DSH 执行失败: {detail[:200]}")
    return reply, (sid_out or session_id)
