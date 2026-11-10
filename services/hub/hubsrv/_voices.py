"""声线管理：cosyvoice_ref/profiles 下的克隆声线目录与激活标记。"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ._admins import ROOT

# 声线资产已归拢到 audio/ 子包，兼容旧的平铺位置
_VOICE_CANDIDATES = (
    ROOT / "audio" / "cosyvoice_ref" / "profiles",
    ROOT / "cosyvoice_ref" / "profiles",
)
VOICE_ROOT = next((p for p in _VOICE_CANDIDATES if p.is_dir()), _VOICE_CANDIDATES[0])

_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff-]")


def _voice_dir(name: str) -> Path:
    name = str(name or "").strip()
    if not name or _SAFE_NAME.sub("", name) != name or name.startswith("."):
        raise ValueError("声线名称非法")
    return VOICE_ROOT / name


def _voice_prompt(folder: Path) -> str:
    """参考音对应的转写文本。

    prompt.txt 里如果是「（使用 instruct 模式，无需参考文本）」这类备注，
    就不是转写，不能喂给 inference_zero_shot（参考音和文本对不上，音色会走样）。
    """
    path = folder / "prompt.txt"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8-sig").strip()
    if (not text or text.startswith(("（", "("))
            or "无需参考文本" in text or "instruct" in text.lower()):
        return ""
    return text


def _voice_mode(folder: Path) -> Tuple[str, str]:
    """按 CosyVoice2 实际推理路径判定模式（cosyvoice_tts_server.synthesize）。

    有参考音 + 对应转写 -> inference_zero_shot（零样本克隆）
    只有参考音           -> inference_cross_lingual（跨语种克隆，不用转写）
    连参考音都没有       -> 合成不了

    ft_model_*.pth 只是训练产物，TTS 服务当前只加载 CosyVoice2 基座，
    不会走 inference_sft —— 所以不能把「有权重文件」当成「微调模型在用」。
    """
    has_ref = (folder / "ref.wav").exists()
    if not has_ref:
        return "broken", "缺参考音"
    if _voice_prompt(folder):
        return "zero_shot", "零样本克隆"
    return "cross_lingual", "跨语种克隆"


def voices() -> Dict[str, Any]:
    active = ""
    marker = VOICE_ROOT / ".active"
    if marker.exists():
        active = marker.read_text(encoding="utf-8-sig").strip()
    rows: List[Dict[str, Any]] = []
    if VOICE_ROOT.exists():
        for folder in sorted((p for p in VOICE_ROOT.iterdir() if p.is_dir())):
            meta_path = folder / "meta.txt"
            meta = ""
            if meta_path.exists():
                meta = meta_path.read_text(encoding="utf-8-sig").strip()
            mode, mode_label = _voice_mode(folder)
            rows.append({
                "name": folder.name,
                "meta": meta,
                "mode": mode,
                "mode_label": mode_label,
                "has_ref": (folder / "ref.wav").exists(),
                # 训练权重存在 ≠ 服务在用；前端据此显示「未加载」，不再冒充微调模型
                "ft_weight": any(folder.glob("ft_model_*.pth")),
                "active": folder.name == active,
            })
    return {"active": active, "voices": rows}


def switch_voice(name: str) -> Dict[str, Any]:
    folder = _voice_dir(name)
    if not (folder / "ref.wav").exists():
        raise ValueError("该声线缺少 ref.wav")
    VOICE_ROOT.mkdir(parents=True, exist_ok=True)
    (VOICE_ROOT / ".active").write_text(folder.name + "\n", encoding="utf-8")
    return {"ok": True, "active": folder.name}


def delete_voice(name: str) -> Dict[str, Any]:
    folder = _voice_dir(name)
    if not folder.exists():
        raise ValueError("声线不存在")
    shutil.rmtree(folder)
    remaining = sorted(p.name for p in VOICE_ROOT.iterdir() if p.is_dir())
    active = ""
    marker = VOICE_ROOT / ".active"
    if marker.exists():
        active = marker.read_text(encoding="utf-8-sig").strip()
    if name == active or not active:
        active = next((n for n in remaining if n == "小忆"), remaining[0] if remaining else "")
        marker.write_text((active + "\n") if active else "", encoding="utf-8")
    return {"ok": True, "active": active}


def add_voice(name: str, ref_path: str, prompt: str = "", meta: str = "自定义") -> Dict[str, Any]:
    folder = _voice_dir(name)
    if folder.exists():
        raise ValueError("声线已存在: " + folder.name)
    src = Path(str(ref_path or "").strip())
    if not src.is_file():
        raise ValueError("参考音频不存在: " + str(src))
    if src.suffix.lower() != ".wav":
        raise ValueError("参考音频必须是 .wav")
    folder.mkdir(parents=True)
    shutil.copy2(src, folder / "ref.wav")
    (folder / "prompt.txt").write_text(str(prompt or "").strip() + "\n", encoding="utf-8")
    (folder / "meta.txt").write_text(str(meta or "自定义").strip() + "\n", encoding="utf-8")
    return {"ok": True, "name": folder.name}


def voice_audio(name: str) -> Any:
    from infra import Blob

    folder = _voice_dir(name)
    ref = folder / "ref.wav"
    if not ref.exists():
        raise ValueError("没有可试听的 ref.wav")
    return Blob(ref.read_bytes(), "audio/wav")
