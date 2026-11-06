"""把「简谱 + 歌词」渲染成唱歌音频。

思路（纯 numpy + ffmpeg，不靠大模型/GPU）：
  1. 每句歌词整句用 edge-tts 合成一次（留住自然的口型、辅音和共振峰），
     同时收下 WordBoundary，均分成每个字的时间片；
  2. 对整句做逐帧基频跟踪 + 声门周期标记（epoch）—— 这是 PSOLA 类语音合成
     系统的标准前端；
  3. 按乐谱时间轴重建整句：按目标音高对应的周期，在输出时间轴上逐个摆放
     「源音频的一个周期颗粒」，交叠相加（overlap-add）。颗粒本身是从源波形
     直接取的、不重采样，所以共振峰/音色不变（不再像单纯变调那样变尖变闷）；
     相邻颗粒彼此交叠补帧，所以句子是连贯的一条，而不是一格一格的断音；
     音量由窗口和归一化，不会一个字大声一个字小声；
  4. 字头辅音按原速直通（不被拉长成糊音）；按顺序拼成整首，导出 mp3。

跑在 basic_tools 插件的 venv 里（edge-tts / numpy / pydub / ffmpeg 都现成），
用法：python sing_render.py <job.json>，stdout 最后一行为 JSON 结果。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

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


def frame_f0(y: np.ndarray, sr: int = SR, hop: int = HOP, win: int = WIN,
             fmin: int = 110, fmax: int = 620) -> list[float]:
    """逐帧基频（归一化自相关）。0 表示该帧无浊音。"""
    nfft = 1
    while nfft < 2 * win:
        nfft <<= 1
    lo = max(2, int(sr / fmax))
    hi = min(win - 2, int(sr / fmin))
    out: list[float] = []
    prev = 0.0
    for i in range(0, max(0, len(y) - win), hop):
        w = y[i:i + win]
        if float(np.sqrt(np.mean(w * w))) < 0.006:
            out.append(0.0)
            prev = 0.0
            continue
        w = w - w.mean()
        spec = np.fft.rfft(w, nfft)
        ac = np.fft.irfft(spec * np.conj(spec), nfft)[:win]
        if ac[0] <= 1e-9:
            out.append(0.0)
            prev = 0.0
            continue
        ac = ac / ac[0]
        seg = ac[lo:hi]
        # 只在局部极大值里挑周期：自相关在 lo 处往往还很高，直接取阈值会误判成最短周期
        peaks = np.nonzero((seg[1:-1] >= seg[:-2]) & (seg[1:-1] >= seg[2:]))[0] + 1
        if not len(peaks):
            out.append(0.0)
            prev = 0.0
            continue
        mx = float(seg[peaks].max())
        if mx < 0.3:
            out.append(0.0)
            prev = 0.0
            continue
        # 取第一个接近全局峰值的局部峰，避免选到 2 倍周期（低一个八度）
        good = peaks[seg[peaks] >= 0.85 * mx]
        lag = lo + int(good[0])
        f = sr / lag
        if prev > 0:  # 与上一帧差太远时按八度校正
            while f > prev * 1.4 and lag * 2 < hi:
                lag *= 2
                f = sr / lag
            while f < prev * 0.7 and lag // 2 >= lo:
                lag //= 2
                f = sr / lag
        out.append(f)
        prev = f
    return out


def midi_hz(midi: int) -> float:
    return 440.0 * (2.0 ** ((int(midi) - 69) / 12.0))


def find_epochs(y: np.ndarray, sr: int, f0s: list[float],
                hop: int = HOP) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """找声门周期标记（epoch）：返回 (标记样本位置, 该处源周期, 极性)。

    沿基频预测的时间点，在低通波形上取 |峰| 最大处，按周期往前推进；
    极性用于把负峰颗粒翻转回正峰，避免相邻颗粒反相互相抵消。
    """
    n = len(y)
    if n == 0:
        return np.zeros(0, dtype=int), np.zeros(0, dtype=int), np.zeros(0)
    k = max(3, int(sr / 1200))
    lp = np.convolve(y, np.ones(k) / k, mode="same")
    nf = len(f0s)
    prof = [float(f) for f in f0s]
    for i in range(1, nf - 1):  # 三帧中值平滑，压掉跳变
        near = [v for v in (prof[i - 1], prof[i], prof[i + 1]) if v > 0]
        if prof[i] > 0 and len(near) >= 2:
            prof[i] = float(np.median(near))
    fsamp = np.zeros(n)
    for i in range(nf):
        if prof[i] > 0:
            fsamp[i * hop: min(n, (i + 1) * hop)] = prof[i]

    ep: list[int] = []
    per: list[int] = []
    sgn: list[int] = []
    i = 0
    while True:
        while i < n and fsamp[i] <= 0:  # 跳过无浊音段
            i += hop
        if i >= n:
            break
        T = int(round(sr / fsamp[i]))
        b = min(n, i + 2 * T)
        j = i + int(np.argmax(np.abs(lp[i:b])))
        while True:  # 沿预测周期逐周期推进
            fv = float(fsamp[min(j, n - 1)])
            if fv <= 0:  # 落到无浊音区（或末尾），本段结束，别拿 0 做除数
                break
            T = int(round(sr / fv))
            if T <= 0:
                break
            ep.append(j)
            per.append(T)
            sgn.append(1 if lp[j] >= 0 else -1)
            nxt = j + T
            a2 = max(j + 1, nxt - int(0.35 * T))
            b2 = min(n, nxt + int(0.35 * T))
            if b2 <= a2 or a2 >= n:
                j = n
                break
            j = a2 + int(np.argmax(np.abs(lp[a2:b2])))
            if fsamp[min(j, n - 1)] <= 0:  # 进入无浊音段，本段结束
                break
        i = j + hop if j < n else n
    return (np.array(ep, dtype=int), np.array(per, dtype=int),
            np.array(sgn, dtype=int))


def synth_line(y: np.ndarray, spans: list[tuple[float, float]], notes: list,
               beat_sec: float, max_stretch: float = 1.8) -> np.ndarray:
    """按音符时间轴把整句「唱」出来（float 单声道数组）。

    对整句源语音做 TD-PSOLA：按目标音高的周期在输出时间轴上逐个摆放源波形
    「一个周期颗粒」，交叠相加并做窗口归一化。音高完全由颗粒间距决定（不再靠
    估算源音高比例），颗粒直接取自源波形（不改共振峰），交叠补帧（没有断音缝）。
    字头辅音（浊音起点之前那一段）按原速直通，避免被拉长成糊音。

    max_stretch 限制单个字最多被拉长几倍（按这个字自己的源时长算）。小星星这种
    「一拍一个字」的曲子里，短音节会被拉 5~7 倍，元音糊成一片、ASR 直接丢字；
    限制到 1.8 倍后字基本都还在，代价是整体节奏跟着字的长短走、比乐谱快。
    """
    total = 0.0
    for _m, b in notes:
        total += max(0.02, float(b) * beat_sec)
    if total <= 0 or len(y) < WIN:
        return np.zeros(max(1, int(round(max(0.02, total) * SR))))

    f0s = frame_f0(y, SR)
    ep, per, sgn = find_epochs(y, SR, f0s)
    nf = len(f0s)
    fsamp = np.zeros(len(y))
    for i in range(nf):
        if f0s[i] > 0:
            fsamp[i * HOP: min(len(y), (i + 1) * HOP)] = f0s[i]

    # 子段：out 起点/时长、源起点/时长、目标音高（0 表示直通、不做 PSOLA）、滑音起点
    subs: list[tuple[float, float, float, float, float, float]] = []
    t_out = 0.0
    si = 0
    last_want = 0.0
    for midi, beats in notes:
        dur = max(0.02, float(beats) * beat_sec)
        if midi is None:  # 休止符：断开滑音
            t_out += dur
            last_want = 0.0
            continue
        s0, sd = spans[si] if si < len(spans) else (
            spans[-1] if spans else (0.0, max(0.2, dur)))
        si += 1
        s0 = max(0.0, s0)
        sd = max(0.05, min(sd, len(y) / SR - s0))
        if max_stretch and max_stretch > 0:
            dur = min(dur, sd * max_stretch)  # 别把短音节拉成糊音
        want = midi_hz(midi)
        a = int(s0 * SR)
        b = int(min(len(y), (s0 + sd) * SR))
        von = -1
        if b > a:
            idx = np.nonzero(fsamp[a:b] > 0)[0]
            if len(idx):
                von = int(idx[0])
        cons = (von / SR) if von > 0 else sd  # 字头辅音时长（源）
        cons = min(sd, max(0.0, cons))
        if von > 0 and 0.012 <= cons <= 0.45 * sd and dur > 0.14:
            c_out = min(cons, 0.45 * dur)
            subs.append((t_out, c_out, s0, cons, 0.0, 0.0))
            subs.append((t_out + c_out, dur - c_out, s0 + cons, sd - cons, want, last_want))
        else:
            subs.append((t_out, dur, s0, sd, want, last_want))
        last_want = want
        t_out += dur

    n_out = int(round((t_out + 0.08) * SR))
    out = np.zeros(n_out)
    norm = np.zeros(n_out)
    if len(ep) == 0:
        out = np.zeros(n_out)  # 整句都没找到浊音周期，退化为直通
        for os_, od, ss, sd, _w, _g in subs:
            a = int(round(os_ * SR))
            b = min(n_out, int(round((os_ + od) * SR)))
            if b > a:
                out[a:b] = _copy_span(y, a, b, os_, ss, sd, od, SR)
        return out

    GLIDE = 0.025  # 音符之间的对数滑音，去掉"一级一级"的台阶感
    for os_, od, ss, sd, want, glide_from in subs:
        if want <= 0 or od <= 0:
            continue
        lo = int(np.searchsorted(ep, int(ss * SR)))
        hi = int(np.searchsorted(ep, int((ss + sd) * SR), side="right"))
        if hi <= lo:
            continue
        t = os_
        end = os_ + od
        while t < end:
            tl = t - os_
            if 0 < glide_from < want * 3 and tl < GLIDE:
                w_mid = glide_from * (want / glide_from) ** (tl / GLIDE)
            else:
                w_mid = want
            to_s = 1.0 / w_mid          # 当前目标周期（秒）
            to_samp = SR / w_mid        # 当前目标周期（采样点）
            st = ss + (t - os_) * (sd / od)
            k = int(np.searchsorted(ep, int(st * SR)))
            k = lo if k < lo else (hi - 1 if k >= hi else k)
            if k - 1 >= lo and abs(st * SR - ep[k - 1]) < abs(ep[k] - st * SR):
                k -= 1
            e = int(ep[k])
            ta = int(per[k])
            lw = int(round(max(2.0 * ta, 3.0 * to_samp)))
            if lw % 2 == 0:
                lw += 1
            half = lw // 2
            g0, g1 = e - half, e + half + 1
            if g0 < 0 or g1 > len(y):
                t += to_s
                continue
            win = np.hanning(lw)
            grain = y[g0:g1] * win
            if sgn[k] < 0:
                grain = -grain
            p = int(round(t * SR))
            w0 = p - half
            if w0 < 0:
                cut = -w0
                win, grain = win[cut:], grain[cut:]
                w0 = 0
            w1 = w0 + len(grain)
            if w1 > n_out:
                keep = n_out - w0
                if keep <= 0:
                    t += to_s
                    continue
                win, grain = win[:keep], grain[:keep]
                w1 = w0 + keep
            out[w0:w1] += grain
            norm[w0:w1] += win
            t += to_s

    # 直通音（辅音、无浊音段）用线性插值按时间映射取源波形
    copy = np.zeros(n_out)
    for os_, od, ss, sd, _w, _g in subs:
        a = int(round(os_ * SR))
        b = min(n_out, int(round((os_ + od) * SR)))
        if b <= a:
            continue
        copy[a:b] = _copy_span(y, a, b, os_, ss, sd, od, SR)

    mask = np.clip(norm / 0.6, 0.0, 1.0)
    kk = max(3, int(0.005 * SR))
    mask = np.convolve(mask, np.ones(kk) / kk, mode="same")
    voiced = out / np.maximum(norm, 1e-3)
    res = mask * voiced + (1.0 - mask) * copy
    res = _level_even(res, SR)
    return np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)


_WORLD_OK: bool | None = None


def world_available() -> bool:
    """pyworld（WORLD 声码器）是否可用；没有就退回 TD-PSOLA。"""
    global _WORLD_OK
    if _WORLD_OK is None:
        try:
            import pyworld  # noqa: F401
            _WORLD_OK = True
        except Exception:  # noqa: BLE001
            _WORLD_OK = False
    return bool(_WORLD_OK)


def synth_line_world(y: np.ndarray, spans: list[tuple[float, float]], notes: list,
                     beat_sec: float, max_stretch: float = 1.8,
                     sr: int = SR, contour: np.ndarray | None = None) -> np.ndarray:
    """用 WORLD 声码器按乐谱重唱整句（比颗粒 PSOLA 少一层「电音」）。

    与 synth_line 的区别：不再把源波形的一个周期颗粒反复摆到输出时间轴上
    （固定周期的颗粒叠加会产生幅度纹波，听感就是机械嗡嗡），而是先把整句拆成
    「基频 / 频谱包络 / 非周期成分」三条流，按输出时间轴逐帧重新取样，最后用
    WORLD 重新合成。频谱包络只做时间映射、不做频率搬移，共振峰不变；基频换成
    带滑音、颤音、微抖动的目标曲线，长时间音符也不再是死准的直线。

    字头辅音仍按原速直通（噪声型频谱原样保留），不会被拉成糊音。
    """
    import pyworld as pw
    y = np.ascontiguousarray(y, dtype=np.float64)
    fp = 0.005  # 5ms 一帧
    f0_s, tt = pw.harvest(y, sr, f0_floor=60.0, f0_ceil=800.0,
                          frame_period=fp * 1000.0)
    sp_s = pw.cheaptrick(y, f0_s, tt, sr)
    ap_s = pw.d4c(y, f0_s, tt, sr, threshold=0.85)
    nf = len(f0_s)
    if nf < 3:
        return synth_line(y, spans, notes, beat_sec, max_stretch)
    voiced_s = f0_s > 0.0
    # 逐帧 RMS：用来识别「字头其实只是 TTS 字间的静音」
    flen = int(round(fp * sr))
    ne = len(y) // flen
    fr_e = (np.sqrt(np.mean(y[:ne * flen].reshape(ne, flen) ** 2, axis=1))
            if ne > 0 else np.zeros(0))
    if len(fr_e) < nf:
        fr_e = np.pad(fr_e, (0, nf - len(fr_e)))

    total = 0.0
    for _m, b in notes:
        total += max(0.02, float(b) * beat_sec)
    if total <= 0:
        return np.zeros(1)
    nfr = int(np.ceil((total + 0.06) / fp)) + 2
    f0_o = np.zeros(nfr)
    sp_o = np.zeros((nfr, sp_s.shape[1]))
    ap_o = np.zeros((nfr, sp_s.shape[1]))
    act = np.zeros(nfr, dtype=bool)

    # 每句换一组微参数，避免所有句子颤成同一个味儿
    rng = np.random.default_rng(20261031)
    vib_hz = 5.2 + 0.9 * float(rng.random())
    vib_ph = 2.0 * np.pi * float(rng.random())
    drift_hz = 0.6 + 0.4 * float(rng.random())
    drift_ph = 2.0 * np.pi * float(rng.random())

    def _fill_holes(v: np.ndarray, max_gap: int = 12) -> None:
        """把元音中间 <60ms 的掉帧空洞补回来，避免一句里周期性掉音（听感断续）。"""
        i = 0
        n = len(v)
        while i < n:
            if v[i]:
                i += 1
                continue
            j = i
            while j < n and not v[j]:
                j += 1
            if i > 0 and j < n and (j - i) < max_gap:
                v[i:j] = True
            i = j

    def _place_note(ot: float, dur: float, fa: int, von: int, fb: int,
                    want: float, glide_from: float) -> None:
        a = max(0, int(round(ot / fp)))
        b = min(nfr, int(round((ot + dur) / fp)))
        if b <= a:
            return
        m = b - a
        # 字头辅音按原速直通；音符太短就压缩辅音，绝不把元音整个吃掉
        # （以前 mc=min(von-fa,m)：一拍短音配上 0.2s 的字头，元音帧数为 0，
        # 整个字只剩一坨噪声辅音，听感就是「吐字不清」）
        mc = 0
        if von > fa:
            mc = von - fa
            if mc > 0.55 * m:
                mc = max(1, int(0.55 * m))
        idx = np.empty(m, dtype=int)
        if mc > 0:
            if von - fa > mc:  # 被压缩过：整段辅音铺开，保持字头完整
                idx[:mc] = np.clip(
                    np.round(np.linspace(fa, von, mc, endpoint=False)).astype(int),
                    0, nf - 1)
            else:
                idx[:mc] = np.clip(fa + np.arange(mc), 0, nf - 1)
        mv = m - mc
        vsrc = von if von > fa else fa
        if mv > 0:
            # 元音只做「正向」时间映射：最多拉 3 倍（实测 2.4~3 倍仍可懂），
            # 超出部分停在最后一个源帧上。以前超 2.2 倍就在末尾两帧之间来回
            # 走（tri 倒放），共振峰过渡被倒着播——这正是「不流畅/断音」的主因。
            nat = max(1, fb - vsrc)
            cap = max(1, int(nat * 3.0))
            if mv <= cap:
                pos = np.linspace(vsrc, max(vsrc, fb - 1), mv)
            else:
                pos = np.concatenate([
                    np.linspace(vsrc, max(vsrc, fb - 1), cap),
                    np.full(mv - cap, float(fb - 1))])
            idx[mc:] = np.clip(np.round(pos).astype(int), 0, nf - 1)
            # 频谱/非周期成分在相邻源帧之间做线性插值：长音拉伸时不会
            # 一格一格地重复同一帧，音色过渡更连续，也少一层「电子颗粒感」。
            i0 = np.clip(np.floor(pos).astype(int), 0, nf - 1)
            i1 = np.minimum(i0 + 1, nf - 1)
            fr = (pos - i0)[:, None]
            sp_v = sp_s[i0] * (1.0 - fr) + sp_s[i1] * fr
            ap_v = ap_s[i0] * (1.0 - fr) + ap_s[i1] * fr

        # 整条音符（含字头）共用一条目标音高曲线，换字时不再跳音高
        t = np.arange(a, b) * fp
        if contour is not None:
            # 「跟原唱」模式：目标音高逐帧取自原唱 F0 曲线（含真人颤音/滑音），
            # 不再叠加合成颤音/scoop/滑音，否则两层 vibrato 会互相打架。
            seg = contour[a:b] if len(contour) >= b else np.zeros(m)
            f0_t = np.clip(np.where(seg > 0, seg, float(want)), 40.0, 1400.0)
            v = voiced_s[idx].copy()
            if mv > 0:
                _fill_holes(v[mc:], max_gap=mv + 1)
                if not np.any(v[mc:]):
                    v[mc:] = True
            f0_o[a:b] = np.where(v, f0_t, 0.0)
            if mc > 0:
                sp_o[a:a + mc] = sp_s[idx[:mc]]
                ap_o[a:a + mc] = np.clip(ap_s[idx[:mc]] * 1.15 + 0.02, 0.0, 1.0)
            if mv > 0:
                sp_o[a + mc:b] = sp_v
                ap_o[a + mc:b] = np.clip(ap_v * 1.06 + 0.01, 0.0, 1.0)
            act[a:b] = True
            return
        base = np.full(m, float(want))
        if 0.0 < glide_from < want * 3:
            # 滑音时长跟着音程走：小二度 ~80ms，五度以上拉到 180ms。
            # 固定 80ms 时，五度大跳相当于 90 音分/ms，听感就是「音调陡变」。
            gs = abs(12.0 * np.log2(float(want) / float(glide_from)))
            gsec = min(0.18, max(0.08, 0.06 + 0.02 * gs))
            # 短音符上滑音最多占 22%：小星星一拍约 0.4s，五度滑音本来要
            # 160ms，等于近一半时间都在过渡，听感就是「跑调、滑来滑去」。
            gsec = min(gsec, max(0.05, 0.22 * dur))
            k = np.clip((t - ot) / gsec, 0.0, 1.0)
            base = glide_from * (want / glide_from) ** k
        # 起音 scoop 只用在「从静音开口」的第一个音；每个字都 scoop 会变成打嗝。
        # -0.5 半音（-50 音分）把整句中位音高拉平约 18 音分，收到 -0.3、衰减加快，
        # 保留开口那一下的自然上扬又不明显跑低。
        scoop = (-0.3 if glide_from <= 0 else 0.0) * np.exp(
            -np.maximum(0.0, t - ot) / 0.045)
        # 颤音用整句时间轴的连续相位，不在每个音符重置：否则每换一个字
        # 颤音就断一次，听感就是「一顿一顿」的机械感
        ramp = np.clip((t - 0.25) / 0.38, 0.0, 1.0)
        # 真人唱歌的颤音比说话明显（约 30~50 音分），但要长音才慢慢展开：
        # 起音 0.22 让短音符保持干净音准，长音展开到 0.29（约 35 音分）。
        vib_amt = 0.22 + 0.07 * np.clip((t - ot) / 0.6, 0.0, 1.0)
        vib = vib_amt * ramp * np.sin(2.0 * np.pi * vib_hz * t + vib_ph)
        drift = 0.045 * np.sin(2.0 * np.pi * drift_hz * t + drift_ph)
        f0_t = np.clip(base * (2.0 ** ((scoop + vib + drift) / 12.0)),
                       40.0, 1400.0)

        v = voiced_s[idx].copy()
        if mv > 0:
            # 音符内是要唱满的浊音：harvest 在元音里漏标的全部补回（不设 60ms
            # 上限），否则 f0=0 的帧被 WORLD 合成成噪声——听感就是「断音」
            _fill_holes(v[mc:], max_gap=mv + 1)
            if not np.any(v[mc:]):  # 整段都没浊音（气声字/对齐偏了）：硬唱成浊音
                v[mc:] = True
        f0_o[a:b] = np.where(v, f0_t, 0.0)
        if mc > 0:
            sp_o[a:a + mc] = sp_s[idx[:mc]]
            ap_o[a:a + mc] = np.clip(ap_s[idx[:mc]] * 1.15 + 0.02, 0.0, 1.0)
        if mv > 0:
            # 长音内的轻微起伏，避免整段一条水平的「电音直线」
            x = np.clip((t[mc:] - ot) / max(0.15, dur), 0.0, 1.0)
            env = (0.97 + 0.06 * np.sin(np.pi * x))[:, None]
            sp_o[a + mc:b] = sp_v * env
            ap_o[a + mc:b] = np.clip(ap_v * 1.06 + 0.01, 0.0, 1.0)
        act[a:b] = True

    si = 0
    t_out = 0.0
    last_want = 0.0
    spans_t: list[tuple[float, float]] = []
    for midi, beats in notes:
        dur = max(0.02, float(beats) * beat_sec)
        if midi is None:
            t_out += dur
            last_want = 0.0
            continue
        sp = spans[si] if si < len(spans) else (
            spans[-1] if spans else (0.0, max(0.2, dur)))
        si += 1
        asr_aligned = len(sp) > 2 and sp[2] == "asr"
        s0, sd = sp[0], sp[1]
        s0 = max(0.0, s0)
        sd = max(0.05, min(sd, len(y) / sr - s0))
        # WORLD 路径不再按 max_stretch 截短音符：节奏完全贴谱，
        # 长音的咬字问题由 _place_note 里「元音限拉伸 + sustain」解决
        want = midi_hz(midi)
        fa = max(0, min(nf - 1, int(round(s0 / fp))))
        fb = max(fa + 1, min(nf, int(round((s0 + sd) / fp))))
        von = -1
        for j in range(fa, fb):
            if voiced_s[j]:
                von = j
                break
        if von < 0:
            # 字槽里一帧浊音都没有（cosy 的停顿/纯气声段）：照原样映射只会把
            # 静音频谱唱进音槽，整字变成一截空白——正是「断断续续」里最长的那几段。
            # 借整句最响的浊音段来唱这个字，宁可多唱一拍也不掉音。
            cand = np.where(voiced_s)[0]
            if len(cand):
                j0 = int(cand[np.argmax(fr_e[cand])])
                u0 = j0
                while u0 > 0 and voiced_s[u0 - 1]:
                    u0 -= 1
                u1 = j0
                while u1 + 1 < nf and voiced_s[u1 + 1]:
                    u1 += 1
                fa = max(0, u0 - 2)
                von = u0
                fb = u1 + 1
        # 尾巴上拖着的气声/静音帧不铺进元音，否则每个字结尾都会掉一下
        if von >= 0:
            lastv = -1
            for j in range(fb - 1, von - 1, -1):
                if voiced_s[j]:
                    lastv = j
                    break
            if lastv >= von:
                fb = lastv + 1
            # ASR 对齐的边界本身就是字的真实起止，字头辅音（t/x/j 这类低能量
            # 塞音/擦音）必须原样保留：再按能量裁就会把「天」的 t 裁掉，
            # 听成「年」。所以只在无对齐（能量谷切分）时才做下面两步裁剪。
            if not asr_aligned:
                # 「断断续续」主因：cosy 没有词边界，char_spans 把 TTS 字与字之间的
                # 静音也切进了每个字的「字头」。这段空白按原速直通到音槽开头，
                # 每个字起唱前都挂一截无声 → 整句一顿一顿。字头区能量远低于元音
                # 就判定是静音，裁到只剩 2 帧衔接；真辅音（擦音/塞音）能量够，不动。
                if von > fa and fb > von:
                    e_lead = float(fr_e[fa:von].max())
                    e_vow = float(np.median(fr_e[von:fb]))
                    if e_vow > 1e-4 and e_lead < 0.12 * e_vow:
                        fa = von - 2
                # 元音两端的衰减尾巴也裁掉：字槽按原速映射时，尾巴挂在每个音槽的
                # 开头/结尾，一串字听下来就是一下一下的起伏（换字处掉音）。
                # 只裁「非浊音」的低能帧：鼻音韵尾（-n/-ng）虽是能量低谷，
                # 但仍是有周期性的浊音，按 35% 能量一刀切会把它切掉。
                if fb - von >= 8:
                    vmed = float(np.median(fr_e[von:fb]))
                    thr = 0.35 * vmed
                    n0, n1 = von, fb
                    while n0 < n1 - 8 and fr_e[n0] < thr and not voiced_s[n0]:
                        n0 += 1
                    while n1 - 1 > n0 + 7 and fr_e[n1 - 1] < thr and not voiced_s[n1 - 1]:
                        n1 -= 1
                    von, fb = n0, n1
        _place_note(t_out, dur, fa, von, fb, want, last_want)
        last_want = want
        spans_t.append((t_out, dur))
        t_out += dur

    if not np.any(act):
        return np.zeros(max(1, int(round(t_out * sr))))
    if not os.environ.get("SING_NO_FILL"):
        # 输出域「放大补帧」：字槽里借不到浊音（或元音被裁得太短）时，线内
        # 仍会留下能量塌陷的帧——WORLD 把它们合成成空档，听感就是一字一顿。
        # 这里按帧能量找洞，用前后有效帧对频谱包络/非周期成分做线性插值，
        # 基频按 Hz 插值，把整句接成一条连续的声带轨迹。
        # 只补「整段拿不到浊音」的长空洞：辅音/停顿本来就是低能量非浊音帧，
        # 补进去会被插值成元音包络，塞音擦音直接糊掉——「满天」听成「漫年」、
        # 「挂在天上放光明」听成「我愛在天上的光明」，主观上就是“漏字”。
        # 而且要够长（>=125ms）才补：正常换字的辅音缝只有几十毫秒，不能动。
        nz = np.nonzero(act)[0]
        a0, a1 = int(nz[0]), int(nz[-1]) + 1
        e = np.sqrt(np.maximum(np.sum(sp_o[:a1] ** 2, axis=1), 0.0))
        pos_e = e[e > 0]
        if len(pos_e) and a1 > a0 + 4:
            thr = 0.12 * float(np.median(pos_e))
            bad = np.zeros(nfr, dtype=bool)
            bad[a0:a1] = (e[a0:a1] < thr) & (f0_o[a0:a1] <= 0.0)
            i = a0
            while i < a1:
                if not bad[i]:
                    i += 1
                    continue
                j = i
                while j < a1 and bad[j]:
                    j += 1
                if j - i < 25:  # <125ms 的缝是正常换字辅音，补了反而糊字
                    i = j
                    continue
                p = i - 1 if i - 1 >= a0 else -1
                q = j if j < a1 else -1
                if p >= 0 and q >= 0:
                    w = np.linspace(0.0, 1.0, j - i + 2)[1:-1][:, None]
                    sp_o[i:j] = sp_o[p] * (1.0 - w) + sp_o[q] * w
                    ap_o[i:j] = ap_o[p] * (1.0 - w) + ap_o[q] * w
                elif p >= 0:
                    sp_o[i:j] = sp_o[p]
                    ap_o[i:j] = ap_o[p]
                elif q >= 0:
                    sp_o[i:j] = sp_o[q]
                    ap_o[i:j] = ap_o[q]
                f0p = f0_o[p] if p >= 0 else 0.0
                f0q = f0_o[q] if q >= 0 else 0.0
                if f0p > 0 and f0q > 0:
                    f0_o[i:j] = np.linspace(f0p, f0q, j - i + 2)[1:-1]
                elif f0p > 0:
                    f0_o[i:j] = f0p
                elif f0q > 0:
                    f0_o[i:j] = f0q
                i = j
        # 字边界上偶尔会留下孤立的半八度尖刺（补帧插入的帧与邻帧频谱突变）。
        # 判据：本帧基频偏离前后 ±2 帧中值 >3 半音，且邻域自身一致（≤1 半音），
        # 就把它按邻域中值回填——这样只杀孤立尖刺，不碰真正的滑音和颤音。
        if not os.environ.get("SING_NO_SPIKE"):
            med5 = np.zeros(nfr)
            for i in range(nfr):
                s = f0_o[max(0, i - 2):i + 3]
                s = s[s > 0]
                med5[i] = float(np.median(s)) if len(s) >= 3 else 0.0
            for i in range(nfr):
                if f0_o[i] <= 0 or med5[i] <= 0:
                    continue
                if i < 2 or i >= nfr - 2:
                    continue
                s = f0_o[i - 2:i + 3]
                s = s[s > 0]
                if len(s) < 4:
                    continue
                d_c = 12 * abs(np.log2(f0_o[i] / med5[i]))
                spread = 12 * abs(np.log2(s.max() / s.min())) if s.min() > 0 else 0
                if d_c > 3.0 and spread <= 1.0:
                    f0_o[i] = med5[i]
    if os.environ.get("SING_DUMP_F0"):
        try:
            np.save(os.environ["SING_DUMP_F0"], f0_o)
        except Exception:  # noqa: BLE001
            pass
    y_out = np.asarray(pw.synthesize(f0_o, sp_o, ap_o, sr,
                                     frame_period=fp * 1000.0))
    # 实际长度按「被 max_stretch 收过的音符」算，别拿乐谱原时长做尾部静音
    n_out = int(round((t_out + 0.02) * sr))
    if len(y_out) < n_out:
        y_out = np.pad(y_out, (0, n_out - len(y_out)))
    else:
        y_out = y_out[:n_out]
    # 休止符/句界处 10ms 过渡，避免硬切出「咔」
    mask = np.repeat(act.astype(np.float64), int(round(sr * fp)))
    if len(mask) < n_out:
        mask = np.pad(mask, (0, n_out - len(mask)), mode="edge")
    mask = mask[:n_out]
    k = max(5, int(0.01 * sr))
    k += 1 - k % 2
    win = np.hanning(k)
    mask = np.convolve(mask, win / max(1e-9, win.sum()), mode="same")
    y_out = y_out * np.clip(mask, 0.0, 1.0)
    y_out = _level_even(y_out, sr)
    # 整句一条缓慢的「呼吸」曲线：起句 10% 渐入 → 中段满 → 句尾 25% 轻收。
    # 之前是给每个音符单独套力度包络（0.62 起、结尾掉 30%），一串音符听下来
    # 就是音量一格一格地打嗝，反而比不加更难听；起伏放在句子层面才像人唱。
    tt = np.arange(n_out) / sr
    x = np.clip(tt / max(1e-3, n_out / sr), 0.0, 1.0)
    y_out = y_out * (
        (0.82 + 0.18 * np.clip(x / 0.10, 0.0, 1.0))
        * (1.0 - 0.12 * np.clip((x - 0.75) / 0.25, 0.0, 1.0))
    )
    return np.nan_to_num(y_out, nan=0.0, posinf=0.0, neginf=0.0)


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
    a = np.clip(arr, -1.0, 1.0)
    return AudioSegment(
        data=(a * 32767).astype(np.int16).tobytes(),
        sample_width=2, frame_rate=sr, channels=1,
    )


async def tts_line(text: str, mp3_path: str, attempts: int = 5,
                   voice: str = "zh-CN-XiaoyiNeural") -> list[tuple[float, float, str]]:
    """整句合成一次，返回 WordBoundary [(开始秒, 时长秒, 词)]。"""
    import edge_tts
    last: Exception | None = None
    for i in range(attempts):
        try:
            com = edge_tts.Communicate(
                text, voice, rate="+0%", pitch="+0Hz",
                boundary="WordBoundary",
            )
            audio: list[bytes] = []
            bounds: list[tuple[float, float, str]] = []
            async for item in com.stream():
                if item["type"] == "audio":
                    audio.append(item["data"])
                elif item["type"] == "WordBoundary":
                    # edge 的 offset/duration 单位是 100ns
                    bounds.append((item["offset"] / 1e7, item["duration"] / 1e7,
                                   str(item.get("text") or "")))
            if not audio:
                raise RuntimeError("edge-tts 没返回音频")
            with open(mp3_path, "wb") as f:
                f.write(b"".join(audio))
            return bounds
        except Exception as e:  # noqa: BLE001
            last = e
            await asyncio.sleep(0.8 + i * 1.2)
    raise RuntimeError(f"TTS {attempts} 次都失败: {type(last).__name__}: {last}")


# 本地 CosyVoice2 克隆声线（聊天语音系统同款，主人点的「晓波」）。没有词边界，
# 切字走 char_spans 的能量均分兜底；情绪语速统一 1.0（唱歌自己会拉时长）。
COSY_URL = os.environ.get("COSYVOICE_URL", "http://127.0.0.1:8714")
_COSY_PROMPT = ("今天天气真好呀，我们一起去公园散步吧。"
                "你看那边的花都开了，心情一下子就亮起来了呢。")
_COSY_AF = ("highpass=f=90,equalizer=f=2800:t=q:w=1.0:g=3,"
            "loudnorm=I=-17:TP=-1.5:LRA=11")


_COSY_URL_PARTS = urlsplit(COSY_URL)
_COSY_HOST = _COSY_URL_PARTS.hostname or "127.0.0.1"
_COSY_PORT = _COSY_URL_PARTS.port or 8714
_COSY_LOCAL = threading.local()


def _cosy_connection(timeout: float = 300.0) -> http.client.HTTPConnection:
    """每线程复用一条长连接，整首歌几十句不用反复重连。"""
    conn = getattr(_COSY_LOCAL, "conn", None)
    if conn is None:
        conn = http.client.HTTPConnection(_COSY_HOST, _COSY_PORT, timeout=timeout)
        _COSY_LOCAL.conn = conn
    return conn


def _cosy_drop() -> None:
    conn = getattr(_COSY_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass
    _COSY_LOCAL.conn = None


def tts_line_cosy_sync(text: str, out_path: str, speed: float = 1.0,
                       prompt_wav: str | None = None,
                       prompt_text: str | None = None) -> None:
    body = {"text": text, "speed": speed, "fmt": "mp3",
            "prompt_text": prompt_text or _COSY_PROMPT, "af": _COSY_AF}
    if prompt_wav:
        body["prompt_wav"] = prompt_wav  # 服务端按请求换参考声线（A/B 音色用）
    payload = json.dumps(body).encode("utf-8")
    try:
        conn = _cosy_connection()
        conn.request(
            "POST",
            "/tts",
            body=payload,
            headers={"Content-Type": "application/json", "Connection": "keep-alive"},
        )
        response = conn.getresponse()
        audio = response.read()
        if response.will_close:
            _cosy_drop()
        if response.status >= 400:
            raise RuntimeError(f"CosyVoice HTTP {response.status}: {audio[:200]!r}")
    except (http.client.HTTPException, OSError):
        _cosy_drop()
        raise
    if not audio or len(audio) < 1000:
        raise RuntimeError("CosyVoice 返回音频过短/为空")
    with open(out_path, "wb") as f:
        f.write(audio)


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


def sapi_line(text: str, wav_path: str) -> bool:
    """离线兜底：用 Windows SAPI 合成整句（没有词边界，按字数均摊即可）。"""
    if sys.platform != "win32":
        return False
    txt = wav_path + ".txt"
    raw = wav_path + ".sapi.wav"
    with open(txt, "w", encoding="utf-8") as f:
        f.write(text)
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$t=Get-Content -LiteralPath '" + txt + "' -Encoding UTF8 -Raw;"
        "$sp=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$sp.SetOutputToWaveFile('" + raw + "');"
        "$sp.Speak($t);$sp.Dispose()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=60, check=True)
        if not (os.path.exists(raw) and os.path.getsize(raw) > 1024):
            return False
        to_wav(raw, wav_path)
        return os.path.exists(wav_path) and os.path.getsize(wav_path) > 1024
    except Exception:  # noqa: BLE001
        return False


async def render(job: dict) -> dict:
    from pydub import AudioSegment
    bpm = float(job.get("bpm") or 96)
    beat_sec = 60.0 / bpm
    tmpdir = tempfile.mkdtemp(prefix="sing_")
    try:
        # ── 第一遍：整句 TTS + 切字，量出每个字的自然时长 ──
        # 「唱太慢导致青涩」：写死 96bpm 时一拍一个字要把 0.35s 的字硬拉成
        # 0.625s（近 2 倍），全程拖着唱，拘谨放不开。先按歌词密度反推速度：
        # 平均拉伸控制在 ~1.5 倍内——字头弹得出去，元音也来得及唱满。
        prepared: list[dict] = []
        for li, line in enumerate(job.get("lines") or []):
            text = str(line.get("text") or "")
            chars = _HAN_RE.findall(text)
            notes = line.get("notes") or []
            singable = [n for n in notes if n[0] is not None]
            if not chars or not singable:
                continue
            while len(chars) < len(singable):
                chars.append(chars[-1])
            chars = chars[: len(singable)]

            base = os.path.join(tmpdir, f"line{li}")
            mp3, wav = base + ".mp3", base + ".wav"
            bounds: list[tuple[float, float, str]] = []
            voice = str(job.get("voice") or "zh-CN-XiaoyiNeural")
            try:
                if voice == "cosy":
                    try:
                        pwav = str(job.get("prompt_wav") or "") or None
                        ptxt = str(job.get("prompt_text") or "") or None
                        await asyncio.to_thread(tts_line_cosy_sync, text, mp3,
                                                1.0, pwav, ptxt)
                        bounds = []
                    except Exception as ce:  # noqa: BLE001
                        print(f"[sing_render] 第{li+1}句 CosyVoice 失败"
                              f"({type(ce).__name__})，回退 edge 晓晓", file=sys.stderr)
                        bounds = await tts_line(text, mp3, voice="zh-CN-XiaoxiaoNeural")
                else:
                    bounds = await tts_line(text, mp3, voice=voice)
                to_wav(mp3, wav)
            except Exception as e:  # noqa: BLE001
                print(f"[sing_render] 第{li+1}句 edge-tts 失败({type(e).__name__})，用本地 SAPI 兜底",
                      file=sys.stderr)
                if not sapi_line(text, wav):
                    print(f"[sing_render] 第{li+1}句彻底失败，留静音", file=sys.stderr)
                    prepared.append({"li": li, "notes": notes, "failed": True})
                    continue
                bounds = []
            y = read_wav(wav)
            total_sec = len(y) / SR
            v0, v1 = voiced_range(y, SR)
            # edge 给的词边界和实际音频常有几十毫秒偏差，按首字发音起点对齐
            if bounds:
                delta = max(-0.35, min(0.35, v0 - bounds[0][0]))
                if abs(delta) > 0.02:
                    bounds = [(s + delta, d, w) for s, d, w in bounds]
            spans = char_spans(bounds, chars, total_sec, (v0, v1),
                               _frame_energy(y, SR))
            if not bounds:
                # CosyVoice 没有词边界：优先用 ASR 词级时间戳切字，
                # 比按能量谷猜的边界准得多（短字不会被切掉半截）
                asr_spans = asr_char_spans(wav, chars, total_sec, (v0, v1))
                if asr_spans:
                    spans = asr_spans
            prepared.append({"li": li, "text": text, "notes": notes, "y": y,
                             "spans": spans, "v0": v0, "v1": v1,
                             # 句后停顿：跟原唱模式按原曲实际间隙给；-1 = 没指定，用默认 320ms
                             "gap_after": (max(0.0, min(0.8, float(line["gap_after"])))
                                           if line.get("gap_after") is not None
                                           else -1.0),
                             "contour": (np.asarray(line["contour"], dtype=np.float64)
                                         if line.get("contour") else None)})

        # ── 自适应速度：取各句「自然时长/拍数」的中位数，只提速、不拖慢 ──
        # 「跟原唱」模式跳过：拍数就是原唱秒数，提速会破坏原曲节奏
        ideals: list[float] = []
        if not any(d.get("contour") is not None for d in prepared):
            for d in prepared:
                if d.get("failed"):
                    continue
                beats = sum(max(0.02, float(b)) for _m, b in d["notes"])
                nat = sum(float(s[1]) for s in d["spans"])
                if beats > 0 and nat > 0:
                    ideals.append(nat / beats)
        if ideals:
            ideals.sort()
            n_ = len(ideals)
            med = (ideals[n_ // 2] if n_ % 2
                   else 0.5 * (ideals[n_ // 2 - 1] + ideals[n_ // 2]))
            fit = max(0.40, min(0.70, med * 1.5))
            if fit < beat_sec:
                print(f"[sing_render] 速度自适应: bpm {60.0 / beat_sec:.0f} → "
                      f"{60.0 / fit:.0f}（字自然密度 {med:.2f}s/拍）", file=sys.stderr)
                beat_sec = fit

        # ── 第二遍：按定好的速度逐句合成 ──
        made: list[AudioSegment] = []
        for d in prepared:
            notes = d["notes"]
            if d.get("failed"):
                gap = int(sum(max(0.02, float(b)) for _m, b in notes) * beat_sec * 1000)
                made.append(AudioSegment.silent(duration=gap))
                continue
            y, spans, v0, v1 = d["y"], d["spans"], d["v0"], d["v1"]
            try:
                ms = float(job.get("max_stretch", 5.0))
                if world_available():
                    try:
                        audio = synth_line_world(y, spans, notes, beat_sec, ms,
                                                 contour=d.get("contour"))
                    except Exception as we:  # noqa: BLE001
                        print(f"[sing_render] 第{d['li']+1}句 WORLD 合成失败，回退 PSOLA: "
                              f"{type(we).__name__}: {we}", file=sys.stderr)
                        audio = synth_line(y, spans, notes, beat_sec,
                                           min(ms, 1.8))
                else:
                    audio = synth_line(y, spans, notes, beat_sec, min(ms, 1.8))
                # 句首/句尾做 5ms 淡入淡出：PSOLA 颗粒在边界处相位不连续，
                # 直接接静音会「咔」一下，接下一句也会顶出机械感。
                # 句后停顿用原曲实际间隙（gap_after），别再一律 320ms：
                # 跟原唱时一字一顿的「断句感」就来自这个固定停顿。
                gap_ms = int(round(d.get("gap_after", -1.0) * 1000))
                made.append(
                    arr_to_seg(audio).fade_in(5).fade_out(5)
                    + AudioSegment.silent(duration=gap_ms if gap_ms >= 0 else 320)
                )
            except Exception as e:  # noqa: BLE001
                print(f"[sing_render] 第{d['li']+1}句合成失败: {type(e).__name__}: {e}",
                      file=sys.stderr)
                # 兜底：把整句按原速塞进目标时长，至少还能听出字，别让整句变静音
                dur = sum(float(b) for _, b in notes) * beat_sec
                a_n = max(1, int(round(dur * SR)))
                try:
                    audio = _copy_span(y, 0, a_n, 0.0, v0,
                                       max(0.05, v1 - v0), max(0.05, dur), SR)
                    made.append(arr_to_seg(audio) + AudioSegment.silent(duration=320))
                except Exception:  # noqa: BLE001
                    gap = int(dur * 1000)
                    made.append(AudioSegment.silent(duration=gap + 320))
            print(f"[sing_render] 第 {d['li']+1} 句完成：{d['text']}", file=sys.stderr)

        if not made:
            raise RuntimeError("一个音符都没渲染出来")
        merged = made[0]
        for seg in made[1:]:
            merged = merged + seg
        peak = merged.max_dBFS
        if -99.0 < peak < -2.0:
            merged = merged.apply_gain(min(18.0, -3.0 - peak))
        out = str(job.get("out") or os.path.join(tmpdir, "sing.mp3"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        merged.export(out, format="mp3", bitrate="128k")
        return {"ok": True, "path": out, "duration": round(merged.duration_seconds, 2)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> None:
    if not ffmpeg():
        print(json.dumps({"ok": False, "error": "ffmpeg 找不到"}))
        return
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    try:
        res = asyncio.run(render(job))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
