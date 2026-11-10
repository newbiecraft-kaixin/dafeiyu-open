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
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra import find_node, find_venv_python, resolve_versioned

from .probe import port_alive as _port_alive

LOG = logging.getLogger("hub.manage")

ROOT = Path(__file__).resolve().parents[3]
SERVICES = ROOT / "services"
LOG_DIR = SERVICES / "hub" / "data"
# Python 依赖放在 Python 默认环境：默认复用启动 hub 的解释器，
# 旧 venv 只在显式 PATH 里指定时才用，不再写死。
VENV_PY = Path(os.environ.get("FLATFISH_PYTHON") or sys.executable)
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
    # NapCat 既可能解压在仓库根，也可能在 installer 下
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
            executable = sys.executable
    if not executable and file.suffix == ".js":
        # 有些 node 应用挑版本（dsh 在 22 上会静默退出），清单里可以用 node 字段点名。
        wanted = str(runtime.get("node") or "").strip()
        if wanted == "system":
            executable = "node"
        elif wanted:
            candidate = _abs_path(wanted)
            executable = str(candidate) if candidate.exists() else "node"
        else:
            executable = str(NODE if NODE.exists() else "node")
    if not executable:
        executable = str(VENV_PY)

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
        spec = _runtime_spec(manifest, runtime)
        if spec is not None:
            spec["depends"] = manifest.get("depends") or []
            spec["probe"] = manifest.get("probe") or ""
        return spec

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
    return {"cwd": cwd, "cmd": cmd, "port": manifest.get("port"),
            "probe": manifest.get("probe") or "",
            "depends": manifest.get("depends") or []}


