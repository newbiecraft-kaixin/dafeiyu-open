"""train_api：音色训练（seed-vc 微调）的接口与后台任务。

能力域：audio（可选扩展）——装了可以训练音色/管歌曲，不装不影响桌宠对话。

接口
  POST /train/jobs                提交训练任务（立即返回 job id，后台线程跑）
  GET  /train/jobs                任务列表
  GET  /train/jobs/<id>           单任务详情
  GET  /train/jobs/<id>/log       日志尾部（?tail=200）
  POST /train/jobs/<id>/cancel    终止训练子进程
  GET  /train/refs                扫描已有参考音频，供页面下拉选择

seed-vc 的数据集约定（已读 seed-vc-main/data/ft_dataset.py 确认）：
  FT_Dataset 只 os.walk 目录里任意位置的 .wav/.mp3/.flac/.ogg/.m4a/.opus，
  不需要 CosyVoice 那套 wav.scp/parquet；所以单个人声文件直接复制到
  data/datasets/<音色名>/<run_name>/ 即可开训。

训练产物：<seed-vc 目录>/runs/<run_name>/ft_model.pth
（train.py 的 config 里 log_dir 是相对 cwd 的 "./runs/"）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import fail, ok  # noqa: E402

# ---- 路径 ----
DATA_DIR = _HERE.parent / "data"
JOBS_FILE = DATA_DIR / "jobs.json"
LOG_DIR = DATA_DIR / "logs"
DATASET_ROOT = DATA_DIR / "datasets"

# 两份 seed-vc 的 train.py 内容一致；优先选带本地 HF 缓存的副本，训练时不用联网下载
SEEDVC_XIAOYI = Path(r"C:\path\to\workspace\xiaoyi_cloud\seed-vc-main")
SEEDVC_WORKING = Path(r"C:\path\to\dafeiyu\seed-vc\seed-vc-main")
CONFIG_NAME = "config_dit_mel_seed_uvit_xlsr_tiny.yml"

# 页面下拉用的参考音频目录（不存在就跳过）
REF_DIRS = [
    Path(r"C:\path\to\dafeiyu\cosyvoice_ref"),
    Path(r"C:\path\to\workspace\xiaoyi_cloud\ft_data"),
    Path(r"C:\path\to\workspace\sing_ab\songs"),
]

AUDIO_EXT = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}
MAX_REFS = 800

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_DATA_LOCK = threading.RLock()
_JOBS: Optional[List[Dict[str, Any]]] = None
_PROCS: Dict[str, subprocess.Popen] = {}

_PY_LOCK = threading.Lock()
_PY_CACHE: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------- 小工具


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(name: str) -> str:
    """把音色名转成可安全做目录名的片段（Windows 非法字符全部替换）。"""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip().strip(".")
    text = re.sub(r"\s+", "_", text)
    return (text or "voice")[:60]


def _resolve_path(text: str) -> Path:
    raw = str(text).strip().strip('"').strip("'")
    return Path(os.path.expanduser(raw)).resolve()


def _list_audio(folder: Path, limit: int = 5000) -> List[Path]:
    found: List[Path] = []
    try:
        for p in folder.rglob("*"):
            if len(found) >= limit:
                break
            if p.is_file() and p.suffix.lower() in AUDIO_EXT:
                found.append(p)
    except OSError:
        pass
    return found


def _wav_duration(path: Path) -> Optional[float]:
    """只对 wav 读时长（标准库即可）；其它格式返回 None 不做拦截。"""
    if path.suffix.lower() != ".wav":
        return None
    try:
        import wave

        with wave.open(str(path), "rb") as f:
            rate = f.getframerate() or 1
            return f.getnframes() / float(rate)
    except Exception:  # noqa: BLE001 - 读不出来就别拦，交给训练自己报错
        return None


def _as_int(value: Any, label: str, default: int, lo: int, hi: int) -> int:
    if value is None or value == "":
        value = default
    try:
        num = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是整数") from exc
    if not lo <= num <= hi:
        raise ValueError(f"{label} 需要在 {lo}~{hi} 之间")
    return num


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on", "是")


# ---------------------------------------------------------------- 环境探测


def _python_candidates() -> List[Path]:
    out: List[Path] = []
    env = os.environ.get("SEEDVC_PYTHON", "").strip()
    if env:
        out.append(Path(os.path.expanduser(env.strip('"'))))
    out += [
        Path(r"C:\path\to\dafeiyu\cosyvoice_env\Scripts\python.exe"),
        SEEDVC_WORKING / "venv" / "Scripts" / "python.exe",
        SEEDVC_WORKING / ".venv" / "Scripts" / "python.exe",
        SEEDVC_XIAOYI / "venv" / "Scripts" / "python.exe",
        SEEDVC_XIAOYI / ".venv" / "Scripts" / "python.exe",
        Path(r"C:\path\to\dafeiyu\astrbot-win\venv\Scripts\python.exe"),
    ]
    which = shutil.which("python")
    if which:
        out.append(Path(which))
    return out


def _python_info() -> Dict[str, Any]:
    """找出第一个真正能 import torch/torchaudio/librosa/yaml 的解释器，只探测一次。"""
    global _PY_CACHE
    with _PY_LOCK:
        if _PY_CACHE is not None:
            return _PY_CACHE
        notes: List[str] = []
        for cand in _python_candidates():
            if not cand.exists():
                continue
            try:
                proc = subprocess.run(
                    [str(cand), "-c", "import torch, torchaudio, librosa, yaml"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    creationflags=_NO_WINDOW,
                )
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{cand} 探测失败：{exc}")
                continue
            if proc.returncode == 0:
                _PY_CACHE = {"path": str(cand), "detail": "依赖可用"}
                return _PY_CACHE
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            notes.append(f"{cand} 缺少依赖：{tail[-1] if tail else '未知错误'}")
        _PY_CACHE = {
            "path": "",
            "detail": "；".join(notes[-3:]) or "没找到可用的 Python 解释器",
        }
        return _PY_CACHE


def _seedvc_dir() -> Path:
    env = os.environ.get("SEEDVC_DIR", "").strip()
    if env:
        return Path(os.path.expanduser(env.strip('"'))).resolve()
    best: Optional[Path] = None
    for cand in (SEEDVC_XIAOYI, SEEDVC_WORKING):
        if not (cand / "train.py").is_file():
            continue
        if best is None:
            best = cand
        # 本地已有 Seed-VC 的 HF 缓存时优先用它，避免每次训练重新下载权重
        if (cand / "checkpoints" / "models--Plachta--Seed-VC").exists():
            return cand
    return best or SEEDVC_XIAOYI


def _config_path() -> Path:
    env = os.environ.get("SEEDVC_CONFIG", "").strip()
    if env:
        return Path(os.path.expanduser(env.strip('"'))).resolve()
    return _seedvc_dir() / "configs" / "presets" / CONFIG_NAME


def _backend() -> Dict[str, Any]:
    py = _python_info()
    seedvc = _seedvc_dir()
    config = _config_path()
    problems: List[str] = []
    if not py["path"]:
        problems.append(
            "未找到能跑 seed-vc 的 Python（需 torch/torchaudio/librosa/yaml），"
            "可用环境变量 SEEDVC_PYTHON 指定解释器路径"
        )
    if not (seedvc / "train.py").is_file():
        problems.append(f"缺少 train.py：{seedvc}")
    if not config.is_file():
        problems.append(f"缺少训练配置：{config}")
    return {
        "ready": not problems,
        "problems": problems,
        "python": py["path"],
        "python_detail": py["detail"],
        "dir": str(seedvc),
        "config": str(config),
    }


# ---------------------------------------------------------------- 任务持久化


def _read_jobs_file() -> List[Dict[str, Any]]:
    if not JOBS_FILE.exists():
        return []
    try:
        blob = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(blob, dict):
        blob = blob.get("jobs") or []
    if not isinstance(blob, list):
        return []
    return [j for j in blob if isinstance(j, dict) and j.get("id")]


def _save_locked() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"jobs": _JOBS or []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(JOBS_FILE))


def _load_jobs() -> List[Dict[str, Any]]:
    """首次读盘；上次服务退出时还在跑的任务标记为中断（进程已不在了）。"""
    global _JOBS
    with _DATA_LOCK:
        if _JOBS is not None:
            return _JOBS
        _JOBS = _read_jobs_file()
        changed = False
        for job in _JOBS:
            if job.get("status") in ("queued", "running"):
                job["status"] = "failed"
                job["error"] = "服务重启，训练进程已丢失，请重新提交任务"
                job["finished"] = _ts()
                changed = True
        if changed:
            _save_locked()
        return _JOBS


def _raw_find(job_id: str) -> Optional[Dict[str, Any]]:
    for job in _load_jobs():
        if job.get("id") == job_id:
            return job
    return None


def _find(job_id: str) -> Optional[Dict[str, Any]]:
    with _DATA_LOCK:
        job = _raw_find(job_id)
        return dict(job) if job else None


def _patch(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    with _DATA_LOCK:
        job = _raw_find(job_id)
        if job is None:
            return None
        job.update(fields)
        _save_locked()
        return dict(job)


def _tail_text(path: Path, lines: int) -> str:
    """最多读末尾 2MB，够页面展示，也不会把大日志整个读进内存。"""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    try:
        with path.open("rb") as f:
            if size > 2_000_000:
                f.seek(size - 2_000_000, os.SEEK_SET)
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    rows = text.splitlines()[-lines:]
    return "\n".join(rows)


def _parse_step(text: str) -> int:
    """train.py 会打印 "epoch N, step N, loss: x"，检查点文件名里也有 step_NNNNN。"""
    best = 0
    for line in text.splitlines():
        for match in re.finditer(r"step[ _:=]*(\d+)", line, re.IGNORECASE):
            best = max(best, int(match.group(1)))
    return best


def _public(job: Dict[str, Any]) -> Dict[str, Any]:
    steps = int(job.get("steps") or 0)
    status = str(job.get("status") or "")
    progress = 0
    if status == "completed":
        progress = 100
    elif steps > 0:
        log_path = Path(job.get("log_file") or "")
        parsed = _parse_step(_tail_text(log_path, 120)) if log_path.is_file() else 0
        progress = min(99, int(parsed * 100 / steps))
        if status in ("failed", "cancelled"):
            progress = min(100, int(parsed * 100 / steps))
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "run_name": job.get("run_name"),
        "status": status,
        "created": job.get("created"),
        "started": job.get("started"),
        "finished": job.get("finished"),
        "steps": steps,
        "batch_size": job.get("batch_size"),
        "bf16": bool(job.get("bf16")),
        "gpu": job.get("gpu"),
        "dataset_dir": job.get("dataset_dir"),
        "artifact": job.get("artifact") or "",
        "error": job.get("error") or "",
        "log_file": job.get("log_file"),
        "progress": progress,
        "cmd": job.get("cmd") or "",
    }


def _snapshot() -> List[Dict[str, Any]]:
    with _DATA_LOCK:
        return [_public(dict(job)) for job in _load_jobs()]


def status() -> Dict[str, Any]:
    """给 /health 用的摘要。"""
    try:
        jobs = _snapshot()
    except Exception as exc:  # noqa: BLE001 - health 不允许因为读盘失败而整个挂掉
        return {"ready": False, "jobs": 0, "error": str(exc)}
    counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
    for job in jobs:
        if job["status"] in counts:
            counts[job["status"]] += 1
    backend = _backend()
    return {
        "ready": backend["ready"],
        "jobs": len(jobs),
        "running": counts["running"],
        "queued": counts["queued"],
        "completed": counts["completed"],
        "failed": counts["failed"],
        "cancelled": counts["cancelled"],
        "python": backend["python"],
        "python_detail": backend["python_detail"],
        "seedvc_dir": backend["dir"],
        "problems": backend["problems"],
        "note": "音频能力域为可选扩展：未就绪不影响桌宠对话",
    }


# ---------------------------------------------------------------- 训练执行


def _build_cmd(job: Dict[str, Any], seedvc: Path) -> List[str]:
    cmd = [
        _python_info()["path"],
        str(seedvc / "train.py"),
        "--config", str(_config_path()),
        "--dataset-dir", str(job["dataset_dir"]),
        "--run-name", str(job["run_name"]),
        "--batch-size", str(job["batch_size"]),
        "--max-steps", str(job["steps"]),
        "--save-every", str(min(500, max(1, int(job["steps"])))),
        "--num-workers", "0",
        "--gpu", str(job["gpu"]),
    ]
    if job.get("bf16"):
        cmd.append("--bf16")
    pretrained = os.environ.get("SEEDVC_PRETRAINED_CKPT", "").strip()
    if pretrained and Path(pretrained).is_file():
        cmd += ["--pretrained-ckpt", pretrained]
    return cmd


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=6)
        return
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                creationflags=_NO_WINDOW,
            )
        except Exception:  # noqa: BLE001
            pass


def _worker(job_id: str) -> None:
    """后台线程：起子进程、把输出写进日志、结束后回写状态与产物。"""
    job = _find(job_id)
    if not job or job.get("cancel_requested"):
        _patch(job_id, status="cancelled", finished=_ts())
        return

    seedvc = _seedvc_dir()
    cmd = _build_cmd(job, seedvc)
    # 排队期间就被取消的话，这里直接收工，别把进程再拉起来
    if (_find(job_id) or {}).get("cancel_requested"):
        _patch(job_id, status="cancelled", finished=_ts())
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = Path(job["log_file"])
    _patch(job_id, status="running", started=_ts(), cmd=subprocess.list2cmdline(cmd))

    code = -1
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as logf:
            logf.write(f"[train {_ts()}] 工作目录 {seedvc}\n")
            logf.write(f"$ {subprocess.list2cmdline(cmd)}\n\n")
            logf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(seedvc),
                stdout=logf,
                stderr=subprocess.STDOUT,
                creationflags=_NO_WINDOW,
            )
            _PROCS[job_id] = proc
            _patch(job_id, pid=proc.pid)
            code = proc.wait()
    except Exception as exc:  # noqa: BLE001
        _patch(job_id, status="failed", finished=_ts(), error=f"启动训练进程失败：{exc}")
        return
    finally:
        _PROCS.pop(job_id, None)

    job = _find(job_id) or {}
    if job.get("cancel_requested"):
        _patch(job_id, status="cancelled", finished=_ts())
        return
    if code != 0:
        _patch(
            job_id,
            status="failed",
            finished=_ts(),
            error=f"训练进程退出码 {code}，请展开日志查看原因",
        )
        return

    artifact = seedvc / "runs" / str(job.get("run_name")) / "ft_model.pth"
    if artifact.is_file():
        _patch(job_id, status="completed", finished=_ts(), artifact=str(artifact))
    else:
        _patch(
            job_id,
            status="failed",
            finished=_ts(),
            error=f"训练结束但没找到 ft_model.pth（预期位置 {artifact}）",
        )


def _make_run_name(name: str) -> str:
    safe = _safe_name(name)
    base = f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}"
    used = {str(j.get("run_name") or "") for j in _load_jobs()}
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


# ---------------------------------------------------------------- 接口


def handle_jobs_create(req) -> Any:  # noqa: ANN001
    if not isinstance(req.payload, dict):
        return fail("请求体必须是 JSON 对象，例如 {name, ref_path, steps}", 400)
    body = req.payload

    name = str(body.get("name") or "").strip()
    if not name:
        return fail("音色名不能为空", 400)

    raw_path = str(body.get("ref_path") or body.get("dataset_dir") or "").strip()
    if not raw_path:
        return fail("请填写人声路径或数据集目录", 400)
    src = _resolve_path(raw_path)
    if not src.exists():
        return fail(f"路径不存在：{src}", 400)
    if not src.is_dir() and src.suffix.lower() not in AUDIO_EXT:
        return fail(
            "只支持音频文件：" + "、".join(sorted(AUDIO_EXT)),
            400,
        )

    try:
        steps = _as_int(body.get("steps"), "训练步数", 1000, 1, 500000)
        batch_size = _as_int(body.get("batch_size"), "batch", 2, 1, 32)
        gpu = _as_int(body.get("gpu"), "GPU 编号", 0, 0, 15)
    except ValueError as exc:
        return fail(str(exc), 400)
    bf16 = _as_bool(body.get("bf16"))

    backend = _backend()
    if not backend["ready"]:
        return fail("训练后端未就绪：" + "；".join(backend["problems"]), 503)

    run_name = _make_run_name(name)
    if src.is_dir():
        if not _list_audio(src, limit=1):
            return fail(f"目录里没有可训练音频（支持 {'、'.join(sorted(AUDIO_EXT))}）：{src}", 400)
        dataset_dir = src
    else:
        duration = _wav_duration(src)
        if duration is not None and not 1.0 <= duration <= 30.0:
            return fail(
                f"音频时长 {duration:.1f} 秒；seed-vc 单条音频只接受 1~30 秒，"
                "请先切分成片段，或改传一个数据集目录",
                400,
            )
        dataset_dir = DATASET_ROOT / _safe_name(name) / run_name
        dataset_dir.mkdir(parents=True, exist_ok=True)
        target = dataset_dir / src.name
        try:
            if src.resolve() != target.resolve():
                shutil.copy2(str(src), str(target))
        except OSError as exc:
            return fail(f"复制音频到数据集失败：{exc}", 500)

    job_id = "job_" + uuid4_hex()
    job: Dict[str, Any] = {
        "id": job_id,
        "name": name,
        "run_name": run_name,
        "status": "queued",
        "created": _ts(),
        "started": None,
        "finished": None,
        "steps": steps,
        "batch_size": batch_size,
        "bf16": bf16,
        "gpu": gpu,
        "dataset_dir": str(dataset_dir),
        "log_file": str(LOG_DIR / f"{job_id}.log"),
        "artifact": "",
        "error": "",
        "cmd": "",
        "cancel_requested": False,
    }
    with _DATA_LOCK:
        _load_jobs().insert(0, job)
        _save_locked()

    threading.Thread(target=_worker, args=(job_id,), name=f"train-{job_id}", daemon=True).start()
    return ok(_public(job))


def uuid4_hex() -> str:
    return uuid.uuid4().hex[:10]


def handle_jobs_list(req) -> Any:  # noqa: ANN001
    jobs = _snapshot()
    jobs.sort(key=lambda j: str(j.get("created") or ""), reverse=True)
    return ok({"jobs": jobs, "count": len(jobs)})


def _job_parts(req) -> List[str]:  # noqa: ANN001
    return [p for p in str(req.path).strip("/").split("/") if p]


def handle_jobs_get(req) -> Any:  # noqa: ANN001
    parts = _job_parts(req)
    if len(parts) < 3:
        return fail("未知接口", 404)
    job_id = parts[2]
    job = _find(job_id)
    if not job:
        return fail(f"任务不存在：{job_id}", 404)
    if len(parts) == 3:
        return ok(_public(job))
    if len(parts) == 4 and parts[3] == "log":
        try:
            tail = _as_int(req.q("tail"), "tail", 200, 1, 5000)
        except ValueError:
            tail = 200
        log_path = Path(job.get("log_file") or "")
        text = _tail_text(log_path, tail) if log_path.is_file() else ""
        return ok({
            "id": job_id,
            "log_file": str(log_path),
            "lines": text.splitlines(),
            "tail": tail,
        })
    return fail(f"未知接口 {req.path}", 404)


def handle_jobs_cancel(req) -> Any:  # noqa: ANN001
    parts = _job_parts(req)
    if len(parts) != 4 or parts[3] != "cancel":
        return fail(f"未知接口 {req.path}", 404)
    job_id = parts[2]
    job = _find(job_id)
    if not job:
        return fail(f"任务不存在：{job_id}", 404)
    if job.get("status") not in ("queued", "running"):
        return fail(f"任务已结束（{job.get('status')}），无需取消", 409)

    _patch(job_id, cancel_requested=True)
    proc = _PROCS.get(job_id)
    if proc is not None and proc.poll() is None:
        _kill(proc)
    _patch(job_id, status="cancelled", finished=_ts())
    return ok(_public(_find(job_id) or job))


def handle_refs(req) -> Any:  # noqa: ANN001
    refs: List[Dict[str, Any]] = []
    dirs: List[str] = []
    for folder in REF_DIRS:
        if not folder.is_dir():
            continue
        dirs.append(str(folder))
        try:
            for path in sorted(folder.rglob("*")):
                if len(refs) >= MAX_REFS:
                    break
                if not path.is_file() or path.suffix.lower() not in AUDIO_EXT:
                    continue
                stat = path.stat()
                refs.append({
                    "path": str(path),
                    "name": path.name,
                    "dir": str(folder),
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                })
        except OSError:
            continue
    refs.sort(key=lambda r: r["mtime"], reverse=True)
    return ok({"dirs": dirs, "refs": refs, "count": len(refs)})


routes: Dict[str, Any] = {
    "POST /train/jobs": handle_jobs_create,
    "GET /train/jobs": handle_jobs_list,
    "GET /train/jobs/*": handle_jobs_get,
    "POST /train/jobs/*": handle_jobs_cancel,
    "GET /train/refs": handle_refs,
}
