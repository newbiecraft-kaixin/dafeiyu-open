"""sing_render 拆分：WORLD 声码器整句重唱。

从 sing_render.py 按职责拆出；sing_render.py 保留为门面与脚本入口。
把整句拆成「基频 / 频谱包络 / 非周期成分」三条流、按输出时间轴逐帧
重新取样后用 WORLD 合成（synth_line_world）。pyworld 不可用时回退
sing_psola.synth_line。
"""
from __future__ import annotations

import os

import numpy as np

from sing_psola import synth_line
from sing_util import SR, _level_even, midi_hz


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