def _port_of(name: str) -> Optional[int]:
    try:
        manifest = json.loads((SERVICES / name / "service.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    port = manifest.get("port")
    return port if isinstance(port, int) else None


def _offline_deps(depends: List[str]) -> List[str]:
    """启动前看一眼依赖是否在线；只作提示，不阻塞启动。"""
    offline: List[str] = []
    for name in depends:
        if name == "infra":
            continue
        port = _port_of(name)
        if isinstance(port, int) and not _port_alive(port):
            offline.append(name)
    return offline


def _health_ready(port: int, timeout: float = 1.5) -> bool:
    """端口开了不代表服务可用：再探一次 /health。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except OSError:
        return False


def _pids_on_port(port: int) -> List[int]:
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, encoding="mbcs", errors="replace",
            timeout=10, check=False,
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return []
    pids: List[int] = []
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            tail = line.split()
            if tail and tail[-1].isdigit() and int(tail[-1]) not in pids:
                pids.append(int(tail[-1]))
    return pids


def _acquire_service_lock(name: str, timeout_ms: int = 20000):
    """和中控面板共用同一命名互斥量，避免同一服务被拉两份。"""
    if os.name != "nt":
        return None, True
    try:
        import ctypes

        handle = ctypes.windll.kernel32.CreateMutexW(
            None, False, f"Local\\FlatFish.Service.{name}"
        )
        if not handle:
            return None, True
        wait = ctypes.windll.kernel32.WaitForSingleObject(handle, timeout_ms)
        if wait in (0x0, 0x80):
            return handle, True
        ctypes.windll.kernel32.CloseHandle(handle)
        return None, False
    except Exception:  # noqa: BLE001
        return None, True


def _release_service_lock(handle: Any) -> None:
    if not handle:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.ReleaseMutex(handle)
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass


def _pid_file(name: str) -> Path:
    return LOG_DIR / f"manage-{name}.pid"


def _read_pid(name: str) -> int:
    try:
        return int(_pid_file(name).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def _write_pid(name: str, pid: int) -> None:
    try:
        _pid_file(name).write_text(str(pid), encoding="ascii")
    except OSError:
        pass


def _clear_pid(name: str) -> None:
    try:
        _pid_file(name).unlink()
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259
    except Exception:  # noqa: BLE001
        return False


def _log_tail(name: str, size: int = 4096) -> str:
    try:
        with open(LOG_DIR / f"manage-{name}.log", "rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - size))
            text = stream.read().decode("utf-8", "replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return lines[-1][:240] if lines else ""
    except OSError:
        return ""


def _stop(spec: Dict[str, Any]) -> Dict[str, Any]:
    port = spec.get("port")
    if not isinstance(port, int):
        return {"ok": False, "message": "清单里没有端口，无法定位进程"}
    name = str(spec.get("name") or "")
    pids = _pids_on_port(port)
    tracked = _read_pid(name)
    if _pid_alive(tracked) and tracked not in pids:
        pids.append(tracked)  # 还在加载、尚未监听端口的进程也要能停
    if not pids:
        _clear_pid(name)
        return {"ok": True, "message": f"端口 {port} 没有进程，已是停止状态"}
    for pid in pids:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, encoding="mbcs", errors="replace",
            timeout=15, check=False,
        )
        LOG.info("[manage] stop %s pid=%s", name, pid)
    deadline = time.time() + 8
    while time.time() < deadline and _port_alive(port):
        time.sleep(0.3)
    ok = not _port_alive(port)
    if ok:
        _clear_pid(name)
    return {"ok": ok, "message": f"已停止（PID {', '.join(map(str, pids))}）"}


def _start(spec: Dict[str, Any]) -> Dict[str, Any]:
    port = spec.get("port")
    name = str(spec.get("name") or "")
    if isinstance(port, int) and _port_alive(port):
        return {"ok": True, "message": f"端口 {port} 已在监听，无需启动"}
    existing = _read_pid(name)
    if _pid_alive(existing):
        # 进程已在，但还没监听端口（如 CosyVoice 首次加载模型），不再拉第二份
        return {"ok": True, "message": f"已在启动/运行（PID {existing}），不重复拉起",
                "online": False, "starting": True}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(spec.get("env") or {})
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_path = LOG_DIR / f"manage-{name}.log"
    with open(log_path, "ab") as stream:
        process = subprocess.Popen(
            spec["cmd"], cwd=str(spec["cwd"]), stdin=subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.STDOUT, env=env,
            creationflags=flags, close_fds=True,
        )
    _write_pid(name, process.pid)
    LOG.info("[manage] start %s pid=%s -> %s", name, process.pid, " ".join(spec["cmd"]))
    deadline = time.time() + 12
    while time.time() < deadline and isinstance(port, int) and not _port_alive(port):
        if process.poll() is not None:
            break
        time.sleep(0.4)
    alive = _port_alive(port) if isinstance(port, int) else None
    if isinstance(port, int) and not alive and process.poll() is not None:
        _clear_pid(name)
        tail = _log_tail(name)
        return {"ok": False, "message": "启动失败，进程已退出（已回滚）"
                + (f"：{tail}" if tail else "（看 manage-*.log）"),
                "online": False, "ready": False}
    # probe=port 的服务（dsh / astrbot 等）压根没有 /health 路由，端口开了就算就绪。
    probe = str(spec.get("probe") or "").strip().lower()
    ready = bool(alive) and isinstance(port, int) and (probe == "port" or _health_ready(port))
    offline = _offline_deps(spec.get("depends") or [])
    if ready:
        message = f"已启动，端口 {port} 已就绪"
    elif alive:
        message = f"已启动，端口 {port} 已监听，但 /health 还没通过"
    else:
        message = f"已拉起（PID {process.pid}），端口 {port} 暂未监听（看 manage-*.log）"
    if offline:
        message += "；依赖未启动: " + "、".join(offline)
    return {
        # 端口还没监听但进程仍在，说明在加载（如 CosyVoice），不能算失败
        "ok": bool(alive) or process.poll() is None,
        "message": message,
        "online": alive,
        "ready": ready,
        "starting": alive is False,
        "offline_deps": offline,
    }


def run(name: str, action: str) -> Dict[str, Any]:
    """执行一次管控动作，返回 {ok, message, ...}，不抛异常。"""
    spec = control_spec(name)
    if spec is None:
        return {"ok": False, "message": f"{name or '该服务'} 没有独立进程，不支持启停"}
    if action not in {"start", "stop", "restart"}:
        return {"ok": False, "message": f"未知操作 {action}"}

    spec["name"] = name
    lock, acquired = _acquire_service_lock(name)
    if not acquired:
        return {"ok": False, "message": "另一个启动/停止操作正在进行，请稍后再试"}
    try:
        if action == "start":
            result = _start(spec)
        elif action == "stop":
            result = _stop(spec)
        else:
            stopped = _stop(spec)
            result = stopped if not stopped.get("ok") else _start(spec)
    except Exception as error:  # noqa: BLE001 - 管控失败只回消息，不炸接口
        LOG.error("[manage] %s %s 失败: %s", action, name, error)
        result = {"ok": False, "message": f"{action} 失败: {error}"}
    finally:
        _release_service_lock(lock)

    # AstrBot 重启会带走 qq_mcp，dsh 的 streamable-http 会话不会自愈，得跟着重启。
    if name == "astrbot" and action in {"start", "restart"} and result.get("ok"):
        _restart_dsh_async()
    return result


_BULK_RUNNING = False


def _restart_dsh_async() -> None:
    """AstrBot 起来后，后台把 dsh 也重启一遍，让它重新拉 qq_mcp 的工具表。

    只在 dsh 本来就在跑时执行：用户主动关掉的桌宠不去复活。
    """
    if _BULK_RUNNING:
        return  # 一键重启里已经安排在最后单独拉起
    port = _port_of("dsh")
    if not isinstance(port, int) or not _port_alive(port):
        return

    def _worker() -> None:
        time.sleep(2.0)  # 让这轮对话的回复先发出去
        result = run("dsh", "restart")
        LOG.info("[manage] astrbot 重启后联动 dsh: %s", result.get("message", ""))

    threading.Thread(target=_worker, daemon=True, name="restart-dsh-after-astrbot").start()


def _manageable_names() -> List[str]:
    """所有有独立进程的服务（和 registry 的 manageable 判定一致）。"""
    names: List[str] = []
    try:
        folders = sorted(p for p in SERVICES.iterdir() if p.is_dir())
    except OSError:
        return names
    for folder in folders:
        if control_spec(folder.name) is not None:
            names.append(folder.name)
    return names


def run_all(action: str = "restart") -> Dict[str, Any]:
    """一键重启全部服务：后台线程逐个执行，立即返回清单。

    顺序：napcat 最先（协议端先就位），astrbot 最后（起来一次就连上），
    其余服务按目录名排中间。hub 自己没有独立进程，不参与。
    """
    if action not in {"start", "restart"}:
        return {"ok": False, "message": "一键操作只支持 start / restart"}
    names = _manageable_names()
    if not names:
        return {"ok": False, "message": "没有可管理的服务"}
    names.sort(key=lambda n: ({"napcat": 0, "astrbot": 9}.get(n, 5), n))

    def _worker() -> None:
        global _BULK_RUNNING
        _BULK_RUNNING = True
        try:
            # dsh 依赖 AstrBot 里的 qq_mcp，必须等 astrbot 起完再拉，否则工具表还是旧的。
            for name in [item for item in names if item != "dsh"]:
                result = run(name, action)
                LOG.info("[manage] 一键%s %s: %s", action, name, result.get("message", ""))
            if "dsh" in names:
                result = run("dsh", action)
                LOG.info("[manage] 一键%s dsh: %s", action, result.get("message", ""))
        finally:
            _BULK_RUNNING = False

    threading.Thread(target=_worker, daemon=True, name="manage-all").start()
    return {"ok": True, "action": action, "names": names, "count": len(names)}
