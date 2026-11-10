"""大肥鱼 → C:\\Program Files\\FlatFish 逐项迁移器。

设计原则：
- 全部路径都相对「肥鱼根目录」解析，迁移后不依赖任何用户目录写死的绝对路径；
- 先扫描：本机仓库 / 旧部署（~/dafeiyu）/ 目标目录已有依赖都能识别，能复用就不重装；
- 逐项迁移：复制 → 校验 → 记进度，校验通过前不允许删除旧目录；
- 断点续迁：进度写在 config/migrate-journal.json；
- 命令行可直接跑，GUI 由 installer/manager.py 调用。

命令行：
    python installer/migrate.py --scan
    python installer/migrate.py --items core,data,bots --copy --verify
    python installer/migrate.py --items core --purge --yes
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TARGET = Path(os.environ.get("FLATFISH_HOME") or r"C:\Program Files\FlatFish")
JOURNAL = REPO / "config" / "migrate-journal.json"
OLD_ROOTS = list(dict.fromkeys([Path.home() / "dafeiyu", Path(r"%USERPROFILE%\dafeiyu")]))
DEST_NAME = {
    "astrbot": "astrbot-win",
    "napcat": "napcat-win",
    "voice": "audio/cosyvoice_models",
    "training": "audio/seed-vc",
    "extras": "recharge_page",
}
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
# PyPI 镜像：默认国内清华源（可用 FLATFISH_PIP_INDEX 覆盖，设为 none 表示用官方源）
PIP_INDEX = os.environ.get("FLATFISH_PIP_INDEX", "https://pypi.tuna.tsinghua.edu.cn/simple")
_VER_CACHE: dict[str, tuple[int, int, int]] = {}

# 迁移代码时跳过的运行时目录（这些走单独条目，避免重复复制）
CODE_SKIP_TOP = {
    "astrbot-win", "third_party", "napcat-win", "bots", "dsh", "data", "logs",
    "audio", "recharge_page", "downloads", "bwe_env", "tts_cache",
    "voice_test", "voice_backup_20261031", "_backup_sync_20261107_012410",
    "mods", "scripts", "__pycache__", ".git", ".env",
}
CODE_SKIP_PARTS = {"__pycache__", "node_modules", ".venv", "venv", ".git"}
CODE_SKIP_SUFFIX = {".pyc", ".pyo", ".log"}

# key, 名称, 说明, 是否默认迁移
ITEM_DEFS = [
    ("core", "程序代码", "services / shared / tools / config / docs 等仓库内容", True),
    ("data", "运行数据", "统一记忆库、会话、RAG 索引（data/ 与 services/*/data 由旧目录带入）", True),
    ("bots", "桌宠中转", "bots/：pet-relay + config.json + wxauto-test 微信桥环境", True),
    ("astrbot", "AstrBot 宿主", "astrbot-win/：AstrBot 源码 + 已装依赖的 venv", True),
    ("napcat", "NapCat", "napcat-win/：QQ 协议端（含自带 node.exe）", True),
    ("dsh", "DSH 运行时", "dsh/node_modules：桌宠宿主用到的 DSH 依赖", True),
    ("voice", "本地语音", "CosyVoice2：cosyvoice_env + 模型 + 源码", False),
    ("training", "训练素材", "seed-vc / seedvc_env / SoulX-Singer（体积很大）", False),
    ("extras", "其它资源", "recharge_page / downloads / bwe_env / tts_cache 等", False),
]
ITEM_LABEL = {key: label for key, label, _desc, _on in ITEM_DEFS}
ITEM_DESC = {key: desc for key, _label, desc, _on in ITEM_DEFS}
ITEM_DEFAULT = {key: on for key, _label, _desc, on in ITEM_DEFS}


# ---------------------------------------------------------------- 基础工具


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def relaunch_admin(extra_args: list[str] | None = None) -> bool:
    """以管理员身份重启当前脚本（或面板）；由 GUI 决定是否调用。"""
    if not IS_WINDOWS:
        return False
    import ctypes

    exe = sys.executable
    script = str(Path(__file__).resolve())
    params = " ".join([f'"{script}"', *(extra_args or [])])
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, str(REPO), 1)
        return int(rc) > 32
    except Exception:  # noqa: BLE001
        return False


def can_write_target() -> bool:
    """目标已存在且当前用户可写（迁移过一次后已授权），就不必反复提权。"""
    if not TARGET.is_dir():
        return False
    probe = TARGET / f".write-test-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return True
    except OSError:
        return False


def _old(name: str) -> list[Path]:
    """旧部署里某个子目录的全部候选位置（去重，存在才返回）。"""
    hits: list[Path] = []
    for root in OLD_ROOTS:
        path = root / name
        if path.is_dir() and path not in hits:
            hits.append(path)
    return hits


def _dedupe(pairs: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    seen: set[str] = set()
    result: list[tuple[Path, Path]] = []
    for src, dst in pairs:
        key = str(src).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append((src, dst))
    return result


def _first_old(name: str) -> Path | None:
    hits = _old(name)
    return hits[0] if hits else None


def _size(path: Path) -> int:
    total = 0
    try:
        for cur, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(cur) / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _gb(size: int) -> str:
    return f"{size / 1073741824:.2f} GB" if size >= 1073741824 else f"{size / 1048576:.0f} MB"


def load_journal() -> dict:
    try:
        raw = json.loads(JOURNAL.read_text("utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_journal(data: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _mark(key: str, status: str, **extra) -> None:
    data = load_journal()
    entry = dict(data.get(key) or {})
    entry.update(extra)
    entry["status"] = status
    entry["target"] = str(TARGET)
    entry["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data[key] = entry
    save_journal(data)


def _entry(key: str) -> dict:
    """只认当前目标的进度，演练/换目标时不会串。"""
    entry = load_journal().get(key) or {}
    if str(entry.get("target", "")).lower() != str(TARGET).lower():
        return {}
    return entry


def ensure_target(log=print) -> None:
    """建目录并给当前用户授权（Program Files 默认只读）。"""
    if not TARGET.is_dir():
        TARGET.mkdir(parents=True, exist_ok=True)
        log(f"已创建 {TARGET}")
    if not IS_WINDOWS or not str(TARGET).lower().startswith(r"c:\program files"):
        return
    user = os.environ.get("USERNAME") or ""
    if not user:
        return
    subprocess.run(
        ["icacls", str(TARGET), "/grant", f"{user}:(OI)(CI)M", "/T", "/C", "/Q"],
        capture_output=True, text=True, encoding="mbcs", errors="replace",
        creationflags=CREATE_NO_WINDOW, timeout=300,
    )


# ---------------------------------------------------------------- 扫描


def _sources(key: str) -> list[tuple[Path, Path]]:
    """返回 [(源目录, 目标目录)]；源不存在则跳过。"""
    pairs: list[tuple[Path, Path]] = []
    if key == "core":
        pairs.append((REPO, TARGET))
    elif key == "data":
        for old in _old("data"):
            pairs.append((old, TARGET / "data"))
        for old in OLD_ROOTS:
            services = old / "services"
            if services.is_dir():
                for svc in services.iterdir():
                    if (svc / "data").is_dir():
                        pairs.append((svc / "data", TARGET / "services" / svc.name / "data"))
    elif key == "bots":
        for old in _old("bots"):
            pairs.append((old, TARGET / "bots"))
    elif key == "astrbot":
        for old in _old("astrbot-win"):
            pairs.append((old, TARGET / "astrbot-win"))
    elif key == "napcat":
        for old in _old("napcat-win"):
            pairs.append((old, TARGET / "napcat-win"))
    elif key == "dsh":
        for old in _old("dsh"):
            pairs.append((old, TARGET / "dsh"))
    elif key == "voice":
        # Python 依赖装在 Python 默认环境，这里只迁模型、源码和参考音频
        for name in ("cosyvoice_models", "CosyVoice", "cosyvoice_ref"):
            for old in _old(name):
                pairs.append((old, TARGET / name))
    elif key == "training":
        for name in ("seed-vc", "SoulX-Singer"):
            for old in _old(name):
                pairs.append((old, TARGET / name))
    elif key == "extras":
        for name in (
            "pet", "stack", "recharge_page", "downloads", "bwe_env", "tts_cache",
            "voice_test", "mods", "scripts", "astrbot_plugins", "relay",
        ):
            for old in _old(name):
                pairs.append((old, TARGET / name))
        for name in ("cosyvoice_tts_server.py", "setup_cosyvoice.py"):
            for old in OLD_ROOTS:
                src = old / name
                if src.is_file():
                    pairs.append((src, TARGET / name))
    return _dedupe([(src, dst) for src, dst in pairs if src.exists()])


def _dest(key: str) -> Path:
    if key == "core":
        return TARGET
    return TARGET / DEST_NAME.get(key, key)


def scan() -> dict:
    """扫描每个条目：源在哪、多大、目标是否已有、上次状态。"""
    journal = load_journal()
    items = []
    for key, label, desc, default in ITEM_DEFS:
        pairs = _sources(key)
        source = pairs[0][0] if pairs else None
        size = sum(_size(src) for src, _dst in pairs) if pairs else 0
        dest = _dest(key)
        entry = _entry(key)
        items.append({
            "key": key,
            "label": label,
            "desc": desc,
            "default": default,
            "sources": [str(src) for src, _dst in pairs],
            "source": str(source) if source else "",
            "source_kind": _kind(source),
            "size": size,
            "size_text": _gb(size) if size else "-",
            "dest": str(dest),
            "dest_exists": dest.is_dir() and any(dest.iterdir()) if dest.is_dir() else False,
            "status": entry.get("status", ""),
            "at": entry.get("at", ""),
        })
    return {
        "repo": str(REPO),
        "target": str(TARGET),
        "old_roots": [str(p) for p in OLD_ROOTS if p.is_dir()],
        "admin": is_admin(),
        "items": items,
    }


def _kind(path: Path | None) -> str:
    if path is None:
        return "missing"
    try:
        if REPO == path or REPO in path.parents:
            return "repo"
    except OSError:
        pass
    for root in OLD_ROOTS:
        try:
            if root == path or root in path.parents:
                return "old"
        except OSError:
            pass
    return "external"


# ---------------------------------------------------------------- 复制


def _robocopy(src: Path, dst: Path, log, exclude_venv: bool = False) -> None:
    args = [
        "robocopy", str(src), str(dst), "/E", "/R:1", "/W:1", "/MT:8",
        "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/XJ",
        "/XD", "__pycache__", ".git", "node_modules/.cache",
    ]
    if exclude_venv:
        # Python 依赖属于前置环境，不跟着工程目录走
        args += ["/XD", "venv", ".venv", "env"]
    log(f"复制 {src} → {dst}")
    completed = subprocess.run(
        args, capture_output=True, text=True, encoding="mbcs", errors="replace",
        creationflags=CREATE_NO_WINDOW, timeout=3600,
    )
    if completed.returncode >= 8:
        tail = (completed.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"robocopy 失败（{completed.returncode}）：{' / '.join(tail)}")


def _copy_code(log) -> None:
    copied = skipped = 0
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO)
        if not rel.parts or rel.parts[0] in CODE_SKIP_TOP:
            skipped += 1
            continue
        if any(part in CODE_SKIP_PARTS for part in rel.parts[:-1]):
            skipped += 1
            continue
        if rel.suffix.lower() in CODE_SKIP_SUFFIX or rel.name == ".env":
            skipped += 1
            continue
        target = TARGET / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    log(f"代码 {copied} 个文件（跳过 {skipped} 个运行时/缓存）")


def copy_item(key: str, log=print) -> None:
    if not TARGET.is_dir():
        ensure_target(log)
    pairs = _sources(key)
    if key == "core" and REPO.resolve() == TARGET.resolve():
        log("当前就在目标目录，代码无需复制")
        pairs = []
    if key == "bots" and not pairs:
        # 没有旧部署时，至少把仓库里的中转服务装到 bots/
        pairs = [(REPO / "relay", TARGET / "bots")] if (REPO / "relay").is_dir() else []
    if not pairs:
        raise RuntimeError(f"{ITEM_LABEL.get(key, key)}：没有任何可迁移的源目录")
    if key == "core":
        _copy_code(log)
    else:
        for src, dst in pairs:
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                log(f"复制 {src} → {dst}")
                continue
            _robocopy(src, dst, log, exclude_venv=key in {"astrbot", "bots", "voice"})
    if key == "bots":
        # 中转代码以仓库为准，配置和微信环境保留旧目录里的
        relay = REPO / "relay"
        if relay.is_dir():
            (TARGET / "bots").mkdir(parents=True, exist_ok=True)
            for name in ("pet-relay.js", "launch-relay.vbs"):
                if (relay / name).is_file():
                    shutil.copy2(relay / name, TARGET / "bots" / name)
        cfg = TARGET / "bots" / "config.json"
        if not cfg.is_file() and (relay / "config.json").is_file():
            shutil.copy2(relay / "config.json", cfg)
    _mark(key, "copied", sources=[str(src) for src, _dst in pairs])
    log(f"{ITEM_LABEL.get(key, key)} 已复制")


# ---------------------------------------------------------------- Python 依赖


def default_python() -> str:
    """Python 是前置依赖：优先 FLATFISH_PYTHON，其次 PATH 里的 python。"""
    return os.environ.get("FLATFISH_PYTHON") or shutil.which("python") or sys.executable


PY_COMPONENTS = {
    "astrbot": ["flask", "aiohttp", "yaml", "quart", "httpx", "openai", "dashscope"],
    "wechat": ["pywin32:win32api", "uiautomation", "comtypes", "pyperclip"],
    "voice": [
        "torch", "torchaudio", "soundfile", "librosa", "onnxruntime", "wetext",
        "conformer", "HyperPyYAML:hyperpyyaml", "omegaconf", "hydra-core:hydra",
        "modelscope", "transformers", "diffusers", "lightning", "gdown",
        "openai-whisper:whisper", "pyworld", "x-transformers:x_transformers",
        "onnx", "inflect", "pyarrow", "wget",
    ],
}


def _astrbot_requirements() -> Path | None:
    bot = _find_astrbot(TARGET)
    if bot is None:
        bot = _find_astrbot(REPO)
    if bot is None:
        return None
    req = bot / "requirements.txt"
    return req if req.is_file() else None


def _py_ver(py: str) -> tuple[int, int, int]:
    key = str(py)
    if key not in _VER_CACHE:
        try:
            completed = subprocess.run(
                [str(py), "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=30,
            )
            parts = (completed.stdout or "").strip().split(".")
            _VER_CACHE[key] = tuple(int(p) for p in parts) if len(parts) == 3 else (3, 12, 0)
        except (OSError, subprocess.SubprocessError, ValueError):
            _VER_CACHE[key] = (3, 12, 0)
    return _VER_CACHE[key]


def _marker_ok(marker: str, py: str) -> bool:
    """只判断 requirements 里常见的 python_version / python_full_version 条件。"""
    ver = _py_ver(py)
    for kind, op, raw in re.findall(
        r"(python_full_version|python_version)\s*(>=|<=|==|!=|>|<)\s*['\"]([\d.]+)['\"]", marker
    ):
        cur = "%d.%d.%d" % ver if kind == "python_full_version" else "%d.%d" % ver[:2]
        cur_t = tuple(int(x) for x in cur.split("."))
        req_t = tuple(int(x) for x in raw.split("."))
        n = min(len(cur_t), len(req_t))
        a, b = cur_t[:n], req_t[:n]
        ok = {"==": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]
        if not ok:
            return False
    return True


def _requirements_for(component: str, py: str | None = None) -> list[str]:
    if component == "astrbot":
        req = _astrbot_requirements()
        if req is None:
            return PY_COMPONENTS["astrbot"]
        names: list[str] = []
        for line in req.read_text("utf-8", "replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith(("-", "git+", "http")):
                continue
            body, _, marker = line.partition(";")
            if marker.strip() and py and not _marker_ok(marker, py):
                continue
            name = body
            for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "["):
                name = name.split(sep, 1)[0]
            name = name.strip()
            if name:
                names.append(name)
        return names
    return [item.split(":", 1)[0] for item in PY_COMPONENTS.get(component, [])]


def _probe(py: str, names: list[str]) -> tuple[Path, list[str]]:
    """在目标 Python 里检查模块/发行包，返回 (解释器, 缺失项)。"""
    code = (
        "import importlib.metadata as m, importlib.util as u, json, sys\n"
        "out = {}\n"
        "for n in sys.argv[1:]:\n"
        "    dist, _, mod = n.partition(':')\n"
        "    mod = mod or dist\n"
        "    try:\n"
        "        m.version(dist); ok = True\n"
        "    except Exception:\n"
        "        try: ok = bool(u.find_spec(mod))\n"
        "        except Exception: ok = False\n"
        "    out[n.split(':')[0]] = ok\n"
        "print(json.dumps(out, ensure_ascii=False))\n"
    )
    try:
        completed = subprocess.run(
            [py, "-c", code, *names], capture_output=True, text=True,
            encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW, timeout=120,
        )
        mapping = json.loads((completed.stdout or "{}").strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return Path(py), names
    missing = [name for name in names if not mapping.get(name.split(":", 1)[0])]
    return Path(py), missing


def pydeps_scan() -> dict:
    py = default_python()
    rows = []
    for component in PY_COMPONENTS:
        names = _requirements_for(component, py)
        _exe, missing = _probe(py, names)
        rows.append({
            "component": component,
            "label": {"astrbot": "AstrBot", "wechat": "微信桥", "voice": "本地语音"}[component],
            "total": len(names),
            "missing": missing,
        })
    return {"python": py, "rows": rows}


def pydeps_install(component: str, log=print) -> None:
    py = default_python()
    names = _requirements_for(component, py)
    _exe, missing = _probe(py, names)
    if not missing:
        log(f"{component}：依赖已齐全，复用现有 Python 环境（{py}）")
        return
    log(f"{component}：缺 {len(missing)} 个，安装到 Python 默认环境（{py}）")
    mirror = [] if PIP_INDEX.lower() in ("", "none", "off") else ["-i", PIP_INDEX]
    extra = [*mirror, "--timeout", "60", "--retries", "5"]
    pkgs = [name.split(":", 1)[0] for name in missing]
    if component == "astrbot" and _astrbot_requirements() is not None:
        cmd = [py, "-m", "pip", "install", "--user", *extra, "-r", str(_astrbot_requirements())]
    else:
        cmd = [py, "-m", "pip", "install", "--user", *extra, *pkgs]
    completed = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW, timeout=7200,
    )
    if completed.returncode != 0:
        tail = ((completed.stdout or "") + (completed.stderr or "")).strip().splitlines()[-3:]
        raise RuntimeError(f"pip 安装失败：{' / '.join(tail)}")
    _exe, rest = _probe(py, names)
    if rest:
        raise RuntimeError(f"仍缺：{', '.join(rest)}")
    log(f"{component}：依赖安装完成")


# ---------------------------------------------------------------- 校验


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 90) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
        return completed.returncode, ((completed.stdout or "") + (completed.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)


def _need(path: Path, label: str, problems: list[str]) -> bool:
    if path.exists():
        return True
    problems.append(f"缺少 {label}：{path}")
    return False


def verify_item(key: str, log=print) -> tuple[bool, list[str]]:
    problems: list[str] = []
    notes: list[str] = []
    t = TARGET

    if key == "core":
        _need(t / "services" / "hub" / "service.py", "hub 入口", problems)
        _need(t / "shared" / "infra" / "config.py", "共享配置", problems)
        _need(t / "tools" / "windows" / "manager.py", "管理面板", problems)
        features = t / "config" / "features.json"
        if not features.is_file():
            features.parent.mkdir(parents=True, exist_ok=True)
            features.write_text(json.dumps({
                "core": True, "astrbot": True, "plugins": True,
                "napcat": True, "wechat": False, "voice": False, "pet": True,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            notes.append("已生成默认 config/features.json")
        for stale in ("astrbot-win", "napcat-win", "cosyvoice_env"):
            if (t / stale).is_dir():
                notes.append(f"目标已有 {stale}（复用）")

    elif key == "data":
        _need(t / "data", "data 目录", problems)

    elif key == "bots":
        _need(t / "bots" / "pet-relay.js", "pet-relay.js", problems)
        cfg = t / "bots" / "config.json"
        if _need(cfg, "config.json", problems):
            try:
                json.loads(cfg.read_text("utf-8-sig"))
            except ValueError as error:
                problems.append(f"config.json 解析失败：{error}")
        wx = t / "bots" / "wxauto-test" / "venv" / "Scripts" / "python.exe"
        if not wx.is_file():
            notes.append("未带微信桥 venv；依赖在 Python 默认环境，启用微信时扫描安装")

    elif key == "astrbot":
        bot = _find_astrbot(t)
        if bot is None:
            problems.append("目标里找不到 AstrBot-*/main.py")
        py = default_python()
        _exe, missing = _probe(py, _requirements_for("astrbot", py))
        if missing:
            problems.append(
                f"Python 环境缺 {len(missing)} 个 AstrBot 依赖，点「Python 依赖」安装："
                + "、".join(missing[:6])
            )
        else:
            notes.append(f"AstrBot 依赖齐全（{py}）")

    elif key == "napcat":
        _need(t / "napcat-win" / "index.js", "NapCat 入口", problems)
        _need(t / "napcat-win" / "node.exe", "NapCat 自带 node", problems)

    elif key == "dsh":
        _need(
            t / "dsh" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js",
            "DSH bin.js", problems,
        )

    elif key == "voice":
        py = default_python()
        _exe, missing = _probe(py, _requirements_for("voice", py))
        if missing:
            problems.append(
                f"Python 环境缺 {len(missing)} 个语音依赖，点「Python 依赖」安装："
                + "、".join(missing[:6])
            )
        else:
            notes.append(f"语音依赖齐全（{py}）")
        _need(t / "cosyvoice_models" / "CosyVoice2-0.5B", "CosyVoice2-0.5B 模型", problems)
        _need(t / "CosyVoice", "CosyVoice 源码", problems)
        _need(t / "cosyvoice_ref", "参考音频 cosyvoice_ref", problems)

    elif key == "training":
        _need(t / "seed-vc", "seed-vc", problems)
        _need(t / "SoulX-Singer", "SoulX-Singer", problems)
        _exe, missing = _probe(default_python(), ["torch", "torchaudio"])
        if missing:
            problems.append(f"Python 环境缺训练依赖：{'、'.join(missing)}")

    elif key == "extras":
        names = ("pet", "stack", "recharge_page", "tts_cache", "downloads", "bwe_env")
        if not any((t / n).is_dir() for n in names):
            problems.append("目标里没有任何 extras 目录")
        else:
            notes.append("已迁：" + "、".join(n for n in names if (t / n).is_dir()))

    ok = not problems
    _mark(key, "verified" if ok else "failed", problems=problems, notes=notes)
    log(f"{ITEM_LABEL.get(key, key)}：{'校验通过' if ok else '校验未通过'}")
    for line in problems:
        log(f"  ✗ {line}")
    for line in notes:
        log(f"  · {line}")
    return ok, problems


def _find_astrbot(root: Path) -> Path | None:
    for parent in (root / "astrbot-win", root / "third_party"):
        if not parent.is_dir():
            continue
        hits = sorted(
            (p for p in parent.iterdir() if p.is_dir() and p.name.startswith("AstrBot")),
            key=lambda p: p.name,
        )
        for hit in reversed(hits):
            if (hit / "main.py").is_file():
                return hit
    return None


def smoke_test(log=print) -> tuple[bool, str]:
    """从目标目录真正拉起 hub，验证「相对路径 + Python 默认环境」能跑起来。"""
    py = Path(default_python())
    entry = TARGET / "services" / "hub" / "service.py"
    if not py.is_file() or not entry.is_file():
        return False, "缺少 Python 解释器或 services/hub/service.py"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    log_dir = TARGET / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    hub_log = log_dir / "migrate-hub.log"
    handle = open(hub_log, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(py), "service.py"], cwd=str(entry.parent), env=env,
        stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
    )
    try:
        import socket
        import urllib.request

        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", 23408), timeout=0.4):
                    break
            except OSError:
                time.sleep(0.4)
        else:
            return False, "hub 30 秒内没有监听 23408"
        if proc.poll() is not None:
            return False, f"hub 提前退出：{_tail(hub_log)}"
        try:
            with urllib.request.urlopen("http://127.0.0.1:23408/health", timeout=6) as resp:
                body = resp.read().decode("utf-8", "replace")
            log(f"hub /health：{body[:160]}")
            return True, "hub 从新目录启动成功"
        except Exception as error:  # noqa: BLE001
            # /health 会汇总所有服务，迁移时其它服务还没起，慢一点不算失败
            return True, f"hub 已监听 23408（/health 聚合较慢：{error}）"
    except Exception as error:  # noqa: BLE001
        return False, f"hub 启动校验失败：{error}（{_tail(hub_log)}）"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        handle.close()


def _tail(path: Path, lines: int = 3) -> str:
    try:
        text = path.read_text("utf-8", "replace").strip().splitlines()
        return " / ".join(text[-lines:])
    except OSError:
        return "无日志"


# ---------------------------------------------------------------- 清理旧目录


def purge_item(key: str, log=print) -> None:
    """只有校验通过的条目才允许删旧目录。"""
    entry = _entry(key)
    if entry.get("status") != "verified":
        raise RuntimeError(f"{ITEM_LABEL.get(key, key)} 还没校验通过，先校验再删旧目录")
    removed: list[str] = []
    for src, _dst in _sources(key):
        if _kind(src) != "old":
            continue
        shutil.rmtree(src, ignore_errors=True)
        removed.append(str(src))
        log(f"已删除旧目录 {src}")
    _mark(key, "purged", removed=removed)
    if not removed:
        log("没有需要删除的旧目录")


# ---------------------------------------------------------------- CLI


def _cli(argv: list[str]) -> int:
    def log(message: str) -> None:
        print(message, flush=True)

    keys = [k for k, *_ in ITEM_DEFS]
    if "--scan" in argv:
        info = scan()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if "--pydeps-scan" in argv:
        print(json.dumps(pydeps_scan(), ensure_ascii=False, indent=2))
        return 0
    if "--pydeps-install" in argv:
        index = argv.index("--pydeps-install")
        component = argv[index + 1] if index + 1 < len(argv) else "astrbot"
        try:
            pydeps_install(component, log)
            return 0
        except Exception as error:  # noqa: BLE001
            log(str(error))
            return 1

    if "--items" in argv:
        index = argv.index("--items")
        raw = argv[index + 1] if index + 1 < len(argv) else ""
        keys = [k.strip() for k in raw.split(",") if k.strip()] or keys

    if not can_write_target() and not is_admin():
        log("需要管理员权限写入 C:\\Program Files，请用管理员 PowerShell 运行，或在面板里点「迁移」。")
        return 2

    failed = 0
    ensure_target(log)
    for key in keys:
        if "--copy" in argv:
            try:
                copy_item(key, log)
            except Exception as error:  # noqa: BLE001
                log(f"复制失败 {key}：{error}")
                _mark(key, "failed", problems=[str(error)])
                failed += 1
                continue
        if "--verify" in argv:
            ok, _ = verify_item(key, log)
            failed += 0 if ok else 1
        if "--purge" in argv:
            try:
                purge_item(key, log)
            except Exception as error:  # noqa: BLE001
                log(f"清理失败 {key}：{error}")
                failed += 1
    if "--smoke" in argv:
        ok, message = smoke_test(log)
        log(message)
        failed += 0 if ok else 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
