"""sing_render 拆分：音频基础工具。

从 sing_render.py 按职责拆出；sing_render.py 保留为门面与脚本入口。
ffmpeg 调用、wav 读写、midi 频率换算、响度均衡（_level_even）、
线性插值拷贝片段（_copy_span）、float 数组转 AudioSegment 都在这里。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import numpy as np

SR = 44100
HOP = 256
WIN = 1024
FFMPEG_CAND = os.path.expandvars(
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"
)
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if p:
        return p
    return FFMPEG_CAND if os.path.exists(FFMPEG_CAND) else ""


def run(cmd: list[str], timeout: int = 30) -> None:
    subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)


def to_wav(src: str, dst: str) -> None:
    run([ffmpeg(), "-y", "-v", "quiet", "-i", src, "-ac", "1", "-ar", str(SR), "-f", "wav", dst])


def read_wav(path: str) -> np.ndarray:
    from pydub import AudioSegment
    seg = AudioSegment.from_file(path, format="wav")
    y = np.array(seg.get_array_of_samples(), dtype=np.float64)
    y /= float(1 << (8 * seg.sample_width - 1))
    if seg.channels > 1:
        y = y[:: seg.channels]
    return y


def midi_hz(midi: int) -> float:
    return 440.0 * (2.0 ** ((int(midi) - 69) / 12.0))


def _level_even(res: np.ndarray, sr: int) -> np.ndarray:
    """轻度响度均衡：按 40ms 局部能量做半程压缩，避免一字大声一字小声。

    增益曲线必须再平滑一次：原来是逐样本换增益，等于给声音叠了一层几十 Hz 的
    幅度调制，听感就是「电音/机械嗡嗡」。这里用 ~120ms 滑动平均把增益变成缓慢
    变化，只保留响度修正，不引入高频抖动。
    """
    n = len(res)
    if n < 64:
        return res
    half = max(1, int(0.02 * sr))
    c = np.cumsum(res ** 2)
    c0 = np.concatenate(([0.0], c))
    idx = np.arange(n)
    lo = np.maximum(0, idx - half)
    hi = np.minimum(n, idx + half + 1)
    env = np.sqrt(np.maximum(c0[hi] - c0[lo], 0.0) / (hi - lo)) + 1e-4
    loud = env > 0.01
    if not np.any(loud):
        return res
    ref = float(np.median(env[loud]))
    gain = np.clip((ref / env) ** 0.45, 0.5, 2.5)
    k = max(5, int(0.12 * sr))
    k += 1 - k % 2
    pad = k // 2
    win = np.hanning(k)
    win /= max(1e-9, win.sum())
    gain = np.convolve(np.pad(gain, (pad, pad), mode="edge"), win, mode="valid")
    if len(gain) < n:
        gain = np.pad(gain, (0, n - len(gain)), mode="edge")
    gain = np.clip(gain[:n], 0.7, 1.8)
    return res * gain


def _copy_span(y: np.ndarray, out_a: int, out_b: int, out_start: float,
               src_start: float, src_len: float, out_len: float, sr: int) -> np.ndarray:
    """把源区间 [src_start, src_start+src_len) 线性映射到输出样本 [out_a, out_b)。"""
    tt = np.arange(out_a, out_b) / sr
    st = src_start + (tt - out_start) * (src_len / max(1e-6, out_len))
    idx = np.clip(st * sr, 0, len(y) - 1)
    i0 = np.floor(idx).astype(int)
    i1 = np.minimum(i0 + 1, len(y) - 1)
    fr = idx - i0
    return y[i0] * (1 - fr) + y[i1] * fr


def arr_to_seg(arr: np.ndarray, sr: int = SR):
    from pydub import AudioSegment
    # 超满量程时等比收回，别硬削波（_level_even 最多放大 1.8×，会顶破 0dBFS）
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    if peak > 0.999:
        arr = arr * (0.999 / peak)
    return AudioSegment(
        data=(arr * 32767).astype(np.int16).tobytes(),
        sample_width=2, frame_rate=sr, channels=1,
    )
