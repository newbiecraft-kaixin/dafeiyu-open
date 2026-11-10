"""从 mp3 扒旋律（纯 CPU：pyworld + numpy + ffmpeg，不依赖模型/GPU）。

流程：
  1. 解码成 22050Hz 单声道；
  2. 快速自相关过一遍整首，挑人声最连续的一段（避开前奏/间奏）；
  3. 对这一段用 WORLD harvest 测音高（对伴奏鲁棒性远好于自相关），
     再用 Viterbi 在「候选半音（含 ±1/±2 八度）」上做全局平滑，
     把八度错判和单帧乱跳一起修掉；
  4. 切音符；把间隔 < FOLD_GAP 的快速音群（转音/过腔）折成一个音节，
     音节数贴近真实歌词，填词不会被转音炸开；
  5. 移调到适合人声的音域；
  6. 拍点量化：从音符时长估十六分音符网格，时长/休止全部吸附到网格，
     输出 [midi|None, 拍数] + bpm，节奏不再跟着测音误差漂移；
  7. 质量闸门：音符太少/音域过宽/时长过短一律拒收，坏谱不进缓存。

旋律来源适合人声/轻伴奏；重伴奏的歌会掺进伴奏音，属于正常损耗。
用法：python melody_extract.py <音频路径> [输出json]
stdout 最后一行为 JSON：{"ok": true, "lines": [[midi|None, 拍数]...], "bpm": ..., "total": 秒}
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

SR = 22050
WIN = 1024
HOP = 256
FP = 0.005                      # WORLD 帧长（秒），5ms
FMIN, FMAX = 120, 700           # 人声旋律区间（C3~F5），避开低频鼓点/贝斯
HARVEST_LO, HARVEST_HI = 110.0, 700.0
RMS_MIN = 0.008                 # 静音门限（归一化幅度）
CONF_MIN = 0.42                 # 自相关峰/零延迟，低于它当清音
NOTE_MIN = 0.13                 # 比这短的当噪音丢掉
MERGE_GAP = 0.08                # 同音中间断这么久以内算一个音
FOLD_GAP = 0.05                 # 相邻音符几乎无缝才算转音（真音节之间常有辅音断开）
FOLD_ORN_MAX = 0.25             # 折叠只针对「长音后挂个短装饰音」，两个都长的不折
SMOOTH_FRAMES = 17              # 自相关通道的中值滤波窗口（约 0.2s）
REST_MIN = 0.22                 # 音符间空隙超过它才插休止（0.12 会把连唱切碎成休止）
MAX_SYL = 14                    # 一句最多唱几个字
MAX_LINES = 16
MAX_SECONDS = 55                # 整首时长上限（QQ 语音条 60s 以内还能直接发语音）
WINDOW_SECONDS = 48             # 从整首歌里挑这么长一段（人声最密的地方）
TARGET_CENTER = 67              # 移调后旋律中位数落在 G4 附近
RANGE = (54, 76)                # 允许的 midi 区间（约 185~660Hz，TTS 变调比例不越界）
MAX_RANGE_SPAN = 18             # 音域跨度超过它 = 大概率掺了伴奏/和声，拒收
MIN_NOTES = 8                   # 音符太少不值得唱

FFMPEG_CAND = os.path.expandvars(
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"
)


def ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if p:
        return p
    return FFMPEG_CAND if os.path.exists(FFMPEG_CAND) else ""


def decode(src: str, dst: str) -> None:
    subprocess.run(
        [ffmpeg(), "-y", "-v", "quiet", "-i", src, "-ac", "1", "-ar", str(SR), "-f", "wav", dst],
        capture_output=True, text=True, timeout=120, check=True,
    )


def read_wav(path: str) -> np.ndarray:
    from pydub import AudioSegment

    seg = AudioSegment.from_file(path, format="wav")
    y = np.array(seg.get_array_of_samples(), dtype=np.float64)
    y /= float(1 << (8 * seg.sample_width - 1))
    if seg.channels > 1:
        y = y[:: seg.channels]
    return y


def track_f0(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """快速通道：逐帧 midi（0=清音）+ 置信度。只用于全曲挑窗口和无 WORLD 时兜底。"""
    if len(y) < WIN:
        return np.zeros(0), np.zeros(0)
    frames = np.lib.stride_tricks.sliding_window_view(y, WIN)[::HOP]
    frames = frames * np.hanning(WIN)
    spec = np.fft.rfft(frames, n=2 * WIN, axis=1)
    power = spec.real ** 2 + spec.imag ** 2
    ac = np.fft.irfft(power, axis=1)[:, :WIN]
    rms = np.sqrt(np.mean(frames ** 2, axis=1))

    lag_lo, lag_hi = int(SR / FMAX), min(WIN - 1, int(SR / FMIN))
    seg = ac[:, lag_lo:lag_hi]
    peak = seg.max(axis=1)
    lag = seg.argmax(axis=1) + lag_lo
    conf = np.where(ac[:, 0] > 0, peak / np.maximum(ac[:, 0], 1e-9), 0.0)
    voiced = (rms > RMS_MIN) & (conf >= CONF_MIN)
    f0 = np.where(voiced, SR / lag.astype(np.float64), 0.0)

    midi = np.zeros(len(frames), dtype=np.float64)
    m = np.where(f0 > 0, 69 + 12 * np.log2(np.maximum(f0, 1e-6) / 440.0), 0.0)
    midi[:] = m

    # 八度纠正：按前几帧的走向就近折八度（旋律跨八度时不能拿全曲中位数一刀切）
    prev = 0.0
    recent: list[float] = []
    for i in range(len(midi)):
        if midi[i] <= 0:
            continue
        m = midi[i]
        if prev > 0 and abs(m - prev) >= 9:
            m = min(
                (m + 12 * k for k in (-2, -1, 1, 2)),
                key=lambda x: abs(x - prev),
            )
        midi[i] = m
        recent.append(m)
        if len(recent) > 5:
            recent.pop(0)
        prev = float(np.median(recent))
    # 大窗中值滤波，压掉颤音/滑音和单帧跳变
    sm = midi.copy()
    half = SMOOTH_FRAMES // 2
    for i in range(len(sm)):
        if sm[i] <= 0:
            continue
        a = midi[max(0, i - half): i + half + 1]
        near = [x for x in a if x > 0]
        if near:
            sm[i] = float(np.median(near))
    return sm, conf


def pick_window(midi: np.ndarray) -> tuple[int, int]:
    """在整首歌里挑人声最连续的一段（WINDOW_SECONDS 秒），避开前奏/间奏。"""
    n = len(midi)
    frames = int(WINDOW_SECONDS * SR / HOP)
    if n <= frames:
        return 0, n
    voiced = (midi > 0).astype(np.float64)
    cum = np.cumsum(np.concatenate(([0.0], voiced)))
    step = max(1, int(2 * SR / HOP))
    best_i, best_score = 0, -1.0
    for i in range(0, n - frames + 1, step):
        score = float(cum[i + frames] - cum[i])
        if score > best_score:
            best_i, best_score = i, score
    return best_i, best_i + frames


def harvest_midi(y: np.ndarray) -> np.ndarray:
    """WORLD harvest 测音高 + Viterbi 全局平滑，返回逐帧 midi（0=清音）。

    Viterbi 状态是「原始音高 ± 0/12/24 半音」的取整候选：
    - 观测代价让解贴回测到的音高，折八度要额外付代价（八度错判的根源）；
    - 转移代价惩罚大跳，单帧乱跳和伴奏冒出来的怪音被整段拉平；
    - 清音帧只有一个状态 0，进出清音收固定代价，避免逐帧抖。
    """
    import pyworld as pw

    f0, _tt = pw.harvest(y, SR, f0_floor=HARVEST_LO, f0_ceil=HARVEST_HI,
                         frame_period=FP * 1000.0)
    n = len(f0)
    if n == 0:
        return np.zeros(0)
    # harvest 在静音段也会幻觉出音高，用帧能量把关
    flen = max(1, int(FP * SR))
    nn = min(n, len(y) // flen)
    rms = np.zeros(n)
    if nn > 0:
        rms[:nn] = np.sqrt(np.mean(y[: nn * flen].reshape(nn, flen) ** 2, axis=1))
        if n > nn:
            rms[nn:] = rms[nn - 1]
    voiced = (f0 > 0) & (rms > RMS_MIN)

    raw = np.where(f0 > 0, 69 + 12 * np.log2(np.maximum(f0, 1e-6) / 440.0), 0.0)

    cand: list[list[tuple[int, float]]] = []
    for i in range(n):
        if not voiced[i]:
            cand.append([(0, 0.0)])
            continue
        m = float(raw[i])
        opts: list[tuple[int, float]] = []
        for k in (0, -12, 12, -24, 24):
            v = m + k
            if RANGE[0] - 3 <= v <= RANGE[1] + 3:
                opts.append((int(round(v)), abs(v - m) + (6.0 if k else 0.0)))
        cand.append(opts if opts else [(0, 0.0)])

    INF = float("inf")
    cost = [c[1] for c in cand[0]]
    back: list[list[int]] = [[-1] * len(cand[0])]
    for i in range(1, n):
        prev = cand[i - 1]
        new_cost: list[float] = []
        b: list[int] = []
        for j, (m, obs) in enumerate(cand[i]):
            best, bi = INF, -1
            for k, (pm, _po) in enumerate(prev):
                if pm == 0 and m == 0:
                    tr = 0.0
                elif pm == 0 or m == 0:
                    tr = 2.5
                else:
                    d = abs(m - pm)
                    tr = d * 0.7 + (7.0 if d >= 12 else 0.0)
                c = cost[k] + tr
                if c < best:
                    best, bi = c, k
            new_cost.append(best + obs)
            b.append(bi)
        cost = new_cost
        back.append(b)

    out = np.zeros(n)
    k = int(np.argmin(cost))
    for i in range(n - 1, -1, -1):
        out[i] = cand[i][k][0]
        k = back[i][k]

    # 元音里的短促清音空洞（<0.1s）用前后中位数补上，别把一个字切成两个音
    i = 0
    while i < n:
        if out[i] > 0:
            i += 1
            continue
        j = i
        while j < n and out[j] <= 0:
            j += 1
        if 0 < i and j < n and (j - i) <= int(0.1 / FP):
            near = [out[i - 1], out[j]]
            fill = float(np.median(near))
            out[i:j] = round(fill)
        i = j
    return out


def frames_to_notes(midi: np.ndarray, frame_sec: float) -> list[dict]:
    """把逐帧 midi 切成音符：{"midi", "start", "dur"}，单位秒。"""
    notes: list[dict] = []
    cur: dict | None = None
    silence = 0
    for i, m in enumerate(midi):
        t = i * frame_sec
        if m <= 0:
            silence += 1
            if cur is not None and silence * frame_sec >= 0.03:
                notes.append(cur)
                cur = None
            continue
        silence = 0
        if cur is not None:
            fm = float(np.median(cur["vals"][-5:]))
            if abs(m - fm) <= 0.8 and t - cur["end"] <= 0.05:
                cur["vals"].append(float(m))
                cur["end"] = t + frame_sec
                continue
        if cur is not None:
            notes.append(cur)
        cur = {"vals": [float(m)], "start": t, "end": t + frame_sec}
    if cur is not None:
        notes.append(cur)

    out: list[dict] = []
    for n in notes:
        dur = n["end"] - n["start"]
        if dur < NOTE_MIN:
            continue
        out.append({
            "midi": float(np.median(n["vals"])),
            "start": float(n["start"]),
            "dur": float(dur),
        })
    # 同音相邻合并（中断 MERGE_GAP 以内的当连音）
    merged: list[dict] = []
    for n in out:
        if (
            merged
            and abs(n["midi"] - merged[-1]["midi"]) <= 0.8
            and n["start"] - (merged[-1]["start"] + merged[-1]["dur"]) <= MERGE_GAP
        ):
            last = merged[-1]
            end = max(last["start"] + last["dur"], n["start"] + n["dur"])
            last["dur"] = end - last["start"]
            last["midi"] = float(np.median([last["midi"], n["midi"]]))
        else:
            merged.append(dict(n))
    return merged


def fold_melisma(notes: list[dict]) -> list[dict]:
    """转音折叠：几乎无缝相接（间隔 < FOLD_GAP）且其中一个是短音
    （< FOLD_ORN_MAX）时，视为同一个音节上的装饰音/快速音群，折成一个音
    （音高取时长最长的）。一段连续八分音符的过腔会被折成一字，而两个
    都唱满的真音节不会被误并。"""
    out: list[dict] = []
    for n in notes:
        if (
            out
            and n["start"] - (out[-1]["start"] + out[-1]["dur"]) < FOLD_GAP
            and min(out[-1]["dur"], n["dur"]) < FOLD_ORN_MAX
        ):
            last = out[-1]
            if n["dur"] > last["dur"]:
                last["midi"] = n["midi"]
            end = max(last["start"] + last["dur"], n["start"] + n["dur"])
            last["dur"] = end - last["start"]
        else:
            out.append(dict(n))
    return out


def transpose(notes: list[dict]) -> list[dict]:
    """整条旋律移调，让中位数落在 TARGET_CENTER；个别越界音按八度折回。"""
    if not notes:
        return notes
    med = float(np.median([n["midi"] for n in notes]))
    shift = round(TARGET_CENTER - med)  # 必须是整数：小数位移会让相邻音高被四舍五入搞乱
    lo, hi = RANGE
    for n in notes:
        m = n["midi"] + shift
        while m > hi:
            m -= 12
        while m < lo:
            m += 12
        n["midi"] = round(m)
    return notes


def quantize(notes: list[dict]) -> float:
    """从音符时长估十六分音符网格 g，把时值吸附到 g 的整数倍（写回 n["beats"]，
    单位=拍，1 拍=2g 秒），返回拍长秒数。

    网格限制在 bpm 70~145（拍长 0.41~0.86s）：再快就变成把正常时值切碎，
    没有节奏意义。误差用「偏离网格的格数」平均，量纲无关。"""
    durs = [n["dur"] for n in notes]
    scored: list[tuple[float, float]] = []
    for g in np.arange(0.429, 0.207, -0.003):
        err = 0.0
        for d in durs:
            u = d / g
            err += abs(u - max(1.0, round(u)))
        err /= max(1, len(durs))
        scored.append((err, float(g)))
    min_err = min(e for e, _ in scored)
    # 误差接近的网格视为同样好，取速度最贴近 96 的那个：
    # 纯拟合会偏向把正常时值切碎的快网格，节奏听着就「赶」
    best_g = min(
        (g for e, g in scored if e <= min_err * 1.15 + 1e-9),
        key=lambda g: abs(60.0 / (2.0 * g) - 96.0),
    )
    for n in notes:
        n["beats"] = max(0.5, round(n["dur"] / best_g) * 0.5)
    return 2.0 * best_g


def split_lines(notes: list[dict], beat_sec: float) -> list[list]:
    """按停顿/句长分句，每句 = [[midi|None, 拍数], ...]；总时长封顶。

    断句的停顿按拍长走（接近一拍没唱才算换气），并要求句内至少 4 字，
    免得把正常歌词切成「3 字一句」的碎块；断句后句首不补休止。"""
    lines: list[list] = []
    cur: list[list] = []
    syl = 0
    total_sec = 0.0
    prev_end = 0.0
    for n in notes:
        gap = n["start"] - prev_end if prev_end else 0.0
        if cur and ((gap >= beat_sec * 0.9 and syl >= 4) or syl >= MAX_SYL):
            lines.append(cur)
            cur, syl = [], 0
            prev_end = n["start"]
            gap = 0.0
            if len(lines) >= MAX_LINES:
                break
        if gap >= REST_MIN and cur:
            rest_b = min(2.0, max(0.5, round(gap / beat_sec * 2) * 0.5))
            cur.append([None, rest_b])
            total_sec += rest_b * beat_sec
        cur.append([int(n["midi"]), n["beats"]])
        syl += 1
        total_sec += n["beats"] * beat_sec
        prev_end = n["start"] + n["dur"]
        if total_sec >= MAX_SECONDS:
            break
    if cur and len(lines) < MAX_LINES:
        lines.append(cur)
    # 尾巴上孤零零一两个字的残句并进上一句（上一句装得下的话）
    if len(lines) >= 2:
        tail = lines[-1]
        tail_syl = sum(1 for m, _ in tail if m is not None)
        prev_syl = sum(1 for m, _ in lines[-2] if m is not None)
        if tail_syl < 2 and prev_syl + tail_syl <= MAX_SYL:
            lines[-2].extend(lines.pop())
    # 相邻短句合并唱满：像平凡之路这种旋律，分句器会切出一堆 3~4 字的碎句，
    # 每句开头还各被 320ms 行间隔顶一次，整首听着又空又断。合并成 6~12 字的
    # 长句（中间只补半拍换气），一行唱满，稀疏问题从根上解决。
    def _syl(ln):
        return sum(1 for m, _ in ln if m is not None)
    merged: list[list] = []
    for ln in lines:
        if merged and _syl(merged[-1]) + _syl(ln) <= MAX_SYL:
            merged[-1].append([None, 0.5])
            merged[-1].extend(ln)
        else:
            merged.append(list(ln))
    lines = [ln for ln in merged if _syl(ln) >= 1]
    # 延音折叠：扒出来的稀疏旋律常是「0.5 拍字 + 1.5 拍休止」，直接唱就变成
    # 一个字拖一秒静音。真人跨过器乐空隙靠的是把前一个音拖长，不是停下——
    # 所以把休止并进前一个音，只在 >=1.5 拍的大换气处留 0.25 拍呼吸。
    dense: list[list] = []
    for ln in lines:
        new: list[list] = []
        for m, b in ln:
            if m is None:
                if new and new[-1][0] is not None:
                    add = b if b <= 1.0 else 0.75
                    new[-1][1] = round(new[-1][1] + add, 2)
                    if b >= 1.5:
                        new.append([None, 0.25])
                continue
            new.append([m, b])
        dense.append(new)
    lines = [ln for ln in dense if _syl(ln) >= 1]
    # 行尾休止直接去掉（渲染端本来就有 320ms 行间隔）
    for ln in lines:
        while ln and ln[-1][0] is None:
            ln.pop()
    return lines


def cover_window(notes: list[dict], span: int) -> list[dict]:
    """挑一个能覆盖最多音符的 span 半音音域窗，掐掉窗口外的音。

    流行歌的窗口常混进主歌+副歌两段音区（双峰分布），max-min 跨度必然爆表；
    取「多数音符所在的那段」来唱，比整首毙掉合理。留不住 60% 的音符时
    说明分布真的散，原样交给闸门去拒。"""
    if len(notes) < 2:
        return notes
    ms = sorted(n["midi"] for n in notes)
    best_cnt, best_lo = -1, ms[0]
    for lo in ms:
        cnt = sum(1 for m in ms if lo <= m <= lo + span)
        if cnt > best_cnt:
            best_cnt, best_lo = cnt, lo
    kept = [n for n in notes if best_lo <= n["midi"] <= best_lo + span]
    return kept if len(kept) >= max(MIN_NOTES, int(len(notes) * 0.6)) else notes


def quality_check(notes: list[dict], lines: list[list], syllables: list[int],
                  total_sec: float) -> str:
    """坏谱不进缓存：掺了伴奏/和声的谱往往音域炸裂或碎成一堆短音。"""
    if len(notes) < MIN_NOTES:
        return f"音符太少（{len(notes)}），大概率没扒到人声"
    span = int(max(n["midi"] for n in notes) - min(n["midi"] for n in notes))
    if span > MAX_RANGE_SPAN:
        return f"音域跨度 {span} 半音，掺了伴奏/和声的可能性太高"
    if not lines or sum(syllables) < MIN_NOTES:
        return "分句后没剩下可唱的音节"
    if total_sec < 5:
        return f"旋律太短（{total_sec:.0f}s）"
    return ""


def extract(path: str) -> dict:
    tmp = tempfile.mkdtemp(prefix="melody_")
    try:
        wav = os.path.join(tmp, "in.wav")
        decode(path, wav)
        y = read_wav(wav)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 第一遍：快速自相关只为挑「人声最连续」的窗口，精度不重要
    midi_fast, _ = track_f0(y)
    a, b = pick_window(midi_fast)
    yw = y[a * HOP: min(len(y), b * HOP)]
    if len(yw) < WIN * 4:
        return {"ok": False, "error": "音频太短或几乎全是静音"}

    # 第二遍：窗口内用 WORLD + Viterbi；没有 pyworld 就退回自相关通道
    frame_sec = FP
    try:
        midi = harvest_midi(yw)
    except Exception as e:  # noqa: BLE001
        print(f"[melody] WORLD 不可用（{type(e).__name__}），退回自相关", file=sys.stderr)
        midi, _ = track_f0(yw)
        frame_sec = HOP / SR

    notes = fold_melisma(frames_to_notes(midi, frame_sec))
    notes = transpose(notes)
    notes = cover_window(notes, MAX_RANGE_SPAN)
    beat_sec = quantize(notes)
    lines = split_lines(notes, beat_sec)
    syllables = [sum(1 for m, _ in ln if m is not None) for ln in lines]
    total_sec = sum(d for _, d in (it for ln in lines for it in ln)) * beat_sec
    bad = quality_check(notes, lines, syllables, total_sec)
    if bad:
        return {"ok": False, "error": bad}
    return {
        "ok": True,
        "lines": lines,
        "syllables": syllables,
        "bpm": round(60.0 / beat_sec, 1),
        "beat": round(beat_sec, 3),
        "total": round(total_sec + 0.32 * len(lines), 2),
        "range": [int(min(n["midi"] for n in notes)), int(max(n["midi"] for n in notes))],
        "notes": len(notes),
    }


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "缺少音频路径"}))
        return
    try:
        res = extract(sys.argv[1])
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return
    out = sys.argv[2] if len(sys.argv) > 2 else ""
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
    # stdout 最后一行为结果（主进程解析）
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
