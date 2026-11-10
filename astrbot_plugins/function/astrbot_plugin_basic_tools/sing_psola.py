"""sing_render 拆分：TD-PSOLA 颗粒合成。

从 sing_render.py 按职责拆出；sing_render.py 保留为门面与脚本入口。
逐帧基频跟踪（frame_f0）、声门周期标记（find_epochs）、按乐谱时间轴
重摆周期颗粒（synth_line）都在这里。pyworld 可用性缓存（_WORLD_OK）
是全局可变状态的唯一归属；WORLD 路径在 sing_world.py。
"""
from __future__ import annotations

import numpy as np

from sing_util import HOP, SR, WIN, _copy_span, _level_even, midi_hz


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
