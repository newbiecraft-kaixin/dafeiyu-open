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

按职责拆分为子模块，本文件保留为门面（转发全部原有模块级名字）与脚本入口：
  sing_util.py    音频基础工具（ffmpeg / wav 读写 / 响度均衡 / 片段拷贝）
  sing_tts.py     整句语音合成（edge-tts / CosyVoice / SAPI 兜底）
  sing_align.py   歌词-音频对齐（有声区间 / 能量谷切字 / ASR 词级时间戳）
  sing_psola.py   TD-PSOLA 颗粒合成（基频跟踪 / epoch / 重摆颗粒）
  sing_world.py   WORLD 声码器整句重唱
渲染主流程（render）与命令行入口（main）留在本文件。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

import numpy as np

# ── 门面转发：原有模块级名字原样从子模块导出 ──
from sing_align import _frame_energy, asr_char_spans, char_spans, voiced_range  # noqa: F401
from sing_psola import find_epochs, frame_f0, synth_line, world_available  # noqa: F401
from sing_tts import COSY_URL, sapi_line, tts_line, tts_line_cosy_sync  # noqa: F401
from sing_util import (  # noqa: F401
    FFMPEG_CAND,
    HOP,
    SR,
    WIN,
    _HAN_RE,
    _copy_span,
    _level_even,
    arr_to_seg,
    ffmpeg,
    midi_hz,
    read_wav,
    run,
    to_wav,
)
from sing_world import synth_line_world  # noqa: F401


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
