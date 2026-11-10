"""sing_render 拆分：歌词-音频对齐。

从 sing_render.py 按职责拆出；sing_render.py 保留为门面与脚本入口。
有声区间估计、短时能量、按能量谷切字（char_spans）、faster-whisper
词级时间戳对齐（asr_char_spans）都在这里。ASR 模型懒加载状态
（_ASR_MODEL / _ASR_FAILED）是全局可变状态的唯一归属。
"""
from __future__ import annotations

import numpy as np

from sing_util import _HAN_RE


def voiced_range(y: np.ndarray, sr: int) -> tuple[float, float]:
    """找有声音的起止时间（去掉首尾静音），估不出来就给整段。"""
    frame = 1024
    if len(y) < frame:
        return 0.0, len(y) / sr
    idx: list[int] = []
    for i in range(0, len(y) - frame, frame):
        w = y[i:i + frame]
        if float(np.sqrt(np.mean(w * w))) > 0.012:
            idx.append(i)
    if not idx:
        return 0.0, len(y) / sr
    return idx[0] / sr, min(len(y) / sr, (idx[-1] + frame) / sr)


def _frame_energy(y: np.ndarray, sr: int, hop: float = 0.01) -> np.ndarray:
    """10ms 一帧的短时能量，用来找字与字之间的低谷（辅音/换字点）。"""
    hn = max(1, int(round(hop * sr)))
    n = len(y) // hn
    if n <= 0:
        return np.zeros(1)
    e = np.sqrt(np.mean(
        y[: n * hn].reshape(n, hn) ** 2, axis=1) + 1e-10)
    k = 5
    if n > k:
        e = np.convolve(np.pad(e, (k // 2, k // 2), mode="edge"),
                        np.ones(k) / k, mode="valid")[:n]
    return e


def char_spans(bounds: list[tuple[float, float, str]], chars: list[str],
               total_sec: float, vrange: tuple[float, float],
               env: np.ndarray | None = None) -> list[tuple[float, float]]:
    """把每个词的时长分给它的汉字，优先对齐到音频里的真实换字低谷。

    edge 的 WordBoundary 是按词组给的（如「一闪一闪」一个 0.74s 的区间），
    原来按字数均分，会把刀切在元音中间：辅音被劈开、字的起音丢一半，
    听感就是「吐字不清」。有 env 时在每个字的预期位置附近找能量低谷
    （换字/辅音的落点）作为边界，让每个字拿到自己完整的一段。
    """
    v0, v1 = vrange
    # cosy 没有词边界（bounds 空）：整句当成一个「词」交给下面的低谷切分，
    # 别退化成均摊——均摊的刀常切在元音中间，每个字起头都落在能量谷底，
    # 听感就是一字一顿的断续
    if not bounds and chars and (v1 - v0) > 0.05:
        bounds = [(v0, v1 - v0, "".join(chars))]
    spans: list[tuple[float, float]] = []
    for start, dur, word in bounds:
        n = len(_HAN_RE.findall(word))
        if n <= 0:
            continue
        if env is None or n == 1:
            step = dur / n
            for k in range(n):
                spans.append((start + k * step, step))
            continue
        fs = max(0, int(round(start / 0.01)))
        fe = min(len(env), int(round((start + dur) / 0.01)))
        if fe - fs < n * 4:  # 音频区间太短，退回均分
            step = dur / n
            for k in range(n):
                spans.append((start + k * step, step))
            continue
        cuts = [fs]
        seg = (fe - fs) / n
        for k in range(1, n):
            center = fs + k * seg
            lo = int(max(fs + 3, center - 0.45 * seg))
            hi = int(min(fe - 3, center + 0.45 * seg))
            if cuts and lo < cuts[-1] + 7:  # 相邻边界至少隔 70ms
                lo = cuts[-1] + 7
            if hi <= lo:
                cuts.append(int(center))
                continue
            e = env[lo:hi]
            cuts.append(lo + int(np.argmin(e)))
        cuts.append(fe)
        for k in range(n):
            s0 = cuts[k] * 0.01
            s1 = cuts[k + 1] * 0.01
            spans.append((max(0.0, s0), max(0.06, s1 - s0)))
    if len(spans) < len(chars):
        # 没拿到词边界：在有声区间里按字数均摊（别铺到尾部静音上）
        have = len(spans)
        need = len(chars) - have
        step = max(0.12, (v1 - v0) / max(1, need))
        for k in range(need):
            spans.append((min(v1, v0 + k * step), step))
    # 掐掉超出有声区间的时长（句尾常有 TTS 自带的静音，映射进去会唱成空白）
    fixed: list[tuple[float, float]] = []
    for start, dur in spans[: len(chars)]:
        if start >= v1:
            start = max(v0, v1 - 0.12)
        dur = max(0.06, min(dur, total_sec - start, max(0.06, v1 - start)))
        fixed.append((max(0.0, start), dur))
    return fixed


_ASR_MODEL = None
_ASR_FAILED = False


def _asr_model():
    """懒加载 faster-whisper（可选依赖，没有就退回能量谷切分）。"""
    global _ASR_MODEL, _ASR_FAILED
    if _ASR_FAILED:
        return None
    if _ASR_MODEL is None:
        try:
            from faster_whisper import WhisperModel
            _ASR_MODEL = WhisperModel("small", device="cpu", compute_type="int8")
        except Exception:  # noqa: BLE001
            _ASR_FAILED = True
            return None
    return _ASR_MODEL


def asr_char_spans(wav_path: str, chars: list[str], total_sec: float,
                   vrange: tuple[float, float]) -> list[tuple] | None:
    """用 faster-whisper 的词级时间戳把整句切成逐字区间。

    CosyVoice 不带词边界，`char_spans` 只能按能量谷猜换字点：短字（如「天」）
    常被切到只有 0.07s，拉成整拍后咬字就被毁掉。ASR 给出每个词的真实起止，
    把词的时长按字数均分即可得到逐字起止；末尾再放 30ms 收尾把鼻音韵尾带上。
    对齐结果带 "asr" 标记，`synth_line_world` 会用它的真实边界、不再按能量裁字。
    """
    model = _asr_model()
    if model is None or not chars:
        return None
    try:
        from pydub import AudioSegment
        seg16 = (AudioSegment.from_wav(wav_path).set_frame_rate(16000)
                 .set_channels(1))
        arr = np.array(seg16.get_array_of_samples(), dtype=np.float32) / 32768.0
        segments, _ = model.transcribe(
            arr, language="zh", beam_size=5, vad_filter=False,
            condition_on_previous_text=False, word_timestamps=True)
        pieces: list[tuple[float, float]] = []
        for s in segments:
            for w in (s.words or []):
                n = len(_HAN_RE.findall(w.word or ""))
                st, en = float(w.start), float(w.end)
                if n <= 0 or en <= st:
                    continue
                step = (en - st) / n
                for k in range(n):
                    pieces.append((st + k * step, step))
        if len(pieces) < len(chars):
            return None
        pieces = pieces[:len(chars)]
        v1 = vrange[1]
        out: list[tuple] = []
        for k, (st, sd) in enumerate(pieces):
            st = max(0.0, st)
            end = st + sd
            if k + 1 < len(pieces):
                nxt = pieces[k + 1][0]
                # 相邻字之间没有长停顿就连起来；有停顿就只留 30ms 收尾，
                # 别把整段静音铺进音符（否则长出一截空白，听感像漏字）
                end = (max(end, nxt) if nxt <= end + 0.04 else end + 0.03)
            else:
                end = max(end, min(total_sec, v1))
            dur = max(0.06, min(total_sec - st, end - st))
            out.append((st, dur, "asr"))
        return out
    except Exception:  # noqa: BLE001
        return None
