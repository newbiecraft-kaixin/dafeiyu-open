"""train_api：声线档案（推理参数）的接口，不再跑 seed-vc 训练。

能力域：audio（可选扩展）——装了可以管理声线/管歌曲，不装不影响桌宠对话。

声线档案 = cosyvoice_ref/profiles/<声线名>/ 下的 ref.wav + prompt.txt。
TTS（CosyVoice / SoulX）推理只读这两个文件；ft_model 只被 Seed-VC 回退
路径使用，对默认说话音色没有作用，所以这里不再「提交训练」，而是把
参考音频和提示文本直接落盘，提交后声线立刻可用。

接口
  POST /train/voices   新增声线（name + ref_path + prompt，最小校验后落盘）
  GET  /train/voices   已有声线列表
  GET  /train/refs     扫描已有参考音频，供页面下拉选择
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import fail, ok  # noqa: E402

# ---- 路径 ----
_REPO = _HERE.parents[2]
VOICE_ROOT = _REPO / "cosyvoice_ref" / "profiles"

# 页面下拉用的参考音频目录（不存在就跳过）
REF_DIRS = [
    _REPO / "cosyvoice_ref",
    Path.home() / "dafeiyu" / "cosyvoice_ref",
    Path.home() / "workspace" / "xiaoyi_cloud" / "ft_data",
    Path.home() / "workspace" / "sing_ab" / "songs",
]

# 声线档案统一落成 ref.wav，推理侧只认 wav
AUDIO_EXT = {".wav"}
MAX_REFS = 800
MIN_SECONDS = 1.0
MAX_SECONDS = 30.0

# 和 hub 的 botctl._voice_dir 保持一致，避免两个服务对同一档案名判定不同
_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff-]")


# ---------------------------------------------------------------- 小工具


def _safe_name(name: str) -> str:
    """把音色名转成可安全做目录名的片段（Windows 非法字符全部替换）。"""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip().strip(".")
    text = re.sub(r"\s+", "_", text)
    return (text or "voice")[:60]


def _voice_dir(name: str) -> Path:
    name = str(name or "").strip()
    if not name or _SAFE_NAME.sub("", name) != name or name.startswith("."):
        raise ValueError("声线名称非法（只能包含中英文、数字、下划线和短横线）")
    return VOICE_ROOT / name


def _resolve_path(text: str) -> Path:
    raw = str(text).strip().strip('"').strip("'")
    return Path(os.path.expanduser(raw)).resolve()


def _wav_duration(path: Path) -> Optional[float]:
    """读标准 PCM wav 的时长；读不出来返回 None。"""
    try:
        with wave.open(str(path), "rb") as f:
            rate = f.getframerate() or 1
            return f.getnframes() / float(rate)
    except Exception:  # noqa: BLE001 - 交给调用方给出可读提示
        return None


def _voice_prompt(folder: Path) -> str:
    """读取参考音对应的转写文本；instruct 备注不算转写。"""
    path = folder / "prompt.txt"
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    if (not text or text.startswith(("（", "("))
            or "无需参考文本" in text or "instruct" in text.lower()):
        return ""
    return text


def _voice_rows() -> Dict[str, Any]:
    active = ""
    marker = VOICE_ROOT / ".active"
    if marker.exists():
        try:
            active = marker.read_text(encoding="utf-8-sig").strip()
        except OSError:
            active = ""
    rows: List[Dict[str, Any]] = []
    if VOICE_ROOT.is_dir():
        for folder in sorted((p for p in VOICE_ROOT.iterdir() if p.is_dir()), key=lambda p: p.name):
            has_ref = (folder / "ref.wav").is_file()
            prompt = _voice_prompt(folder)
            if not has_ref:
                mode, mode_label = "broken", "缺参考音"
            elif prompt:
                mode, mode_label = "zero_shot", "零样本克隆"
            else:
                mode, mode_label = "cross_lingual", "跨语种克隆"
            rows.append({
                "name": folder.name,
                "has_ref": has_ref,
                "prompt": prompt,
                "mode": mode,
                "mode_label": mode_label,
                "active": folder.name == active,
            })
    return {"active": active, "voices": rows}


# ---------------------------------------------------------------- 接口


def handle_voice_create(req) -> Any:  # noqa: ANN001
    if not isinstance(req.payload, dict):
        return fail("请求体必须是 JSON 对象，例如 {name, ref_path, prompt}", 400)
    body = req.payload

    name = str(body.get("name") or "").strip()
    if not name:
        return fail("声线名称不能为空", 400)
    try:
        folder = _voice_dir(name)
    except ValueError as exc:
        return fail(str(exc), 400)
    if folder.exists():
        return fail(f"声线已存在：{folder.name}", 409)

    raw_path = str(body.get("ref_path") or body.get("audio") or "").strip()
    if not raw_path:
        return fail("请填写参考音频路径", 400)
    src = _resolve_path(raw_path)
    if not src.is_file():
        return fail(f"参考音频不存在：{src}", 400)
    if src.suffix.lower() not in AUDIO_EXT:
        return fail("参考音频必须是 .wav", 400)

    prompt = str(body.get("prompt") or body.get("text") or "").strip()
    if not prompt:
        return fail("提示文本不能为空（填写参考音频里说的那句话）", 400)

    duration = _wav_duration(src)
    if duration is None:
        return fail("无法读取参考音频时长，请确认是标准 PCM wav 文件", 400)
    if not MIN_SECONDS <= duration <= MAX_SECONDS:
        return fail(
            f"参考音频时长 {duration:.1f} 秒；请提供 {MIN_SECONDS:.0f}~{MAX_SECONDS:.0f} 秒的干净人声",
            400,
        )

    VOICE_ROOT.mkdir(parents=True, exist_ok=True)
    folder.mkdir()
    try:
        shutil.copy2(src, folder / "ref.wav")
        (folder / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
        (folder / "meta.txt").write_text("自定义\n", encoding="utf-8")
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return fail(f"写入声线档案失败：{exc}", 500)

    return ok({
        "name": folder.name,
        "folder": str(folder),
        "ref": str(folder / "ref.wav"),
        "prompt": prompt,
        "duration_s": round(duration, 2),
        "mode": "zero_shot",
        "mode_label": "零样本克隆",
    })


def handle_voices(req) -> Any:  # noqa: ANN001
    return ok(_voice_rows())


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


def status() -> Dict[str, Any]:
    """给 /health 用的摘要。"""
    problems: List[str] = []
    try:
        VOICE_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        problems.append(f"声线档案目录不可写：{VOICE_ROOT}（{exc}）")
    rows = _voice_rows()
    return {
        "ready": not problems,
        "profile_dir": str(VOICE_ROOT),
        "voices": len(rows["voices"]),
        "active": rows["active"],
        "problems": problems,
        "note": "音频能力域为可选扩展：未就绪不影响桌宠对话",
    }


routes: Dict[str, Any] = {
    "POST /train/voices": handle_voice_create,
    "GET /train/voices": handle_voices,
    "GET /train/refs": handle_refs,
}
