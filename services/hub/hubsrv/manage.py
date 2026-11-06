"""能力管控：启动 / 停止 / 重启本地服务进程。

只接本机控制台使用，不暴露到外网；hub 本身不支持在这里重启（会把请求掐断）。
启动规格优先来自 service.json 的 runtime 段（cwd / entry / python / args / env），
没有 runtime 时回落到按 entry 推导，保证老服务清单继续可用。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra import find_node, find_venv_python, resolve_versioned

from .probe import port_alive as _port_alive

LOG = logging.getLogger("hub.manage")

ROOT = Path(__file__).resolve().parents[3]
SERVICES = ROOT / "services"
LOG_DIR = SERVICES / "hub" / "data"
# 固定位置找不到时按版本号搜索，再退化到当前解释器，避免写死 AstrBot 版本。
VENV_PY = find_venv_python(ROOT) or Path(sys.executable)
WECHAT_PY = ROOT / "bots" / "wxauto-test" / "venv" / "Scripts" / "python.exe"
NODE = Path(find_node(ROOT) or "node")


def _abs_path(value: str) -> Path:
    """相对路径按仓库根解析；写死的 AstrBot 版本号自动换成实际目录。"""
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    if path.exists():
        return path
    path = resolve_versioned(path, ROOT)
    if path.exists():
        return path
    # NapCat 既可能解压在仓库根，也可能在 tools/windows 下
    if "napcat-win" in path.parts:
        tail = path.parts[path.parts.index("napcat-win") + 1:]
        alt = (ROOT / "tools" / "windows" / "napcat-win").joinpath(*tail)
        if alt.exists():
            return alt
    return path


def _resolve_env(env: Dict[str, Any]) -> Dict[str, str]:
    resolved: Dict[str, str] = {}
    for key, value in env.items():
        text = str(value)
        candidate = _abs_path(text)
        # 只把「本仓库里真实存在的相对路径」补成绝对路径，普通开关值保持原样。
        if not Path(text).is_absolute() and candidate.exists():
            text = str(candidate)
        resolved[str(key)] = text
    return resolved


def _runtime_spec(manifest: Dict[str, Any], runtime: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按 manifest 的 runtime 段生成启动规格；路径相对仓库根解析。"""
    entry = str(runtime.get("entry") or manifest.get("entry") or "").strip()
    if not entry:
        return None
    file = _abs_path(entry)
    if not file.exists():
        return None
    cwd = _abs_path(str(runtime.get("cwd") or ".").strip() or ".")

    python = str(runtime.get("python") or "").strip()
    executable = ""
    if python:
        candidate = _abs_path(python)
        if candidate.exists():
            executable = str(candidate)
        elif python.lower() in {"python", "python3"}:
            executable = python
    if not executable:
        executable = str(NODE if NODE.exists() else "node") if file.suffix == ".js" else str(VENV_PY)

    args = [str(arg) for arg in runtime.get("args") or []]
    spec: Dict[str, Any] = {
        "cwd": cwd,
        "cmd": [executable, str(file)] + args,
        "port": manifest.get("port"),
    }
    env = runtime.get("env")
    if isinstance(env, dict) and env:
        spec["env"] = _resolve_env(env)
    return spec


def control_spec(name: str) -> Optional[Dict[str, Any]]:
    """返回服务的启动规格；没独立进程（hub / 虚拟层）返回 None。"""
    name = str(name or "").strip()
    if not name or name == "hub":
        return None

    path = SERVICES / name / "service.json"
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    runtime = manifest.get("runtime")
    if isinstance(runtime, dict):
        return _runtime_spec(manifest, runtime)

    entry = str(manifest.get("entry") or "").strip()
    if not entry:
        return None

    if entry.startswith("bots/"):
        file = ROOT / entry
        cwd = ROOT / "bots"
    elif "/" in entry:
        file = ROOT / entry
        cwd = file.parent
    else:
        cwd = SERVICES / name
        file = cwd / entry
    if not file.exists():
        return None

    if file.suffix == ".js":
        cmd = [str(NODE if NODE.exists() else "node"), file.name]
    else:
        python = manifest.get("python")
        if not python and name == "io-wechat" and WECHAT_PY.exists():
            python = str(WECHAT_PY)
        cmd = [str(python or VENV_PY), file.name]
    return {"cwd": cwd, "cmd": cmd, "port": manifest.get("port")}


def _pids_on_port(port: int) -> List[int]:
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: List[int] = []
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            tail = line.split()
            if tail and tail[-1].isdigit() and int(tail[-1]) not in pids:
                pids.append(int(tail[-1]))
    return pids


def _stop(spec: Dict[str, Any]) -> Dict[str, Any]:
    port = spec.get("port")
    if not isinstance(port, int):
        return {"ok": False, "message": "清单里没有端口，无法定位进程"}
    pids = _pids_on_port(port)
    if not pids:
        return {"ok": True, "message": f"端口 {port} 没有进程，已是停止状态"}
    for pid in pids:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        LOG.info("[manage] stop %s pid=%s", spec.get("name"), pid)
    deadline = time.time() + 8
    while time.time() < deadline and _port_alive(port):
        time.sleep(0.3)
    return {"ok": not _port_alive(port), "message": f"已停止（PID {', '.join(map(str, pids))}）"}


def _start(spec: Dict[str, Any]) -> Dict[str, Any]:
    port = spec.get("port")
    if isinstance(port, int) and _port_alive(port):
        return {"ok": True, "message": f"端口 {port} 已在监听，无需启动"}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(spec.get("env") or {})
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_path = LOG_DIR / f"manage-{spec.get('name')}.log"
    with open(log_path, "ab") as stream:
        subprocess.Popen(
            spec["cmd"], cwd=str(spec["cwd"]), stdin=subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.STDOUT, env=env,
            creationflags=flags, close_fds=True,
        )
    LOG.info("[manage] start %s -> %s", spec.get("name"), " ".join(spec["cmd"]))
    deadline = time.time() + 12
    while time.time() < deadline and isinstance(port, int) and not _port_alive(port):
        time.sleep(0.4)
    alive = _port_alive(port) if isinstance(port, int) else None
    return {
        "ok": alive is not False,
        "message": f"已启动，端口 {port} " + ("已就绪" if alive else "暂未监听（看 manage-*.log）"),
        "online": alive,
    }


def run(name: str, action: str) -> Dict[str, Any]:
    """执行一次管控动作，返回 {ok, message, ...}，不抛异常。"""
    spec = control_spec(name)
    if spec is None:
        return {"ok": False, "message": f"{name or '该服务'} 没有独立进程，不支持启停"}
    if action not in {"start", "stop", "restart"}:
        return {"ok": False, "message": f"未知操作 {action}"}

    spec["name"] = name
    try:
        if action == "start":
            return _start(spec)
        if action == "stop":
            return _stop(spec)
        stopped = _stop(spec)
        if not stopped.get("ok"):
            return stopped
        return _start(spec)
    except Exception as error:  # noqa: BLE001 - 管控失败只回消息，不炸接口
        LOG.error("[manage] %s %s 失败: %s", action, name, error)
        return {"ok": False, "message": f"{action} 失败: {error}"}
