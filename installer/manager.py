"""大肥鱼 服务管理器（无控制台 GUI）。

读取仓库里的 services/*/service.json，统一启停各服务，并在浏览器打开对应页面。
- 双击「大肥鱼管理面板.vbs」启动，不会出现黑色控制台窗口；
- 也可以 `pythonw tools\\windows\\manager.py` 启动；
- 所有子进程都用 CREATE_NO_WINDOW 拉起，日志写到 services/hub/data/manage-<name>.log。

只用标准库（tkinter），不额外装依赖。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tkinter as tk
from tkinter import messagebox
from tkinter import font as tkfont

# ---------------------------------------------------------------- 路径与配置

HERE = Path(__file__).resolve().parent


def _find_root() -> Path:
    """管理器可能在仓库里，也可能在部署目录里；都按「向上找到 services/hub」处理。

    依次尝试：脚本上两级 → 上三级（installer 布局）→ FlatFish → 旧部署 ~/dafeiyu。
    """
    flatfish = Path(os.environ.get("FLATFISH_HOME") or r"C:\Program Files\FlatFish")
    for cand in (HERE.parent.parent, HERE.parent, flatfish, Path.home() / "dafeiyu"):
        if (cand / "services" / "hub" / "service.py").is_file():
            return cand
    return HERE.parent.parent


ROOT = _find_root()
SERVICES = ROOT / "services"
LOG_DIR = SERVICES / "hub" / "data"
LOG_DIR.mkdir(parents=True, exist_ok=True)

REPO_SLUG = "newbiecraft-kaixin/dafeiyu-open"
REPO_BRANCH = "main"
REPO_ZIP = f"https://codeload.github.com/{REPO_SLUG}/zip/refs/heads/{REPO_BRANCH}"


def _find_script(name: str) -> Path:
    """安装脚本可能在 installer/（新布局），也可能在旧布局 tools/windows/。"""
    for folder in (ROOT / "installer", HERE, ROOT / "tools" / "windows"):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return ROOT / "installer" / name


SETUP_PS1 = _find_script("setup.ps1")
FEATURES_FILE = ROOT / "config" / "features.json"

# 可选能力：一键部署只安装这里启用的项，编号对应 setup.ps1 的菜单
FEATURE_ITEMS = [
    ("core", "运行环境 + 核心四服务", ["1", "2"]),
    ("astrbot", "AstrBot 宿主", ["3"]),
    ("plugins", "大肥鱼插件包", ["4"]),
    ("napcat", "NapCat（QQ 接入）", ["5"]),
    ("wechat", "微信桥 io-wechat", ["6"]),
    ("voice", "本地语音 CosyVoice2（约 3GB）", ["7"]),
    ("pet", "桌宠 dsh-pet", ["8"]),
]
DEFAULT_FEATURES = {
    "core": True, "astrbot": True, "plugins": True,
    "napcat": True, "wechat": False, "voice": False, "pet": True,
}
FEATURE_SERVICES = {
    "voice": "voice", "io-wechat": "wechat", "pet": "pet",
    "astrbot": "astrbot", "napcat": "napcat",
}

# 核心部署：环境 + 核心四服务 + AstrBot + 基础插件 + NapCat + 桌宠
CORE_STEPS = "1,2,3,4,5,8"
ALL_STEPS = "1,2,3,4,5,6,7,8"
# 核心部署不启动的外围能力（语音、音频、视频、字幕、RAG、微信桥）
CORE_SKIP_SERVICES = {"voice", "audio", "video", "subtitle", "rag", "io-wechat", "wechat"}
UNINSTALL_PS1 = _find_script("uninstall.ps1")

# Python 属于前置依赖：默认用启动面板的解释器（即 Python 默认环境），
# 旧 deployment 里的 venv 只在显式指定时兜底，不再优先。
PY = os.environ.get("FLATFISH_PYTHON") or sys.executable
VENV_PY = ROOT / "astrbot-win" / "venv" / "Scripts" / "python.exe"
NODE = shutil.which("node") or "node"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

LAYER_ORDER = {"中枢层": 0, "能力层": 1, "输入输出层": 2, "监控层": 3, "软件层": 4}
START_ORDER = [
    "hub", "store", "model", "brain",
    "audio", "video", "voice",
    "io-wechat", "astrbot", "napcat", "pet", "dsh",
]

# 梦幻蓝主题：浅蓝渐变背景 + 白色卡片 + 深海蓝文字
BG = "#d9ecff"
BG_TOP = "#eef8ff"
BG_BOTTOM = "#bfe0fb"
HEADER_BOTTOM = "#c9e5fb"
FOOTER_BG = "#c6e4fb"
CARD = "#ffffff"
CARD_END = "#eaf5ff"
CARD_HOVER = "#e4f2ff"
BORDER = "#b9dcf5"
FG = "#123a5e"
MUTED = "#5c7f9f"
DIM = "#7e9ab5"
GREEN = "#2f9e63"
RED = "#e2574c"
AMBER = "#c98a1c"
ACCENT = "#1f7fd0"
LOG_BG = "#f5faff"
LOG_FG = "#1b3a57"
ASSETS = HERE / "assets"


def _blend(color_a: str, color_b: str, t: float) -> str:
    """在两个 #rrggbb 颜色之间取插值，用于绘制渐变。"""
    t = max(0.0, min(1.0, t))
    a = tuple(int(color_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(color_b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def paint_gradient(canvas: tk.Misc, width: int, height: int, top: str, bottom: str, tag: str) -> None:
    """在 Canvas 上画竖向渐变；按高度分约 160 段，避免大量图元拖慢重绘。"""
    canvas.delete(tag)
    width, height = max(1, width), max(1, height)
    step = max(1, height // 160)
    y = 0
    while y < height:
        color = _blend(top, bottom, y / max(1, height - 1))
        canvas.create_rectangle(0, y, width, min(y + step, height), fill=color, outline=color, tags=tag)
        y += step


# ---------------------------------------------------------------- 服务清单


def _abs(value: str, root: Path | None = None) -> Path:
    root = root or ROOT
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path


_VERSION_RE = re.compile(r"\d+")


def _version_key(text: str) -> tuple[int, ...]:
    """目录名里的数字段转元组：AstrBot-4.29.3 → (4, 29, 3)。

    不能直接按字符串排序，否则 4.9.0 会被当成比 4.29.3 新。
    """
    return tuple(int(part) for part in _VERSION_RE.findall(text))


def _versioned(path: Path) -> Path:
    """services 里写的是 AstrBot-4.29.3，实际可能是 4.28 或 4.31。

    在同一个父目录里扫描所有 AstrBot*，按**数字版本**挑最高的、且目标文件
    确实存在的那一份；都不存在时原样返回，交给上层报「未安装」。
    """
    parts = list(path.parts)
    for i, part in enumerate(parts):
        if not part.startswith("AstrBot"):
            continue
        parent = Path(*parts[:i])
        if not parent.is_dir():
            continue
        tail = Path(*parts[i + 1:])
        hits: list[tuple[tuple[int, ...], Path]] = []
        try:
            for cand in parent.iterdir():
                if not cand.is_dir() or not cand.name.startswith("AstrBot"):
                    continue
                target = cand / tail if tail.parts else cand
                if target.exists():
                    hits.append((_version_key(cand.name), target))
        except OSError:
            pass
        if hits:
            hits.sort(key=lambda item: item[0])
            return hits[-1][1]
        break
    return path


def _astrbot_scan_roots() -> list[Path]:
    """可能已经装了 AstrBot 的位置（有些人早就装过，别重复下）。"""
    env = os.environ.get("DFY_ASTRBOT", "").strip()
    home = Path.home()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    roots = [
        Path(env) if env else None,
        ROOT / "astrbot-win",
        ROOT,
        home / "astrbot-win",
        home / "AstrBot",
        home / "Desktop" / "astrbot-win",
        home / "Desktop" / "AstrBot",
        Path(local) / "astrbot-win" if local else None,
        Path(local) / "Programs" / "AstrBot" if local else None,
    ]
    # 只在固定盘符上试探常见目录，避免遍历整盘
    for drive in ("C:", "D:", "E:"):
        roots.append(Path(drive + os.sep + "AstrBot"))
        roots.append(Path(drive + os.sep + "astrbot-win"))
    return [r for r in roots if r is not None]


def _looks_astrbot(path: Path) -> bool:
    try:
        return path.is_dir() and ((path / "astrbot").is_dir() or (path / "main.py").is_file())
    except OSError:
        return False


def astrbot_installs() -> list[dict]:
    """扫描本机已有的 AstrBot 安装（含部署目录外的），按版本从高到低。"""
    found: dict[str, dict] = {}
    for base in _astrbot_scan_roots():
        try:
            if not base.is_dir():
                continue
            entries = [base] + [p for p in base.iterdir() if p.is_dir()]
        except OSError:
            continue
        for cand in entries:
            if not cand.name.lower().startswith("astrbot"):
                continue
            if not _looks_astrbot(cand):
                continue
            try:
                key = str(cand.resolve()).lower()
            except OSError:
                key = str(cand).lower()
            name = cand.name.split("-", 1)[-1] if "-" in cand.name else cand.name
            found[key] = {"path": cand, "version": name, "key": _version_key(cand.name)}
    return sorted(found.values(), key=lambda item: item["key"], reverse=True)


def plugin_roots() -> list[Path]:
    """所有已装 AstrBot 的插件目录 + 仓库自带插件目录。"""
    roots: list[Path] = []
    for inst in astrbot_installs():
        path = inst["path"] / "data" / "plugins"
        if path.is_dir():
            roots.append(path)
    extra = ROOT / "astrbot_plugins"
    if extra.is_dir():
        roots.append(extra)
    return roots


def expected_astrbot_version() -> str:
    """清单里期望的 AstrBot 版本（如 4.29.3），读不到返回空串。"""
    try:
        text = (SERVICES / "astrbot" / "service.json").read_text(encoding="utf-8")
    except OSError:
        return ""
    hit = re.search(r"AstrBot-([\d.]+)", text)
    return hit.group(1) if hit else ""


def astrbot_missing_hint() -> str:
    """启动前/失败后的人话提示：说清是没装、还是装了但版本/入口不对。"""
    insts = astrbot_installs()
    expected = expected_astrbot_version()
    if not insts:
        cli = shutil.which("astrbot") or shutil.which("astrbot.exe")
        if cli and not _astrbot_cli_roots():
            return f"检测到命令行 {cli}（uv/pip 全局装），但没找到含 .astrbot 的数据根目录；请到该目录执行 astrbot run"
        if cli:
            return f"检测到命令行 {cli}，但数据根目录不可用；请执行 astrbot run"
        return "未检测到 AstrBot（已扫描部署目录、用户目录与常见盘符），请先点「一键部署」安装"
    best = insts[0]
    text = f"检测到 AstrBot {best['version']}：{best['path']}"
    if expected:
        if best["key"] < _version_key(expected):
            text += f"，低于清单要求的 {expected}，建议升级"
        elif best["key"] > _version_key(expected):
            text += f"，高于清单 {expected}（可直接使用）"
        else:
            text += f"，与清单 {expected} 一致"
    return text + "；但主程序入口缺失，请检查该目录是否完整"


def _relocate_astrbot(manifest: dict, entry: Path, cwd: Path) -> tuple[Path, Path]:
    """清单里的 AstrBot 目录不存在时，退到本机扫描到的安装（入口同名）。"""
    if manifest.get("name") != "astrbot":
        return entry, cwd
    if not entry.is_file():
        for inst in astrbot_installs():
            cand = inst["path"] / entry.name
            if cand.is_file():
                return cand, inst["path"]
    if not cwd.is_dir() and entry.is_file():
        return entry, entry.parent
    return entry, cwd


def _astrbot_cli_roots() -> list[Path]:
    """`astrbot run` 以当前目录为根，要求根目录里有 .astrbot 与 data。"""
    roots: list[Path] = []
    env = os.environ.get("ASTRBOT_ROOT", "").strip()
    if env:
        roots.append(Path(env))
    home = Path.home()
    roots += [home / ".astrbot", home / "astrbot", home / "workspace" / "astrbot"]
    roots += [inst["path"] for inst in astrbot_installs()]
    out: list[Path] = []
    for root in roots:
        try:
            if (root / ".astrbot").exists() and (root / "data").is_dir():
                out.append(root)
        except OSError:
            continue
    return out


def _astrbot_cli_spec(manifest: dict) -> dict | None:
    """uv / pip 全局工具装的 AstrBot 没有源码 main.py，改用 `astrbot run` 启动。"""
    if manifest.get("name") != "astrbot":
        return None
    cli = shutil.which("astrbot") or shutil.which("astrbot.exe")
    if not cli:
        for cand in (
            Path(os.environ.get("LOCALAPPDATA", "")) / "uv" / "tools" / "astrbot" / "Scripts" / "astrbot.exe",
            Path.home() / ".local" / "bin" / "astrbot.exe",
            Path(os.environ.get("APPDATA", "")) / "Python" / "Scripts" / "astrbot.exe",
        ):
            if cand.is_file():
                cli = str(cand)
                break
    if not cli:
        return None
    roots = _astrbot_cli_roots()
    if not roots:
        return None
    return {"cwd": str(roots[0]), "cmd": [cli, "run"], "port": manifest.get("port")}


def load_services() -> list[dict]:
    items = []
    for path in sorted(SERVICES.glob("*/service.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        manifest.setdefault("name", path.parent.name)
        manifest["_dir"] = path.parent
        items.append(manifest)

    # merged_into：已经并进别的服务同进程的（如 subtitle → audio）不再单独占一行，
    # 只在宿主行标一个「含 xxx」；仍有独立进程的（如 CosyVoice voice）保留行、按钮可用。
    by_name = {m["name"]: m for m in items}
    visible = []
    for manifest in items:
        host = str(manifest.get("merged_into") or "").strip()
        if host and host in by_name and not manifest.get("runtime"):
            by_name[host].setdefault("_merged", []).append(manifest["name"])
            continue
        visible.append(manifest)

    def key(m: dict):
        name = m.get("name", "")
        order = START_ORDER.index(name) if name in START_ORDER else len(START_ORDER)
        return (LAYER_ORDER.get(m.get("layer", ""), 9), order, name)

    visible.sort(key=key)
    return visible


def _resolve_env(env: dict) -> dict:
    out = {}
    for key, value in (env or {}).items():
        text = str(value)
        if not text.strip():
            out[str(key)] = ""  # 空值原样保留，别被当成相对路径解析成仓库根目录
            continue
        cand = _abs(text)
        if not Path(text).is_absolute() and cand.exists():
            text = str(cand)
        out[str(key)] = text
    return out


def start_spec(manifest: dict) -> dict | None:
    """把 service.json 翻译成 (cwd, cmd, port)；没有独立进程返回 None。"""
    name = manifest.get("name", "")
    if name == "hub":  # hub 自己不在 manage 清单里，单独处理
        return {"cwd": SERVICES / "hub", "cmd": [PY, "service.py"], "port": 23408}
    if name == "dsh":  # DSH 用系统 node，NapCat 自带的 node 不能跑
        entry = ROOT / "dsh" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
        if not entry.is_file():
            return None
        return {"cwd": ROOT / "dsh", "cmd": [NODE, str(entry), "--profile", "web"], "port": 3080}

    runtime = manifest.get("runtime")
    if isinstance(runtime, dict) and runtime.get("entry"):
        entry = _versioned(_abs(str(runtime["entry"])))
        cwd = _abs(str(runtime.get("cwd") or "."))
        entry, cwd = _relocate_astrbot(manifest, entry, cwd)
        if not entry.is_file():
            return _astrbot_cli_spec(manifest)
        python = str(runtime.get("python") or "").strip()
        exe = ""
        if python:
            cand = _abs(python)
            if cand.is_file():
                exe = str(cand)
            elif python.lower() in {"python", "python3"}:
                exe = PY
            elif python.lower() == "node":
                exe = NODE
        if not exe:
            exe = NODE if entry.suffix == ".js" else PY
        args = [str(a) for a in runtime.get("args") or []]
        spec = {"cwd": cwd, "cmd": [exe, str(entry)] + args, "port": manifest.get("port")}
        env = _resolve_env(runtime.get("env") or {})
        if env:
            spec["env"] = env
        return spec

    entry_text = str(manifest.get("entry") or "").strip()
    if not entry_text:
        return None
    # 裸文件名（如 service.py）相对服务目录解析，带斜杠的相对仓库根解析
    if "/" in entry_text or "\\" in entry_text:
        entry = _versioned(_abs(entry_text))
    else:
        entry = _versioned(manifest["_dir"] / entry_text)
    if not entry.is_file():
        return None
    if entry_text.startswith("bots/"):
        cwd = ROOT / "bots"
    elif "/" in entry_text:
        cwd = entry.parent
    else:
        cwd = manifest["_dir"]
    if entry.suffix == ".js":
        cmd = [NODE, entry.name]
    else:
        python = str(manifest.get("python") or "").strip()
        if not python and name == "io-wechat":
            cand = ROOT / "bots" / "wxauto-test" / "venv" / "Scripts" / "python.exe"
            if cand.is_file():
                python = str(cand)
        cmd = [python or PY, entry.name]
    spec = {"cwd": cwd, "cmd": cmd, "port": manifest.get("port")}
    env = _resolve_env(manifest.get("env") or {})
    if env:
        spec["env"] = env
    return spec


def page_url(manifest: dict) -> str | None:
    links = manifest.get("links") or {}
    ui = str(manifest.get("ui") or "")
    if ui.startswith("http"):
        return ui
    for value in links.values():
        if str(value).startswith("http"):
            return str(value)
    port = manifest.get("port")
    return f"http://127.0.0.1:{port}" if isinstance(port, int) else None


# ---------------------------------------------------------------- 下载 / 依赖

SKIP_TOP = {
    "astrbot-win", "third_party", "dsh", "napcat-win", "cosyvoice_env",
    "logs", "data", ".git", ".env",
}
SKIP_PARTS = {"__pycache__", "node_modules", ".git", ".venv", "venv"}
SKIP_SUFFIX = {".pyc", ".pyo", ".log"}
# 这些是运行时状态，升级仓库内容时不能被仓库模板覆盖
SKIP_REL = {("config", "features.json"), ("config", "migrate-journal.json")}


def _skip(rel: Path) -> bool:
    """同步仓库时跳过运行时产物：venv、data、日志、缓存、.env。"""
    parts = rel.parts
    if not parts or parts[0] in SKIP_TOP:
        return True
    if parts in SKIP_REL:
        return True
    if any(part in SKIP_PARTS for part in parts[:-1]):
        return True
    if rel.suffix.lower() in SKIP_SUFFIX or rel.name == ".env":
        return True
    # services/<name>/data/*：store、hub、rag 的运行时数据库
    if len(parts) > 3 and parts[0] == "services" and "data" in parts[2:-1]:
        return True
    # CosyVoice 的虚拟环境与模型
    if len(parts) > 2 and parts[0] == "tools" and parts[1] in {"cosyvoice_env", "cosyvoice_models"}:
        return True
    return False


def fetch_repo_zip(log, url: str = REPO_ZIP) -> Path:
    log(f"→ {url}")
    target = Path(tempfile.gettempdir()) / f"dafeiyu-open-{REPO_BRANCH}.zip"
    try:
        _urllib_download(url, target, log)
    except Exception as error:  # noqa: BLE001 - 网络异常时换系统下载器
        log(f"  直连下载失败：{error}", "err")
        log("  改用系统下载器重试 …", "dim")
        _powershell_download(url, target)
    log(f"已下载 {target.stat().st_size / 1048576:.1f} MB", "ok")
    return target


def _urllib_download(url: str, target: Path, log) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "dafeiyu-manager"})
    with urllib.request.urlopen(request, timeout=20) as response, open(target, "wb") as stream:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        mark = -1
        while True:
            block = response.read(256 * 1024)
            if not block:
                break
            stream.write(block)
            done += len(block)
            if total:
                percent = done * 100 // total
                if percent >= mark + 10:
                    mark = percent - percent % 10
                    log(f"  下载中 {percent}%  {done / 1048576:.1f}/{total / 1048576:.1f} MB")
            elif done // (5 * 1048576) > mark:
                mark = done // (5 * 1048576)
                log(f"  已下载 {done / 1048576:.1f} MB")


def _powershell_download(url: str, target: Path) -> None:
    quoted_url = url.replace("'", "''")
    quoted_path = str(target).replace("'", "''")
    command = (
        "$ProgressPreference='SilentlyContinue'; "
        f"Invoke-WebRequest -Uri '{quoted_url}' -OutFile '{quoted_path}' -UseBasicParsing"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
        timeout=300,
    )
    if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError((completed.stdout or "系统下载器失败").strip()[-300:])


def extract_repo(zip_path: Path) -> Path:
    staging = Path(tempfile.mkdtemp(prefix="dafeiyu-sync-"))
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"压缩包内有异常路径：{info.filename}")
        archive.extractall(staging)
    folders = [path for path in staging.iterdir() if path.is_dir()]
    if not folders:
        raise ValueError("压缩包结构异常，找不到仓库目录")
    return folders[0]


def detect_local_repo() -> Path | None:
    """找本机仓库工作目录；找到就优先用本地内容（包含未提交改动）。"""
    env = os.environ.get("DFY_REPO", "").strip()
    candidates = [
        Path(env) if env else None,
        Path.home() / "IdeaProjects" / "dafeiyu-open",
        Path.home() / "dafeiyu-open",
        Path.home() / "IdeaProjects" / "dafeiyu",
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            if (candidate / ".git").exists() and (candidate / "services" / "hub" / "service.py").is_file():
                return candidate
        except OSError:
            continue
    return None


def sync_repo(log, dest: Path | None = None, source: str | Path | None = None) -> str:
    """把仓库内容同步到 dest（默认部署根），保留运行时数据。

    source 可以是本机仓库目录，也可以是 GitHub zip 地址；留空时优先本机仓库，
    找不到再下载 GitHub main。
    """
    dest = (Path(dest) if dest else ROOT).resolve()
    raw = str(source or "").strip()
    temp_paths: list[Path] = []
    try:
        if raw.lower().startswith(("http://", "https://")):
            zip_path = fetch_repo_zip(log, raw)
            temp_paths.append(zip_path)
            source_dir = extract_repo(zip_path)
            temp_paths.append(source_dir.parent)
        elif raw:
            source_dir = Path(raw).expanduser().resolve()
            log(f"来源：本机仓库 {source_dir}")
        else:
            local = detect_local_repo()
            if local is not None:
                source_dir = local.resolve()
                log(f"来源：本机仓库 {source_dir}（含未提交改动）")
            else:
                zip_path = fetch_repo_zip(log)
                temp_paths.append(zip_path)
                source_dir = extract_repo(zip_path)
                temp_paths.append(source_dir.parent)
                log("来源：GitHub main")

        if not (source_dir / "services").is_dir():
            raise FileNotFoundError(f"来源目录不像仓库：{source_dir}")
        if source_dir == dest:
            log("当前目录就是仓库本身，无需同步。", "ok")
            return "已在仓库目录，无需更新"

        copied = 0
        skipped = 0
        log(f"开始写入 {dest}")
        for path in source_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(source_dir)
            if _skip(rel):
                skipped += 1
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
        log(f"已更新 {copied} 个文件（跳过 {skipped} 个运行时/缓存文件）", "ok")
        if (dest / ".git").is_dir():
            log("提示：当前是 git 仓库目录，未提交的改动可能已被覆盖。", "dim")
        log("提示：新服务或界面改动需重启本面板后生效。", "dim")
        return f"仓库已更新：{copied} 个文件"
    finally:
        for temp in temp_paths:
            if temp.is_dir():
                shutil.rmtree(temp, ignore_errors=True)
            else:
                try:
                    temp.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------- 自动更新 / 补丁

GIT = shutil.which("git") or ""
# 记录上次部署的远端提交，避免非 git 部署每次重复下载整包
STATE_FILE = LOG_DIR / "deploy-state.json"


def _git(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess | None:
    if not GIT:
        return None
    try:
        return subprocess.run(
            [GIT, *args], cwd=str(ROOT), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _git_text(args: list[str], timeout: float = 60) -> str:
    done = _git(args, timeout)
    if done is None or done.returncode != 0:
        return ""
    return (done.stdout or "").strip()


def remote_commit() -> str:
    """远端最新提交：git 仓库问 origin，非 git 部署问 GitHub API。"""
    if (ROOT / ".git").is_dir() and GIT:
        text = _git_text(["ls-remote", "origin", f"refs/heads/{REPO_BRANCH}"], timeout=45)
        if text:
            return text.split()[0]
    try:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{REPO_SLUG}/commits/{REPO_BRANCH}",
            headers={"User-Agent": "dafeiyu-manager", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        return str(data.get("sha") or "")
    except Exception:  # noqa: BLE001 - 查不到远端时按「可能有更新」处理
        return ""


def _read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def update_repo(log) -> tuple[bool, set[str]]:
    """自动更新部署目录：git 仓库快进拉取，其它情况下载 GitHub 最新代码包。

    返回 (是否有代码变更, 变更文件相对路径集合)，供部署后挑服务重启。
    """
    if (ROOT / ".git").is_dir() and GIT:
        before = _git_text(["rev-parse", "HEAD"])
        remote = remote_commit()
        if not remote:
            log("查不到远端版本（网络不可用），跳过代码更新，继续用本地版本", "err")
            return False, set()
        if before and remote == before:
            log(f"代码已是最新：{before[:7]}", "ok")
            return False, set()
        log(f"发现新版本 {remote[:7]}（本地 {before[:7] or '未知'}），开始拉取 …", "ok")
        fetched = _git(["fetch", "origin", REPO_BRANCH], timeout=180)
        if fetched is None or fetched.returncode != 0:
            log("拉取远端失败，跳过代码更新（补丁与启动照常）", "err")
            return False, set()
        changed: set[str] = set()
        if before:
            changed = {
                line.strip()
                for line in _git_text(
                    ["diff", "--name-only", f"{before}..origin/{REPO_BRANCH}"], timeout=60
                ).splitlines()
                if line.strip()
            }
        pulled = _git(["pull", "--ff-only", "origin", REPO_BRANCH], timeout=240)
        if pulled is None or pulled.returncode != 0:
            detail = ""
            if pulled is not None:
                lines = (pulled.stderr or pulled.stdout or "").strip().splitlines()
                detail = lines[-1] if lines else ""
            log("本地有改动，无法快进更新：" + (detail or "git pull 失败"), "err")
            log("保留本地版本继续部署（补丁和依赖仍会更新）", "dim")
            return False, set()
        log(f"代码已更新到 {remote[:7]}，共 {len(changed)} 个文件", "ok")
        if any(rel.startswith("installer/") for rel in changed):
            log("提示：管理面板自身的更新需关闭并重开面板后生效", "dim")
        return True, changed

    # 非 git 部署：比对上次记录，避免每次重复下载几十 MB
    head = remote_commit()
    state = _read_state()
    if head and state.get("remote") == head:
        log(f"代码已是最新：{head[:7]}", "ok")
        return False, set()
    log("不是 git 工作目录：下载 GitHub 最新代码包", "dim")
    log(sync_repo(log, source=REPO_ZIP), "ok")
    if head:
        state["remote"] = head
        state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_state(state)
    return True, set()


def _package_version(folder: Path) -> str:
    try:
        data = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("version") or "").strip() if isinstance(data, dict) else ""


def _pet_source_version() -> str:
    """桌宠源码包版本取自 pet/dsh-pet/dsh-pet-<版本>.tgz。"""
    for path in sorted((ROOT / "pet" / "dsh-pet").glob("dsh-pet-*.tgz")):
        match = re.match(r"dsh-pet-(.+)\.tgz$", path.name)
        if match:
            return match.group(1)
    return ""


def apply_patches(log) -> tuple[int, int]:
    """把仓库维护的补丁覆盖到已装组件，返回 (桌宠补丁数, 插件补丁数)。"""
    pet_files = 0
    pet_pkg = Path.home() / ".dsh" / "profiles" / "web" / "node_modules" / "dsh-pet"
    pet_patches = ROOT / "pet" / "dsh-pet" / "patches"
    if pet_pkg.is_dir() and pet_patches.is_dir():
        for source in pet_patches.rglob("*"):
            if not source.is_file():
                continue
            target = pet_pkg / source.relative_to(pet_patches)
            if not target.parent.is_dir():
                continue
            shutil.copy2(source, target)
            pet_files += 1
        if pet_files:
            log(f"桌宠补丁：已替换 {pet_files} 个文件", "ok")
        else:
            log("桌宠补丁：无需替换", "dim")

    plugin_files = 0
    plugin_patches = ROOT / "tools" / "patches"
    if plugin_patches.is_dir():
        for install in astrbot_installs():
            plugins = install["path"] / "data" / "plugins"
            if not plugins.is_dir():
                continue
            for source in plugin_patches.rglob("*"):
                if not source.is_file():
                    continue
                rel = source.relative_to(plugin_patches)
                if len(rel.parts) < 2:
                    continue
                target = plugins / rel
                if not target.parent.is_dir():
                    continue
                shutil.copy2(source, target)
                plugin_files += 1
        if plugin_files:
            log(f"插件补丁：已替换 {plugin_files} 个文件", "ok")
    return pet_files, plugin_files


def affected_services(changed: set[str]) -> set[str]:
    """按变更文件推断需要重启的服务；共享层改动视为全部服务都要重启。"""
    if not changed:
        return set()
    names: set[str] = set()
    all_names = {m.get("name") for m in load_services() if m.get("name")}
    for rel in changed:
        parts = Path(rel).parts
        if not parts:
            continue
        top = parts[0]
        if top == "services" and len(parts) > 1:
            names.add(parts[1])
        elif top in {"shared", "installer"}:
            names |= all_names
        elif top in {"astrbot_plugins", "astrbot-win"}:
            names.add("astrbot")
        elif top in {"pet", "dsh"}:
            names.add("dsh")
    return names


# 这些大组件本机装过就复用，避免一键部署重复下载几个 GB
REUSE_ITEMS = [
    ("astrbot-win", "AstrBot"),
    ("napcat-win", "NapCat"),
    (Path("audio") / "CosyVoice", "CosyVoice 源码"),
    (Path("audio") / "cosyvoice_models", "CosyVoice 模型"),
    (Path("audio") / "seedvc_env", "语音运行环境"),
    ("bwe_env", "Python 运行环境"),
    (Path("dsh") / "node_modules", "桌宠宿主 dsh 依赖"),
    (Path("services") / "audio", "音频/字幕能力"),
]


def _is_deployment(path: Path) -> bool:
    return (path / "services" / "hub" / "service.py").is_file()


def donor_root() -> Path | None:
    """扫描本机其它完整部署目录，作为可复用组件的来源。"""
    candidates: list[Path] = []
    for key in ("DFY_REPO", "FLATFISH_HOME", "DFY_ROOT"):
        value = os.environ.get(key, "").strip()
        if value:
            candidates.append(Path(value))
    local = detect_local_repo()
    if local is not None:
        candidates.append(local)
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates += [
        Path(program_files) / "FlatFish",
        Path.home() / "dafeiyu-open",
        Path.home() / "dafeiyu",
    ]
    best: tuple[int, Path] | None = None
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == ROOT or not _is_deployment(resolved):
            continue
        # 同时有多个部署时，选可复用组件最多的那个，避免链到半残旧仓库
        score = sum(1 for rel, _label in REUSE_ITEMS if (resolved / rel).exists())
        if score and (best is None or score > best[0]):
            best = (score, resolved)
    return best[1] if best else None


def _junction(target: Path, source: Path) -> bool:
    """建目录联接：不占额外空间、不需要管理员权限。"""
    try:
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(source)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError:
        return False
    return done.returncode == 0 and target.exists()


def reuse_local_components(log) -> None:
    """把本机已有的大组件链接进部署目录，让 setup.ps1 扫描时判定已安装、不再下载。"""
    donor = donor_root()
    if donor is None:
        log("本机没有可复用的完整部署，缺失组件按需下载", "dim")
        return
    log(f"扫描到本机已有部署：{donor}", "ok")
    reused = 0
    for rel, label in REUSE_ITEMS:
        source = donor / rel
        target = ROOT / rel
        if not source.exists():
            continue
        if target.exists():
            log(f"{label}：目标已存在，保留现有", "dim")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if _junction(target, source):
            log(f"{label}：复用本机已有（目录链接，不下载）", "ok")
            reused += 1
            continue
        try:
            shutil.copytree(source, target)
            log(f"{label}：目录链接失败，已本地复制复用", "ok")
            reused += 1
        except OSError as error:
            log(f"{label}：复用失败（{error}），安装时重新准备", "err")
    env_file = donor / ".env"
    if env_file.is_file() and not (ROOT / ".env").exists():
        try:
            shutil.copy2(env_file, ROOT / ".env")
            log("本地配置 .env：已复用（不下载）", "ok")
        except OSError:
            pass
    if not reused:
        log("没有可直接复用的组件，缺失部分将下载", "dim")


def run_setup(extra: list[str], log) -> int:
    """调用 installer/setup.ps1，把输出实时打到界面。"""
    if not SETUP_PS1.is_file():
        raise FileNotFoundError(
            f"找不到安装脚本：{SETUP_PS1}；请先在「下载 / 更新仓库内容」里同步仓库"
        )
    command = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(SETUP_PS1), *extra,
    ]
    log("$ " + " ".join(command), "dim")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command, cwd=str(ROOT), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=env, creationflags=CREATE_NO_WINDOW,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip())
    code = process.wait()
    if code == 0:
        log("安装/检查完成", "ok")
    else:
        log(f"退出码 {code}，请检查上方输出", "err")
    return code


# ---------------------------------------------------------------- 进程操作


def port_alive(port, timeout: float = 0.35) -> bool:
    if not isinstance(port, int):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def pids_on_port(port: int) -> list[int]:
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=10,
            creationflags=CREATE_NO_WINDOW,
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            tail = line.split()
            if tail and tail[-1].isdigit() and int(tail[-1]) not in pids:
                pids.append(int(tail[-1]))
    return pids


def _acquire_service_lock(name: str, timeout_ms: int = 30000):
    """同一服务的启动/停止跨进程串行：管理器与中枢同时点也只会拉起一份。"""
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
        if wait in (0x0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
            return handle, True
        ctypes.windll.kernel32.CloseHandle(handle)
        return None, False
    except Exception:  # noqa: BLE001 - 拿不到锁时退化为原行为
        return None, True


def _release_service_lock(handle) -> None:
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

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return False


def _kill_pids(pids: list[int]) -> None:
    for pid in pids:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )


def _log_tail(name: str, size: int = 4096) -> str:
    """启动失败时带出日志最后一行，界面直接能看出原因。"""
    try:
        with open(LOG_DIR / f"manage-{name}.log", "rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - size))
            text = stream.read().decode("utf-8", "replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return lines[-1][:240] if lines else ""
    except OSError:
        return ""


def start_service(manifest: dict) -> tuple[bool, str]:
    name = manifest.get("name", "?")
    spec = start_spec(manifest)
    if spec is None:
        entry = (manifest.get("runtime") or {}).get("entry") or manifest.get("entry") or ""
        if name == "astrbot":
            return False, astrbot_missing_hint()
        if name == "napcat" and entry:
            return False, f"未安装：缺少 {entry}，请先点「一键部署」下载安装"
        return False, "没有可执行入口（可能是并入其它服务的子能力）"
    port = spec.get("port")
    lock, acquired = _acquire_service_lock(name)
    if not acquired:
        return False, "另一个启动/停止操作正在进行，请稍后再试"
    try:
        if isinstance(port, int) and port_alive(port):
            return True, f"端口 {port} 已在监听"
        existing = _read_pid(name)
        if _pid_alive(existing):
            # 进程已拉起但端口还在加载（如 CosyVoice 首次加载模型），不能再拉一份
            return True, f"已在启动/运行（PID {existing}），不重复拉起"

        env = os.environ.copy()
        env.update(spec.get("env") or {})
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        log_path = LOG_DIR / f"manage-{name}.log"
        with open(log_path, "ab") as stream:
            process = subprocess.Popen(
                spec["cmd"], cwd=str(spec["cwd"]), stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT, env=env,
                creationflags=CREATE_NO_WINDOW, close_fds=True,
            )
        _write_pid(name, process.pid)
        deadline = time.time() + 20
        while time.time() < deadline and isinstance(port, int) and not port_alive(port):
            if process.poll() is not None:  # 进程提前退出，别傻等
                break
            time.sleep(0.3)
        if not isinstance(port, int):
            return True, "已启动"
        if port_alive(port):
            return True, f"已启动，端口 {port} 就绪"
        if process.poll() is not None:
            _kill_pids([process.pid])
            _clear_pid(name)
            tail = _log_tail(name)
            return False, "启动失败，进程已退出（已回滚）" + (f"：{tail}" if tail else "（看日志）")
        return True, f"已拉起（PID {process.pid}），端口 {port} 还在加载"
    finally:
        _release_service_lock(lock)


def stop_service(manifest: dict) -> tuple[bool, str]:
    name = manifest.get("name", "?")
    port = manifest.get("port")
    if not isinstance(port, int):
        return False, "清单里没有端口，无法定位进程"
    lock, acquired = _acquire_service_lock(name)
    if not acquired:
        return False, "另一个启动/停止操作正在进行，请稍后再试"
    try:
        pids = pids_on_port(port)
        tracked = _read_pid(name)
        if _pid_alive(tracked) and tracked not in pids:
            pids.append(tracked)  # 还没监听端口的加载中进程也要能停
        if not pids:
            _clear_pid(name)
            return True, "已是停止状态"
        _kill_pids(pids)
        deadline = time.time() + 10
        while time.time() < deadline and port_alive(port):
            time.sleep(0.25)
        if port_alive(port):
            return False, f"进程 {pids} 未能退出"
        _clear_pid(name)
        return True, f"已停止（PID {', '.join(map(str, pids))}）"
    finally:
        _release_service_lock(lock)


def sweep_pet() -> None:
    """桌宠可能留下没有端口的 electron / 旁路桥进程，按命令行兜底清理。"""
    pattern = "dsh-pet|dsh-astriax|\\.dsh.electron"
    script = (
        "$pat='{p}';"
        "Get-CimInstance Win32_Process | "
        "Where-Object {{ $_.CommandLine -and $_.CommandLine -match $pat -and $_.ProcessId -ne {me} }} | "
        "ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
    ).format(p=pattern, me=os.getpid())
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=20,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def load_features() -> dict:
    """读取 config/features.json；缺失时生成默认（语音/微信桥默认关闭）。"""
    data = dict(DEFAULT_FEATURES)
    if FEATURES_FILE.is_file():
        try:
            raw = json.loads(FEATURES_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            for key in data:
                if key in raw:
                    data[key] = bool(raw[key])
    else:
        save_features(data)
    data["core"] = True  # 核心运行环境必备，不允许关
    return data


def save_features(data: dict) -> None:
    FEATURES_FILE.parent.mkdir(parents=True, exist_ok=True)
    FEATURES_FILE.write_text(
        json.dumps({k: bool(data.get(k)) for k in DEFAULT_FEATURES}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def features_select(features: dict) -> str:
    """把启用的能力翻译成 setup.ps1 -Select 的编号串。"""
    numbers: list[str] = []
    for key, _label, items in FEATURE_ITEMS:
        if features.get(key):
            numbers.extend(items)
    return ",".join(dict.fromkeys(numbers))


def feature_labels(features: dict) -> list[str]:
    return [label for key, label, _items in FEATURE_ITEMS if features.get(key)]


def one_click_deploy(
    log, source: str | None = None, install: bool = True,
    scope: str | None = None, start: bool = True, profile: str = "all",
) -> str:
    """一键部署：自动更新代码 → 替换补丁 → 按配置安装依赖 → 检查环境 → 重启服务。

    profile="core" 时只装/只起核心链路（桌宠、核心四服务、AstrBot、基础插件、
    NapCat），不碰 CosyVoice、音频/视频/字幕、RAG 与微信桥；"all" 安装全部能力。

    source 留空时自动判断：本机是 git 仓库就先查远端、有新提交就快进更新，
    否则下载 GitHub main 最新代码包；也可以传本机仓库路径或其它 zip 地址。
    显式传 scope 时覆盖 profile 的默认编号串。
    """
    core = profile == "core"
    log("== 0/5 先扫描本机已有组件 ==", "title")
    insts = astrbot_installs()
    if insts:
        for inst in insts[:3]:
            log(f"已装 AstrBot {inst['version']}：{inst['path']}", "dim")
        expected = expected_astrbot_version()
        if expected and insts[0]["key"] < _version_key(expected):
            log(f"已有版本低于清单 {expected}；安装步骤只补依赖，不会覆盖 astrbot-win", "dim")
        else:
            log("已有装配可直接复用；安装步骤只补依赖，不会覆盖 astrbot-win", "dim")
    else:
        log("未检测到已有 AstrBot，将按清单安装", "dim")
    explicit = str(source or "").strip()
    if explicit:
        log(f"== 1/5 同步仓库：{explicit} ==", "title")
        log(sync_repo(log, source=explicit), "ok")
        updated, changed = True, set()
    else:
        log("== 1/5 检查并自动更新代码 ==", "title")
        updated, changed = update_repo(log)

    log("== 1.5/5 复用本机已装组件 ==", "title")
    reuse_local_components(log)

    warnings: list[str] = []
    steps: list[str] = []
    if install:
        if core:
            scope = CORE_STEPS
            log("核心部署：不含语音、音频/视频/字幕、RAG 与微信桥", "dim")
        else:
            scope = scope or ALL_STEPS
            log("全部署：安装全部能力（含 CosyVoice，约 3GB）", "dim")
        steps = [s.strip() for s in str(scope or "").split(",") if s.strip()]
        pet_pkg = Path.home() / ".dsh" / "profiles" / "web" / "node_modules" / "dsh-pet"
        pet_ver = _package_version(pet_pkg)
        source_ver = _pet_source_version()
        if "8" in steps and pet_pkg.is_dir() and pet_ver and pet_ver == source_ver:
            steps = [s for s in steps if s != "8"]
            log(f"桌宠 {pet_ver} 已是最新：跳过重复下载，部署末尾只替换 patches", "dim")
        elif "8" in steps:
            log("桌宠需要安装/更新：本步会下载依赖包并替换补丁", "dim")
        if not steps:
            log("== 2/5 安装依赖：没有需要安装的项 ==", "dim")
        else:
            scope = ",".join(steps)
            log("== 2/5 安装选定依赖 ==", "title")
            setup_args = ["-Select", scope, "-Yes"]
            if core:
                setup_args.append("-Core")
            log(f"$ setup.ps1 {' '.join(setup_args)}", "dim")
            if run_setup(setup_args, log) != 0:
                warnings.append(f"依赖安装未全部成功（-Select {scope}）")
                log("依赖安装未全部成功，继续检查环境并启动可用服务 …", "err")
    else:
        log("== 2/5 安装依赖：已跳过（--no-install）==", "dim")

    log("== 3/5 检查环境 ==", "title")
    if run_setup(["-Check"], log) != 0:
        warnings.append("环境仍有缺口")
        log("环境仍有缺口，见上方清单；继续启动可用服务 …", "err")

    log("== 3.5/5 替换补丁 ==", "title")
    pet_patched, plugin_patched = apply_patches(log)

    if not start:
        log("== 4/5 启动服务：已跳过（--no-start）==", "dim")
        detail = "；".join(warnings) if warnings else "无警告"
        return f"下载/安装/检查完成（{detail}）"

    log("== 4/5 启动服务 ==", "title")
    manifests = [m for m in load_services() if not m.get("merged_into")]
    if core:
        skipped = [m.get("name") for m in manifests if m.get("name") in CORE_SKIP_SERVICES]
        manifests = [m for m in manifests if m.get("name") not in CORE_SKIP_SERVICES]
        if skipped:
            log("核心部署跳过：" + "、".join(sorted(n for n in skipped if n)), "dim")
        running = [
            f"{m.get('name')}({m.get('port')})" for m in load_services()
            if m.get("name") in CORE_SKIP_SERVICES
            and isinstance(m.get("port"), int) and port_alive(m["port"])
        ]
        if running:
            log("注意：这些外围服务此前已在运行，核心部署不会自动停止：" + "、".join(running), "err")
    manifests.sort(
        key=lambda m: (
            LAYER_ORDER.get(m.get("layer", ""), 9),
            START_ORDER.index(m.get("name")) if m.get("name") in START_ORDER else len(START_ORDER),
        )
    )
    # 更新过代码或换过插件/补丁的服务，先停再起，确保跑的是新版本。
    restart_names = affected_services(changed) if updated else set()
    if "4" in steps:
        restart_names.add("astrbot")
    if pet_patched:
        restart_names.add("dsh")
    if plugin_patched:
        restart_names.add("astrbot")
    restart_names &= {m.get("name") for m in manifests}
    if restart_names:
        log("受影响服务将重启：" + "、".join(sorted(restart_names)), "dim")

    failed: list[str] = []
    for manifest in manifests:
        name = manifest.get("name", "?")
        try:
            if (
                name in restart_names
                and isinstance(manifest.get("port"), int)
                and port_alive(manifest["port"])
            ):
                stopped, stop_message = stop_service(manifest)
                if stopped:
                    log(f"重启 {name}：{stop_message}", "dim")
                else:
                    log(f"重启前停止 {name} 失败：{stop_message}", "err")
            ok, message = start_service(manifest)
        except Exception as error:  # noqa: BLE001 - 单个服务失败不影响其余
            ok, message = False, str(error)
        log(f"{'完成' if ok else '失败'} {name}：{message}", "ok" if ok else "err")
        if not ok:
            failed.append(name)

    log("== 5/5 校验端口 ==", "title")
    ports = [
        (m.get("name"), m.get("port")) for m in manifests
        if isinstance(m.get("port"), int)
    ]
    # node 服务（dsh / napcat）起来后还要加载依赖，端口会比进程慢十几秒，这里等一会儿再判定
    deadline = time.time() + 30
    while True:
        down = [f"{n}({p})" for n, p in ports if not port_alive(p)]
        if not down or time.time() >= deadline:
            break
        log("端口还在加载：" + "、".join(down) + "，稍等 …", "dim")
        time.sleep(3)
    # 可选能力（NapCat 要 QQ 登录才有 OneBot 端口、桌宠宿主等）没起来不算部署失败，只提示
    optional_names = {m.get("name") for m in manifests if m.get("optional")}
    hard_failed = [n for n in failed if n not in optional_names]
    hard_down = [d for d in down if d.split("(")[0] not in optional_names]
    if down and hard_down:
        log("未就绪端口：" + "、".join(down), "err")
    elif down:
        log("可选能力未就绪（不影响部署）：" + "、".join(down), "dim")
    else:
        log(f"端口全部就绪：{len(ports)} 个", "ok")
    if hard_failed or hard_down:
        raise RuntimeError(
            "；".join(filter(None, [
                f"未启动：{'、'.join(hard_failed)}" if hard_failed else "",
                f"端口未就绪：{'、'.join(hard_down)}" if hard_down else "",
            ]))
        )
    detail = f"；警告：{'；'.join(warnings)}" if warnings else ""
    title = "核心部署完成" if core else "全部署完成"
    return f"{title}：{len(manifests)} 个服务，{len(ports)} 个端口就绪{detail}"


# ---------------------------------------------------------------- 界面


def enable_dpi_awareness() -> None:
    """开启 DPI 感知：125%/150% 缩放下文字不再模糊、控件也不会被裁掉。"""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - Win7 没有 shcore
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def apply_window_icon(win: tk.Misc) -> None:
    """给窗口（含后续 Toplevel）挂肥鱼图标；缺图标文件时静默跳过。"""
    ico = ASSETS / "dafeiyu.ico"
    if not ico.is_file():
        return
    try:
        win.iconbitmap(default=str(ico))
    except tk.TclError:
        pass


def pick_font(root: tk.Misc, candidates: list[str]) -> str:
    """挑一个系统里真实存在的字体，避免中文/英文系统出现方框或回退错乱。"""
    available = {name.lower() for name in tkfont.families(root)}
    for name in candidates:
        if name.lower() in available:
            return name
    return str(tkfont.nametofont("TkDefaultFont").actual("family"))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.services = load_services()
        self.by_name = {m["name"]: m for m in self.services}
        self.states: dict[str, bool] = {}
        self.rows: dict[str, dict] = {}
        self.events: queue.Queue = queue.Queue()
        self.busy: set[str] = set()
        self.failed: dict[str, str] = {}
        self._refreshing = False
        self._task_win: tk.Toplevel | None = None
        self._task_text: tk.Text | None = None
        self._task_source: tk.StringVar | None = None
        self._task_buttons: list[tk.Button] = []
        self._task_running = False

        family = pick_font(root, ["Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "PingFang SC", "Segoe UI", "Helvetica"])
        mono = pick_font(root, ["Cascadia Mono", "Consolas", "Courier New", "Courier"])
        self.font = tkfont.Font(family=family, size=10)
        self.font_bold = tkfont.Font(family=family, size=10, weight="bold")
        self.font_small = tkfont.Font(family=family, size=9)
        self.font_title = tkfont.Font(family=family, size=15, weight="bold")
        self.font_dot = tkfont.Font(family=family, size=11)
        self.font_mono = tkfont.Font(family=mono, size=9)
        self._desc_labels: list[tk.Label] = []
        self._rows_width = 0

        self._build()
        self.refresh()
        self._poll()

    # ---------- 布局

    def _build(self) -> None:
        self.root.title("DeepSeek大肥鱼 服务管理器")
        self.root.configure(bg=BG)
        self._desc_labels = []
        self._wrap = 0
        self._features = load_features()
        dpi = self.root.winfo_fpixels("1i") / 96.0
        try:
            self.root.tk.call("tk", "scaling", self.root.winfo_fpixels("1i") / 72.0)
        except tk.TclError:
            pass
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width = max(760, min(int(1180 * dpi), int(screen_w * 0.9)))
        height = max(480, min(int(780 * dpi), int(screen_h * 0.86)))
        self.root.geometry(
            f"{width}x{height}+{(screen_w - width) // 2}+{max(0, (screen_h - height) // 2 - 24)}"
        )
        self.root.minsize(
            min(int(760 * dpi), int(screen_w * 0.62)),
            min(int(480 * dpi), int(screen_h * 0.55)),
        )

        apply_window_icon(self.root)
        self._icon_img = None
        icon_png = ASSETS / "dafeiyu_256.png"
        if icon_png.is_file():
            try:
                self._icon_img = tk.PhotoImage(file=str(icon_png))
                self.root.iconphoto(True, self._icon_img)
            except tk.TclError:
                self._icon_img = None
        self._summary_text = ""

        self.header = tk.Canvas(self.root, height=int(104 * dpi), bd=0, highlightthickness=0, bg=BG_TOP)
        self.header.pack(fill="x")
        self.header.bind("<Configure>", self._paint_header)
        self._header_icon = None
        icon_small = ASSETS / "dafeiyu_96.png"
        if icon_small.is_file():
            try:
                self._header_icon = tk.PhotoImage(file=str(icon_small))
            except tk.TclError:
                self._header_icon = None

        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=20, pady=10)
        tool_buttons = [
            self._tool_button(bar, "核心部署", lambda: self.deploy("core"), GREEN),
            self._tool_button(bar, "全部署", lambda: self.deploy("all"), ACCENT),
            self._tool_button(bar, "全部启动", self.start_all, ACCENT),
            self._tool_button(bar, "全部停止", self.stop_all, RED),
            self._tool_button(bar, "刷新状态", self.refresh),
            self._tool_button(bar, "重载清单", self.reload_services),
            self._tool_button(bar, "外部服务", self.open_endpoints),
            self._tool_button(bar, "附属能力", self.open_aux),
            self._tool_button(bar, "部署配置", self.open_features),
            self._tool_button(bar, "部署 / 日志", self.open_tasks, AMBER),
            self._tool_button(bar, "中枢面板", self.open_hub),
            self._tool_button(bar, "部署目录", lambda: self._open_path(ROOT)),
            self._tool_button(bar, "插件路径", self.open_plugins),
            self._tool_button(bar, "卸载", self.uninstall, RED),
        ]
        self._flow_buttons(bar, tool_buttons)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=20)
        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self.list_frame.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self._window = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw", width=960)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        step = 1.0 / max(1, len(self.services) - 1)
        for index, manifest in enumerate(self.services):
            # 卡片底色从上到下轻微加深，形成梦幻蓝渐变
            self._add_row(manifest, _blend(CARD, CARD_END, index * step))

        footer = tk.Frame(self.root, bg=FOOTER_BG, height=34)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        self.status = tk.Label(
            footer, text="就绪", bg=FOOTER_BG, fg=MUTED, font=self.font_small, anchor="w",
        )
        self.status.pack(side="left", padx=16)
        tk.Label(
            footer, text="每 4 秒自动刷新 · 关闭窗口不会停止服务", bg=FOOTER_BG, fg=DIM,
            font=self.font_small,
        ).pack(side="right", padx=16)

    def _paint_header(self, event=None) -> None:
        """标题区：梦幻蓝竖向渐变 + 肥鱼图标 + 路径与在线数。"""
        width = self.header.winfo_width()
        height = self.header.winfo_height()
        if width <= 1 or height <= 1:
            return
        size = (width, height)
        if getattr(self, "_header_size", None) != size:
            paint_gradient(self.header, width, height, BG_TOP, HEADER_BOTTOM, "bg")
            self._header_size = size
        self.header.delete("ui")
        x = 28
        if self._header_icon is not None:
            self.header.create_image(24, height // 2, image=self._header_icon, anchor="w", tags="ui")
            x = 24 + self._header_icon.width() + 20
        self.header.create_text(
            x, height // 2 - 16, text="DeepSeek大肥鱼 服务管理器", fill=FG, font=self.font_title,
            anchor="w", tags="ui",
        )
        self.header.create_text(
            x, height // 2 + 16, text=str(ROOT), fill=DIM, font=self.font_small,
            anchor="w", tags="ui",
        )
        self.header.create_text(
            width - 24, height // 2, text=self._summary_text, fill=ACCENT,
            font=self.font_small, anchor="e", tags="ui",
        )
        self.header.tag_lower("bg")

    def reload_services(self) -> None:
        """重读 services/*/service.json —— 同步仓库后新增/删除的服务不用重启面板。"""
        self.services = load_services()
        self.by_name = {m["name"]: m for m in self.services}
        self.states.clear()
        self.rows.clear()
        self.busy.clear()
        for child in self.root.winfo_children():
            child.destroy()
        self._build()
        self.refresh()
        self._set_status(f"已重载服务清单：{len(self.services)} 项｜{self._scan_astrbot()}")

    def _scan_astrbot(self) -> str:
        """启动前先扫描本机已有 AstrBot（含部署目录外的旧装），并在状态栏说清版本。"""
        insts = astrbot_installs()
        expected = expected_astrbot_version()
        if not insts:
            return "未检测到 AstrBot"
        best = insts[0]
        if expected:
            if best["key"] < _version_key(expected):
                tag = f"低于清单 {expected}"
            elif best["key"] > _version_key(expected):
                tag = f"高于清单 {expected}"
            else:
                tag = f"与清单 {expected} 一致"
        else:
            tag = "已安装"
        return f"AstrBot {best['version']}（{tag}）"

    def _tool_button(self, parent, text, command, color=FG):
        return tk.Button(
            parent, text=text, command=command, bg=CARD, fg=color, font=self.font_small,
            activebackground=CARD_HOVER, activeforeground=FG, relief="flat", bd=0,
            padx=14, pady=6, cursor="hand2", highlightthickness=1,
            highlightbackground=BORDER,
        )

    def _flow_buttons(self, bar: tk.Frame, buttons: list[tk.Button]) -> None:
        """按钮按可用宽度自动换行：小屏/高缩放下不会被挤出窗口。"""
        state = {"width": 0}

        def reflow(_event=None) -> None:
            width = bar.winfo_width()
            if width <= 1 or abs(width - state["width"]) < 24:
                return
            state["width"] = width
            for button in buttons:
                button.grid_forget()
            row = column = used = 0
            for button in buttons:
                need = button.winfo_reqwidth() + 8
                if used and used + need > width:
                    row += 1
                    column = used = 0
                button.grid(row=row, column=column, padx=(0, 8), pady=2, sticky="w")
                used += need
                column += 1

        bar.bind("<Configure>", reflow)
        self.root.after_idle(reflow)

    def _on_canvas_resize(self, event) -> None:
        """列表宽度跟随窗口，描述文本按新宽度重新折行。"""
        width = max(400, event.width - 4)
        self.canvas.itemconfigure(self._window, width=width)
        self._rows_width = width
        wrap = max(200, width - 520)
        if abs(wrap - getattr(self, "_wrap", 0)) < 20:
            return
        self._wrap = wrap
        for label in self._desc_labels:
            try:
                label.configure(wraplength=wrap)
            except tk.TclError:
                pass

    def _add_row(self, manifest: dict, card: str = CARD) -> None:
        name = manifest["name"]
        merged = manifest.get("merged_into")
        row = tk.Frame(self.list_frame, bg=card, highlightthickness=1, highlightbackground=BORDER)
        row.pack(fill="x", pady=4)

        left = tk.Frame(row, bg=card)
        left.pack(side="left", fill="x", expand=True, padx=14, pady=10)

        dot = tk.Label(left, text="●", bg=card, fg=RED, font=self.font_dot)
        dot.pack(side="left", padx=(0, 8))
        tk.Label(left, text=name, bg=card, fg=FG, font=self.font_bold, width=11, anchor="w").pack(side="left")
        desc = str(manifest.get("responsibility") or "")
        if merged and manifest.get("runtime"):
            # 归属 audio 域但依赖独立 venv/GPU，进程仍要能单独启停
            desc = f"独立进程 · 归属 {merged} · {desc}"
        children = manifest.get("_merged") or []
        if children:
            desc = f"含 {'、'.join(children)} · {desc}"
        feature = FEATURE_SERVICES.get(str(manifest.get("name") or ""))
        if feature and not self._features.get(feature, True):
            desc = f"未启用（部署配置） · {desc}"
        right = tk.Frame(row, bg=card)
        right.pack(side="right", padx=12)

        state = tk.Label(right, text="检测中", bg=card, fg=MUTED, font=self.font_small, width=6)
        state.pack(side="left", padx=(0, 8))

        disabled = bool(merged) and not manifest.get("runtime")
        buttons = {}
        for text, action in (("启动", "start"), ("停止", "stop"), ("重启", "restart")):
            btn = tk.Button(
                right, text=text, bg=card, fg=ACCENT if action == "start" else FG,
                font=self.font_small, activebackground=CARD_HOVER, activeforeground=FG,
                relief="flat", bd=0, padx=9, pady=3, cursor="hand2",
                state="disabled" if disabled else "normal",
                command=lambda a=action, m=manifest: self.act(m, a),
            )
            btn.pack(side="left", padx=2)
            buttons[action] = btn
        for text, command in (("页面", lambda m=manifest: self.open_page(m)),
                              ("日志", lambda m=manifest: self.open_log(m)),
                              ("详情", lambda m=manifest, d=desc: self._show_detail(m, d))):
            tk.Button(
                right, text=text, bg=card, fg=MUTED, font=self.font_small,
                activebackground=CARD_HOVER, activeforeground=FG, relief="flat", bd=0,
                padx=9, pady=3, cursor="hand2", command=command,
            ).pack(side="left", padx=2)
        if not disabled:
            tk.Button(
                right, text="卸载", bg=card, fg=RED, font=self.font_small,
                activebackground=CARD_HOVER, activeforeground=RED, relief="flat", bd=0,
                padx=9, pady=3, cursor="hand2",
                command=lambda m=manifest: self.uninstall_service(m),
            ).pack(side="left", padx=2)

        self.rows[name] = {"dot": dot, "state": state, "buttons": buttons, "manifest": manifest}

    def _show_detail(self, manifest: dict, desc: str) -> None:
        """层级、端口、目录、说明收进详情，主列表保持精简。"""
        try:
            rel_dir = str(manifest["_dir"].relative_to(ROOT))
        except (KeyError, ValueError):
            rel_dir = str(manifest.get("_dir", ""))
        port = manifest.get("port")
        messagebox.showinfo(
            f"{manifest.get('name')} 详情",
            f"层级：{manifest.get('layer') or '-'}\n"
            f"端口：{port or '-'}\n"
            f"目录：{rel_dir}\n\n"
            f"{desc or '-'}",
        )

    def uninstall_service(self, manifest: dict) -> None:
        """单个服务卸载：停进程 → 删 services/<name>，默认保留该服务的 data。"""
        name = manifest.get("name", "?")
        folder = manifest.get("_dir")
        if not isinstance(folder, Path):
            messagebox.showerror("无法卸载", f"{name} 没有独立服务目录，无法单独卸载。")
            return
        try:
            folder = folder.resolve()
            folder.relative_to((ROOT / "services").resolve())
        except (OSError, ValueError):
            messagebox.showerror(
                "无法卸载",
                f"{name} 的服务目录不在 services 下：\n{folder}\n\n"
                "请用主工具栏的「卸载」整体卸载。",
            )
            return
        answer = messagebox.askyesnocancel(
            f"卸载 {name}",
            f"将停止 {name}，并删除：\n{folder}\n\n"
            "「是」= 卸载，保留数据（该服务的 data 目录）\n"
            "「否」= 卸载，连数据一起删除\n"
            "「取消」= 不动\n\n"
            "只删这个服务的目录；AstrBot、NapCat、桌宠本体不受影响。",
        )
        if answer is None or name in self.busy:
            return
        keep_data = answer
        self.busy.add(name)
        row = self.rows.get(name)
        if row:
            row["state"].configure(text="卸载中", fg=RED)
            for btn in row["buttons"].values():
                btn.configure(state="disabled")
        self._set_status(f"正在卸载 {name} …", RED)

        def worker() -> None:
            try:
                stop_service(manifest)
                time.sleep(0.8)
                left = []
                for child in folder.iterdir():
                    if keep_data and child.name == "data":
                        continue
                    try:
                        if child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                    except OSError:
                        left.append(child.name)
                if left:
                    ok, msg = False, "删不掉：" + "、".join(left[:4]) + "（可能被占用）"
                else:
                    ok, msg = True, "已卸载" + ("（保留 data）" if keep_data else "（含数据）")
            except Exception as error:
                ok, msg = False, f"卸载失败：{error}"
            message = f"卸载 {name}：{msg}"
            self.events.put(("done", name, ok, message))
            if ok:
                self.events.put(("reload", message))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 状态刷新

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True

        def worker():
            ports = [(m["name"], m.get("port")) for m in self.services]
            with ThreadPoolExecutor(max_workers=12) as pool:
                result = list(pool.map(lambda item: (item[0], port_alive(item[1])), ports))
            self.events.put(("states", dict(result)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_states(self, states: dict) -> None:
        self.states = states
        for name, row in self.rows.items():
            if name in self.busy:  # 动作进行中，保留「启动中 / 停止中」
                continue
            alive = bool(states.get(name))
            starting = not alive and _pid_alive(_read_pid(name))
            if not alive and not starting:
                _clear_pid(name)
            row["dot"].configure(fg=GREEN if alive else (AMBER if starting else RED))
            if alive:
                self.failed.pop(name, None)  # 后来被别处拉起/恢复，失败提示自动消失
                row["state"].configure(text="运行中", fg=GREEN)
            elif name in self.failed:
                row["state"].configure(text=self.failed[name], fg=RED)
            elif starting:
                row["state"].configure(text="启动中", fg=AMBER)
            else:
                row["state"].configure(text="已停止", fg=RED)
        # 有独立进程的（如 voice）即使归属 audio 域也要计入在线数
        counted = [m for m in self.services if not m.get("merged_into") or m.get("runtime")]
        online = sum(1 for m in counted if states.get(m["name"]))
        total = len(counted)
        self._summary_text = f"在线 {online} / {total}"
        self._paint_header()

    def _set_status(self, text: str, color: str = MUTED) -> None:
        self.status.configure(text=text, fg=color)

    # ---------- 动作

    def act(self, manifest: dict, action: str) -> None:
        name = manifest["name"]
        if name in self.busy:
            return
        label = {"start": "启动", "stop": "停止", "restart": "重启"}[action]
        progress = {"start": "启动中", "stop": "停止中", "restart": "重启中"}[action]
        self.busy.add(name)
        self.failed.pop(name, None)
        row = self.rows.get(name)
        if row:
            row["state"].configure(text=progress, fg=AMBER)
            for btn in row["buttons"].values():
                btn.configure(state="disabled")
        self._set_status(f"正在{label} {name} …", ACCENT)

        def worker():
            try:
                if action == "start":
                    ok, msg = start_service(manifest)
                elif action == "stop":
                    ok, msg = stop_service(manifest)
                else:
                    ok, _ = stop_service(manifest)
                    ok2, msg = start_service(manifest)
                    ok = ok and ok2
                if name == "dsh" and action in {"stop", "restart"}:
                    sweep_pet()
            except Exception as error:  # noqa: BLE001 - 界面只回消息
                ok, msg = False, f"{label}失败：{error}"
            self.events.put(("done", name, ok, f"{label} {name}：{msg}"))

        threading.Thread(target=worker, daemon=True).start()

    def start_all(self) -> None:
        if not messagebox.askyesno("全部启动", "按依赖顺序启动全部服务？\n桌宠窗口也会一起打开。"):
            return
        self._batch("start")

    def stop_all(self) -> None:
        if not messagebox.askyesno("全部停止", "停止全部大肥鱼服务？\nOllama 与其它程序不受影响。"):
            return
        self._batch("stop")

    def _batch(self, action: str) -> None:
        ordered = [self.by_name[n] for n in START_ORDER if n in self.by_name]
        ordered += [m for m in self.services if m not in ordered]
        if action == "stop":
            ordered.reverse()
        queues = [m for m in ordered if not m.get("merged_into")]
        for manifest in queues:
            self.busy.add(manifest["name"])
            for btn in self.rows[manifest["name"]]["buttons"].values():
                btn.configure(state="disabled")
        label = "启动" if action == "start" else "停止"
        self._set_status(f"批量{label}中，共 {len(queues)} 个服务 …", ACCENT)

        def worker():
            for manifest in queues:
                name = manifest["name"]
                try:
                    ok, msg = (start_service if action == "start" else stop_service)(manifest)
                except Exception as error:  # noqa: BLE001
                    ok, msg = False, str(error)
                self.events.put(("done", name, ok, f"{label} {name}：{msg}"))
                if action == "stop" and name == "dsh":
                    sweep_pet()
            self.events.put(("batch_done", action))

        threading.Thread(target=worker, daemon=True).start()

    AUX_VIDEO_URL = "http://127.0.0.1:23368/ui/feed"

    def _plugin_conf(self, name: str) -> dict:
        """读插件实际生效的 config.json（AstrBot 已装目录优先）。"""
        paths = [
            base / "data" / "config" / f"astrbot_plugin_{name}_config.json"
            for base in sorted((ROOT / "astrbot-win").glob("AstrBot-*"))
        ]
        for root in plugin_roots():
            # 仓库分包（input/output/function）与旧平铺布局都兼容
            paths += [p / "config.json" for p in sorted(root.glob(f"*/astrbot_plugin_{name}"))]
            paths += [p / "config.json" for p in sorted(root.glob(f"astrbot_plugin_{name}"))]
        for path in paths:
            try:
                # AstrBot 写出的配置可能带 UTF-8 BOM，用 utf-8-sig 两种都能读
                with open(path, encoding="utf-8-sig") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                continue
        return {}

    def open_aux(self) -> None:
        """附属能力：不在服务卡片里的外部组件（视频主页 / 赞助页 / 樱花）。"""
        win = tk.Toplevel(self.root)
        win.title("附属能力")
        win.configure(bg=BG)
        win.geometry("800x540")
        box = tk.Frame(win, bg=BG)
        box.pack(fill="both", expand=True, padx=18, pady=14)
        tk.Label(
            box, text="这些不在服务卡片里：有的是外部客户端，有的不随仓库分发",
            bg=BG, fg=MUTED, font=self.font_small, anchor="w",
        ).pack(fill="x", pady=(0, 6))

        def add(title: str, state: str, color: str, detail: str, buttons: list) -> None:
            card = tk.Frame(box, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
            card.pack(fill="x", pady=5)
            body = tk.Frame(card, bg=CARD)
            body.pack(side="left", fill="x", expand=True, padx=14, pady=10)
            tk.Label(body, text=title, bg=CARD, fg=FG, font=self.font_bold, anchor="w").pack(anchor="w")
            tk.Label(
                body, text=detail, bg=CARD, fg=MUTED, font=self.font_small,
                anchor="w", justify="left", wraplength=430,
            ).pack(anchor="w", pady=(3, 0))
            right = tk.Frame(card, bg=CARD)
            right.pack(side="right", padx=12)
            tk.Label(right, text=state, bg=CARD, fg=color, font=self.font_small).pack(side="left", padx=(0, 8))
            for text, cmd in buttons:
                tk.Button(
                    right, text=text, bg=CARD, fg=ACCENT, font=self.font_small,
                    activebackground=CARD_HOVER, activeforeground=FG, relief="flat", bd=0,
                    padx=9, pady=3, cursor="hand2", command=cmd,
                ).pack(side="left", padx=2)

        def state_of(alive: bool, on_text: str = "运行中") -> tuple[str, str]:
            return (on_text, GREEN) if alive else ("未运行", RED)

        # 视频主页
        add(
            "视频主页（刷 B 站）", *state_of(port_alive(23368)),
            "按标签取片单、维护片单池；视频只在页面里浏览，不把视频内容发进 QQ。",
            [("打开", lambda: webbrowser.open(self.AUX_VIDEO_URL))],
        )

        # 刷视频对外地址（樱花隧道 + recharge_page 的 /bili）
        feed_url = str(self._plugin_conf("web_lookup").get("video_page_url") or "").strip()
        feed_state, feed_color = ("已配置", GREEN) if feed_url else ("未配置", RED)
        add(
            "刷视频对外地址", feed_state, feed_color,
            feed_url or "未配置：不发视频页链接给好友（127.0.0.1 回环地址对方打不开；本机 8712 服务仍在，仅供音乐分享等本机使用）。",
            [("打开", lambda: webbrowser.open(feed_url))] if feed_url
            else [("说明", self._aux_hint_feed)],
        )

        # 赞助 / 充值页
        recharge_dir = ROOT / "recharge_page"
        serve = recharge_dir / "serve.py"
        base = str(self._plugin_conf("human_sim").get("recharge_link_base") or "").strip()
        if serve.is_file():
            detail = f"本机 http://127.0.0.1:25565/（档位列表）\n对外：{base or '未配置 recharge_link_base'}"
        else:
            detail = "没有找到 recharge_page/serve.py（不随仓库分发，需自备收款码）"
        add(
            "赞助 / 充值页", *state_of(port_alive(25565)),
            detail,
            [("打开", lambda: webbrowser.open("http://127.0.0.1:25565/")),
             ("启动", lambda: self._aux_start_recharge(serve, recharge_dir)),
             ("目录", lambda: self._open_path(recharge_dir))],
        )

        # 樱花内网穿透
        add(
            "樱花 SakuraFrp（内网穿透）", *state_of(self._aux_frpc_running()),
            "外部客户端，仓库不分发。隧道：25565 → 赞助页 + /bili 视频页，23408 → 控制台。",
            [("官网", lambda: webbrowser.open("https://www.natfrp.com/tunnel")),
             ("配置", self._aux_open_frp_dir)],
        )

        # 本地记忆模型（Ollama）
        models = self._ollama_model_text()
        if not port_alive(11434):
            mem_state, mem_color = "未运行", RED
            mem_detail = (
                "11434 无监听：记忆总结和向量检索都会失效，聊天回复不受影响（走 DeepSeek）。\n"
                "Ollama 是桌面客户端，关掉窗口就掉线，建议设为开机自启。"
            )
        else:
            missing = [n for n in ("qwen3.5", "nomic-embed-text") if n not in models]
            if missing:
                mem_state, mem_color = "缺模型", RED
                mem_detail = "缺少：" + "、".join(missing) + "；在命令行执行 ollama pull " + missing[0]
            else:
                mem_state, mem_color = "正常", GREEN
                mem_detail = "qwen3.5:9b（备用）+ nomic-embed-text（向量）就绪。"
        add(
            "本地记忆模型（Ollama）", mem_state, mem_color, mem_detail,
            [("启动", self._aux_start_ollama), ("说明", self._aux_hint_ollama)],
        )

    def _ollama_model_text(self) -> str:
        exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if not exe.is_file():
            exe = Path("ollama.exe")
        try:
            out = subprocess.run(
                [str(exe), "list"], capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout or ""
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.lower()

    def _aux_start_ollama(self) -> None:
        exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if not exe.is_file():
            messagebox.showerror("无法启动", "没有找到 ollama.exe，请先安装 Ollama。")
            return
        try:
            subprocess.Popen(
                [str(exe), "serve"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            messagebox.showerror("启动失败", str(error))
            return
        self._set_status("已启动 Ollama（11434）", GREEN)

    def _aux_hint_ollama(self) -> None:
        messagebox.showinfo(
            "本地记忆模型",
            "记忆总结走 DeepSeek，只有向量检索依赖本机 Ollama。\n\n"
            "1. 安装：https://ollama.com/download\n"
            "2. 拉模型：\n   ollama pull qwen3.5:9b\n   ollama pull nomic-embed-text\n"
            "3. 设为开机自启，否则关掉客户端窗口记忆就失效。\n\n"
            "核心部署 / 全部署都不会自动安装 Ollama，新机器请先装好再启动机器人。",
        )

    def _aux_hint_feed(self) -> None:
        messagebox.showinfo(
            "刷视频对外地址",
            "在 web_lookup 插件配置里填 video_page_url，例如：\n"
            "  https://你的樱花域名:52199/bili\n\n"
            "留空就不发链接（127.0.0.1:8712 是回环地址，发出去好友也打不开；"
            "本机 8712 服务仍在跑，音乐分享等本机功能不受影响）。",
        )

    def _aux_start_recharge(self, serve: Path, folder: Path) -> None:
        if not serve.is_file():
            messagebox.showerror("无法启动", f"没有找到：{serve}")
            return
        py = VENV_PY if VENV_PY.is_file() else Path(sys.executable)
        try:
            subprocess.Popen(
                [str(py), str(serve)], cwd=str(folder),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        except OSError as error:
            messagebox.showerror("启动失败", str(error))
            return
        self._set_status("已启动赞助 / 充值页（25565）", GREEN)

    def _aux_frpc_running(self) -> bool:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq frpc.exe", "/NH"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return "frpc.exe" in out.lower()

    def _aux_open_frp_dir(self) -> None:
        path = Path(r"C:\ProgramData\SakuraFrpService")
        if path.is_dir():
            os.startfile(str(path))  # noqa: S606
        else:
            messagebox.showinfo("樱花", "没有找到 C:\\ProgramData\\SakuraFrpService，可能还没装客户端。")

    # ---------- 打开页面 / 日志

    def open_page(self, manifest: dict) -> None:
        url = page_url(manifest)
        if not url:
            messagebox.showinfo("没有页面", f"{manifest['name']} 没有配置页面地址")
            return
        webbrowser.open(url)

    def open_hub(self) -> None:
        manifest = self.by_name.get("hub")
        if manifest and not self.states.get("hub"):
            ok, msg = start_service(manifest)
            self._set_status(f"启动 hub：{msg}", GREEN if ok else RED)
        webbrowser.open("http://127.0.0.1:23408")

    def open_log(self, manifest: dict) -> None:
        name = manifest["name"]
        files = [LOG_DIR / f"manage-{name}.log", ROOT / "logs" / f"{name}-core.log"]
        files = [p for p in files if p.is_file()]
        if not files:
            messagebox.showinfo("暂无日志", f"{name} 还没有启动日志")
            return

        win = tk.Toplevel(self.root)
        apply_window_icon(win)
        win.title(f"{name} 日志")
        win.geometry("900x560")
        win.configure(bg=BG)
        text = tk.Text(
            win, bg=LOG_BG, fg=LOG_FG, insertbackground=FG, font=("Consolas", 9),
            relief="flat", wrap="none",
        )
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.tag_configure("path", foreground=ACCENT)
        text.tag_configure("err", foreground=RED)

        def reload():
            text.configure(state="normal")
            text.delete("1.0", "end")
            for path in files:
                text.insert("end", f"== {path} ==\n", "path")
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
                except OSError as error:
                    lines = [f"读取失败：{error}"]
                for line in lines:
                    tag = "err" if ("Traceback" in line or "Error" in line or "error" in line) else ""
                    text.insert("end", line + "\n", tag)
                text.insert("end", "\n")
            text.configure(state="disabled")
            text.see("end")

        reload()
        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(
            bar, text="刷新", command=reload, bg=CARD, fg=FG, relief="flat", bd=0,
            padx=12, pady=4, cursor="hand2", activebackground=CARD_HOVER,
        ).pack(side="left")

    # ---------- 下载 / 安装

    def open_endpoints(self) -> None:
        """统一外部依赖配置：BASE_URL / API_KEY / MODEL（环境变量优先级更高）。"""
        path = ROOT / "config" / "endpoints.json"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            template = {
                "voice": {"base_url": "", "api_key": "", "model": "", "voice": ""},
                "rag": {"base_url": "", "api_key": ""},
                "model": {"base_url": "", "api_key": "", "model": ""},
                "stickers": {"base_url": "", "api_key": ""},
            }
            path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.startfile(str(path))  # noqa: S606 用系统默认编辑器打开
        except OSError as exc:
            messagebox.showerror("打不开外部服务配置", str(exc))

    def open_features(self) -> None:
        """可选能力开关：控制面板显示哪些服务卡片；部署档位由「核心部署 / 全部署」决定。"""
        load_features()  # 缺文件时生成默认配置
        try:
            os.startfile(str(FEATURES_FILE))  # noqa: S606 用系统默认编辑器打开
        except OSError as exc:
            messagebox.showerror("打不开部署配置", str(exc))

    def open_plugins(self) -> None:
        """列出服务与插件目录，方便核对相对路径。"""
        win = tk.Toplevel(self.root)
        apply_window_icon(win)
        win.title("插件路径")
        win.configure(bg=BG)
        screen_w, screen_h = win.winfo_screenwidth(), win.winfo_screenheight()
        width = min(860, int(screen_w * 0.62))
        height = min(580, int(screen_h * 0.62))
        win.geometry(f"{width}x{height}+{(screen_w - width) // 2}+{(screen_h - height) // 2}")
        tk.Label(
            win, text="插件目录（相对 FlatFish 根目录）", bg=BG, fg=FG, font=self.font_bold,
        ).pack(anchor="w", padx=16, pady=(14, 6))
        text = tk.Text(
            win, bg=LOG_BG, fg=LOG_FG, insertbackground=FG, font=("Consolas", 9),
            relief="flat", wrap="none",
        )
        text.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        lines: list[str] = []
        for root_dir in plugin_roots():
            try:
                rel_root = root_dir.relative_to(ROOT)
            except ValueError:
                rel_root = root_dir
            lines.append(f"[{rel_root}]")
            for item in sorted(root_dir.iterdir()):
                if not item.is_dir():
                    continue
                try:
                    rel = item.relative_to(ROOT)
                except ValueError:
                    rel = item
                if item.name.startswith("astrbot_plugin_"):
                    lines.append(f"  {item.name:<40} 目录 {rel}")
                else:
                    # 仓库分包目录（input/output/function），列一层里面的插件
                    lines.append(f"  [{item.name}]")
                    for sub in sorted(item.iterdir()):
                        if not sub.is_dir():
                            continue
                        try:
                            rel_sub = sub.relative_to(ROOT)
                        except ValueError:
                            rel_sub = sub
                        lines.append(f"    {sub.name:<38} 目录 {rel_sub}")
            lines.append("")
        text.insert("1.0", "\n".join(lines).strip() or "未找到插件目录")
        text.configure(state="disabled")

    def open_tasks(self) -> None:
        if self._task_win is not None and self._task_win.winfo_exists():
            self._task_win.deiconify()
            self._task_win.lift()
            return

        win = tk.Toplevel(self.root)
        apply_window_icon(win)
        win.title("下载 / 安装依赖")
        screen_w, screen_h = win.winfo_screenwidth(), win.winfo_screenheight()
        width = max(660, min(940, int(screen_w * 0.66)))
        height = max(430, min(660, int(screen_h * 0.7)))
        win.geometry(f"{width}x{height}+{(screen_w - width) // 2}+{(screen_h - height) // 2}")
        win.minsize(min(600, width), min(360, height))
        win.configure(bg=BG)
        win.transient(self.root)
        self._task_win = win

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(head, text="下载与依赖", bg=BG, fg=FG, font=self.font_bold).pack(side="left")
        tk.Label(
            head, text=f"{REPO_SLUG} · {REPO_BRANCH}", bg=BG, fg=MUTED, font=self.font_small,
        ).pack(side="right")

        hint = tk.Label(
            win,
            text="本机仓库会带上未提交改动；GitHub 为 main 最新提交。"
                 "同步与安装都会保留 data / venv / .env / 第三方组件。",
            bg=BG, fg=DIM, font=self.font_small, anchor="w", justify="left",
            wraplength=max(300, width - 40),
        )
        hint.pack(fill="x", padx=14)
        win.bind(
            "<Configure>",
            lambda event: hint.configure(wraplength=max(260, event.width - 40)),
            add="+",
        )

        source_row = tk.Frame(win, bg=BG)
        source_row.pack(fill="x", padx=14, pady=(6, 0))
        tk.Label(source_row, text="来源", bg=BG, fg=MUTED, font=self.font_small).pack(side="left", padx=(0, 8))
        local = detect_local_repo()
        self._task_source = tk.StringVar(value=str(local) if local else REPO_ZIP)
        tk.Entry(
            source_row, textvariable=self._task_source, bg=LOG_BG, fg=FG,
            insertbackground=FG, relief="flat", font=self.font_mono,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        ).pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(
            source_row, text="本机仓库", command=lambda: self._task_source.set(str(local or "")),
            bg=CARD, fg=FG if local else MUTED, font=self.font_small, relief="flat", bd=0,
            padx=10, pady=3, cursor="hand2", activebackground=CARD_HOVER,
            state="normal" if local else "disabled",
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            source_row, text="GitHub", command=lambda: self._task_source.set(REPO_ZIP),
            bg=CARD, fg=FG, font=self.font_small, relief="flat", bd=0,
            padx=10, pady=3, cursor="hand2", activebackground=CARD_HOVER,
        ).pack(side="left", padx=(6, 0))

        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", padx=14, pady=8)
        self._task_buttons = []
        for text, command, color in (
            ("下载 / 更新仓库内容", self.download_repo, FG),
            ("检查环境", self.check_env, FG),
            ("安装语音 CosyVoice2", lambda: self.install_deps("voice"), FG),
        ):
            self._task_buttons.append(self._tool_button(bar, text, command, color))
        self._flow_buttons(bar, self._task_buttons)
        if self._task_running:
            self._task_set_buttons(False)

        box = tk.Frame(win, bg=BG)
        box.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        text = tk.Text(
            box, bg=LOG_BG, fg=LOG_FG, insertbackground=FG,
            font=self.font_mono, relief="flat", wrap="word",
        )
        scroll = tk.Scrollbar(box, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        text.tag_configure("title", foreground=ACCENT)
        text.tag_configure("ok", foreground=GREEN)
        text.tag_configure("err", foreground=RED)
        text.tag_configure("dim", foreground=MUTED)
        text.configure(state="disabled")
        self._task_text = text

        def close():
            self._task_text = None
            self._task_win = None
            self._task_source = None
            self._task_buttons = []
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)
        self._task_log(f"部署目录：{ROOT}", "dim")
        self._task_log("提示：下载或安装完成后，重启本面板可加载新服务与界面。", "dim")

    def _task_log(self, message: str, tag: str = "") -> None:
        text = self._task_text
        if text is None:
            return
        try:
            if not text.winfo_exists():
                return
            text.configure(state="normal")
            text.insert("end", str(message) + "\n", tag)
            text.see("end")
            text.configure(state="disabled")
        except tk.TclError:
            pass

    def _task_set_buttons(self, enabled: bool) -> None:
        for button in self._task_buttons:
            try:
                button.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass

    def _run_task(self, title: str, action) -> None:
        if self._task_running:
            self._set_status("已有下载/安装任务在进行", AMBER)
            return
        self._task_running = True
        self._task_set_buttons(False)
        self._task_log("")
        self._task_log(f"== {title} ==", "title")
        self._set_status(f"{title} 进行中 …", ACCENT)

        def worker():
            try:
                action(lambda message, tag="": self.events.put(("tasklog", message, tag)))
            except Exception as error:  # noqa: BLE001 - 界面只回消息
                self.events.put(("tasklog", f"失败：{error}", "err"))
                self.events.put(("task_done", title, False))
            else:
                self.events.put(("task_done", title, True))

        threading.Thread(target=worker, daemon=True).start()

    def _preflight_ports(self) -> None:
        common = {
            6099: "NapCat", 6185: "控制台", 6199: "AstrBot", 25565: "赞助页",
            3000: "网页服务", 23368: "视频主页", 23408: "控制台",
        }
        busy = [f"{port}（{name}）" for port, name in common.items() if port_alive(port)]
        if not busy:
            return
        messagebox.showinfo(
            "端口预检",
            "以下端口已有进程在监听，可能是上次部署残留，或被其它软件占用：\n  "
            + "、".join(busy)
            + "\n\n如果部署后服务起不来，先在主界面「全部停止」再重试；"
              "25565 如果是 Minecraft 等程序占用，需要先关掉它。",
        )

    def deploy(self, profile: str = "all") -> None:
        self._preflight_ports()
        core = profile == "core"
        title = "核心部署" if core else "全部署"
        if core:
            scope_text = (
                "核心部署 = 运行环境 + 核心四服务 + AstrBot + 基础插件 + NapCat + 桌宠。\n"
                "不安装也不启动：CosyVoice 语音、音频/视频/字幕、RAG、微信桥。\n"
            )
        else:
            scope_text = (
                "全部署 = 安装并启动全部能力：核心四服务、AstrBot、全部插件、NapCat、\n"
                "桌宠、微信桥、CosyVoice 语音（约 3GB）、音频/视频/字幕、RAG。\n"
            )
        question = (
            scope_text
            + "\n点「确定」后自动完成：\n"
            "  1. 检查并更新到最新代码（git 仓库快进；否则下载代码包）\n"
            "  2. 扫描本机已装组件，能复用就不下载（AstrBot/NapCat/CosyVoice 等）\n"
            "  3. 替换插件与桌宠补丁\n"
            "  4. 只下载并安装确实缺失的依赖\n"
            "  5. 重启受本次更新影响的服务\n\n"
            f"目标：{ROOT}"
        )
        if not messagebox.askokcancel(title, question):
            return
        self._run_task(title, lambda log: one_click_deploy(log, source=None, profile=profile))

    def uninstall(self) -> None:
        """停止所有服务并删除部署目录；默认保留用户数据（见下）。"""
        if (ROOT / ".git").is_dir():
            messagebox.showerror(
                "无法卸载",
                "当前目录是源码仓库（含 .git）。\n"
                "为避免误删代码，请直接在资源管理器里删除这个目录。",
            )
            return
        answer = messagebox.askyesnocancel(
            "卸载",
            "将停止所有服务并删除部署目录。\n\n"
            "「是」= 卸载，保留用户数据（config、.env、data、各服务 data、\n"
            "        AstrBot data、NapCat 登录配置）\n"
            "「否」= 卸载，连数据一起删除\n"
            "「取消」= 不卸载\n\n"
            "注意：只会结束本目录下的进程，不会取消 Ollama 等外部程序的开机自启。\n\n"
            f"目录：{ROOT}",
        )
        if answer is None:
            return
        if not UNINSTALL_PS1.is_file():
            messagebox.showerror("无法卸载", f"缺少脚本：{UNINSTALL_PS1}")
            return
        import subprocess

        args = [
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(UNINSTALL_PS1), "-Root", str(ROOT),
        ]
        if not answer:
            args.append("-PurgeData")
        inner = ",".join("'" + item.replace("'", "''") + "'" for item in args)
        command = (
            "Start-Process -Verb RunAs -FilePath 'powershell.exe' "
            f"-ArgumentList @({inner}) -WorkingDirectory $env:TEMP"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", command],
            creationflags=0x00000008,
        )
        messagebox.showinfo(
            "卸载已开始",
            "已请求管理员权限执行卸载，面板即将退出。\n"
            "如果在 UAC 弹窗里点了「否」，卸载不会执行，重新打开面板即可。",
        )
        self.root.destroy()

    def download_repo(self) -> None:
        source = self._task_source.get().strip() if self._task_source is not None else ""
        if source:
            question = (
                f"同步源：{source}\n目标：{ROOT}\n\n"
                "会用来源内容覆盖同名文件，但保留 data、venv、.env、日志与第三方组件。"
                "\n\n继续？"
            )
            task = lambda log: log(sync_repo(log, source=source), "ok")  # noqa: E731
        else:
            question = (
                "自动检查更新：\n"
                "· 当前目录是 git 仓库 → 只做快进拉取，本地改动不会丢\n"
                "· 否则下载 GitHub main 最新代码包，保留运行数据\n\n继续？"
            )
            task = update_repo  # noqa: E305
        if not messagebox.askyesno("下载 / 更新仓库内容", question):
            return
        self._run_task("下载 / 更新仓库", task)

    def check_env(self) -> None:
        self._run_task("检查环境", lambda log: run_setup(["-Check"], log))

    def install_deps(self, scope: str) -> None:
        if scope == "all":
            question = (
                "会安装全部依赖：AstrBot、插件、NapCat、微信桥、"
                "CosyVoice2（约 3GB）与桌宠环境。\n\n耗时较长，继续？"
            )
            if not messagebox.askyesno("安装全部依赖", question):
                return
            select = "1,2,3,4,5,6,7,8"
            title = "安装全部依赖"
        elif scope == "voice":
            question = (
                "安装本地语音 CosyVoice2：torch cu128 + 源码 + 模型，约 3GB，"
                "需要 NVIDIA 显卡（8G 显存够 0.5B）。\n\n继续？"
            )
            if not messagebox.askyesno("安装语音 CosyVoice2", question):
                return
            select = "7"
            title = "安装语音 CosyVoice2"
        else:
            select = "1,2"
            title = "安装核心依赖"
        self._run_task(title, lambda log: run_setup(["-Select", select, "-Yes"], log))

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as error:
            messagebox.showerror("打开失败", str(error))

    def _on_wheel(self, event) -> None:
        # 任务窗口的日志文本框自己滚动，别被全局滚轮抢走
        if isinstance(event.widget, (tk.Text, tk.Listbox, tk.Entry, tk.Spinbox)):
            return
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    # ---------- 事件循环

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "states":
                    self._apply_states(event[1])
                    self._refreshing = False
                elif kind == "done":
                    _, name, ok, message = event
                    self.busy.discard(name)
                    row = self.rows.get(name)
                    if row:
                        manifest = row["manifest"]
                        disabled = bool(manifest.get("merged_into")) and not manifest.get("runtime")
                        for btn in row["buttons"].values():
                            btn.configure(state="disabled" if disabled else "normal")
                        if not ok:
                            self.failed[name] = "操作失败"
                            row["state"].configure(text="操作失败", fg=RED)
                    if ok:
                        self.failed.pop(name, None)
                    self._set_status(message, GREEN if ok else RED)
                    self.refresh()
                elif kind == "reload":
                    self.reload_services()
                    if len(event) > 1 and event[1]:
                        self._set_status(event[1], GREEN)
                elif kind == "batch_done":
                    self.refresh()
                elif kind == "tasklog":
                    self._task_log(event[1], event[2])
                elif kind == "task_done":
                    _, title, ok = event
                    self._task_running = False
                    self._task_set_buttons(True)
                    self._task_log(f"{title}：{'完成' if ok else '失败'}", "ok" if ok else "err")
                    self._set_status(
                        f"{title}：{'完成' if ok else '失败，见任务窗口'}",
                        GREEN if ok else RED,
                    )
                    self.refresh()
        except queue.Empty:
            pass
        # 每 4 秒刷新一次状态
        now = time.time()
        if not hasattr(self, "_next_refresh"):
            self._next_refresh = now + 4
        if now >= self._next_refresh:
            self._next_refresh = now + 4
            self.refresh()
        self.root.after(120, self._poll)


def _cli(argv: list[str]) -> int:
    """无界面跑一键部署：--deploy [--core] [--local] [--source 路径/zip] [--no-install] [--no-start]"""
    def log(message: str, tag: str = "") -> None:
        print(message, flush=True)

    source = None
    if "--source" in argv:
        index = argv.index("--source")
        if index + 1 < len(argv):
            source = argv[index + 1]
    elif "--local" in argv:
        local = detect_local_repo()
        if local is None:
            log("找不到本机仓库，改用 GitHub", "err")
        else:
            source = str(local)
    scope = None
    if "--select" in argv:
        index = argv.index("--select")
        if index + 1 < len(argv):
            scope = argv[index + 1]
    log(f"部署目录：{ROOT}")
    profile = "core" if "--core" in argv else "all"
    try:
        log(
            one_click_deploy(
                log, source=source, scope=scope, profile=profile,
                install="--no-install" not in argv,
                start="--no-start" not in argv,
            ),
            "ok",
        )
    except Exception as error:  # noqa: BLE001 - 命令行的失败原因直接打印
        log(f"失败：{error}", "err")
        return 1
    return 0


def set_app_user_model_id() -> None:
    """让任务栏显示肥鱼图标，而不是 pythonw 的默认图标。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FlatFish.ServiceManager")
    except Exception:  # noqa: BLE001 - 老系统没有这个接口
        pass


def _focus_existing_manager() -> bool:
    """把已经开着的管理窗口拉到前台（Tk 主窗口标题固定，用 FindWindow 找）。"""
    try:
        import ctypes

        hwnd = ctypes.windll.user32.FindWindowW(None, "DeepSeek大肥鱼 服务管理器")
        if not hwnd:
            return False
        SW_RESTORE = 9
        if ctypes.windll.user32.IsIconic(hwnd):
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    argv = sys.argv[1:]
    if "--deploy" in argv:
        raise SystemExit(_cli(argv))
    # 单实例：命名互斥体已在别处持有时，把旧窗口唤到前台然后退出，
    # 防止双开两个管理器同时拉起/停止服务把状态搅乱。
    import ctypes

    ERROR_ALREADY_EXISTS = 183
    mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Local\\FlatFish.Manager.GUI")
    already = mutex and ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    if already:
        if mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)
        if not _focus_existing_manager():
            ctypes.windll.user32.MessageBoxW(
                None, "服务管理器已经在运行了（任务栏找「DeepSeek大肥鱼 服务管理器」）。", "提示", 0x40
            )
        return
    enable_dpi_awareness()
    set_app_user_model_id()
    root = tk.Tk()
    App(root)
    try:
        root.mainloop()
    finally:
        if mutex:
            ctypes.windll.kernel32.ReleaseMutex(mutex)
            ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    main()
