"""整首 AI 翻唱：原曲分轨 → 按干声找高潮段 → 用机器人的说话音色换声 → 与原伴奏混音。

音色的来源只有一个：机器人平时说话用的那条嗓子
（voice_engine 现役参考 ref.wav = cosyvoice_ref/ref_loli_xiaoyi.wav，
 见下方 REF_WAV 里的长参考版本），再用这条嗓子微调过的 Seed-VC 权重。
原唱只提供旋律/歌词/节奏（source），**不是**去模仿原唱歌手的音色；
换出来的应该是「肥鱼用自己说话的声音在唱」。

用法：python cover_render.py <job.json>
job 字段：
  mode   cover = 原曲翻唱（整首分轨，但只转换高潮段）；post = 把已有的干声换成说话音色
  src    cover 时是原曲文件；post 时是干声 mp3/wav
  out    输出的 mp3 路径
  semi   半音偏移，默认 0（只做额外音高微调，音域对齐在 f0 参数里）
  steps  Seed-VC 的扩散步数，默认 40
  cfg_rate Seed-VC 的推理 cfg，默认 1.0
  target 自定义参考音色 wav（默认用下面的说话参考）
stdout 最后一行是 JSON 结果。
"""
import difflib
import glob
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave

import numpy as np
from pydub import AudioSegment

# 插件会被复制进 AstrBot，不能 import 仓库的 shared，这里自己按目录找：
# 插件目录的所有上级（含 C:\Program Files\FlatFish 安装根）→ ~/dafeiyu → 用户目录。
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_HOME = os.path.expanduser("~")


def _ancestors(path: str) -> tuple[str, ...]:
    """从插件目录一路往上，直到盘符根；安装根（如 C:\\Program Files\\FlatFish）必在里面。"""
    out: list[str] = []
    cur = path
    while True:
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        out.append(parent)
        cur = parent
    return tuple(out)


_ROOTS = tuple(
    r for r in dict.fromkeys(
        _ancestors(PLUGIN_DIR)
        + (os.environ.get("FLATFISH_HOME", ""),
           os.path.join(_HOME, "dafeiyu"),
           _HOME)
    ) if r
)


def _find(*names: str) -> str:
    for root in _ROOTS:
        for name in names:
            path = os.path.join(root, *name.split("/"))
            if os.path.exists(path):
                return path
    return ""


SEEDVC_REPO = _find("audio/seed-vc/seed-vc-main", "seed-vc/seed-vc-main", "seed-vc")
# 换声唯一引擎：Seed-VC（说话嗓子微调权重，音色相似度优先）。
# SoulX-Singer 已下线：8-12k 高频虽好，但音色相似度明显不如微调后的 Seed-VC。
COSY_PY = (
    _find("audio/cosyvoice_env/Scripts/python.exe",
          "cosyvoice_env/Scripts/python.exe")
    or os.environ.get("COSYVOICE_PY", "")
    or sys.executable  # 独立环境缺失时退回当前解释器（依赖装在运行时里）
)
SEEDVC_PY = _find("audio/seedvc_env/Scripts/python.exe",
                  "seedvc_env/Scripts/python.exe") or COSY_PY
REF_DIR = _find("audio/cosyvoice_ref", "cosyvoice_ref") or os.path.join(
    _HOME, "dafeiyu", "cosyvoice_ref")
# 音色 = 机器人说话的那条嗓子（和 TTS 现役 ref.wav 同一段录音的长版本）。
# 长版本 25 秒、满带宽，Seed-VC 当扩散提示时不会偏闷；短参考只有 8.7 秒 16kHz，
# 生成结果频谱重心 1597Hz，听感「老、偏男」。优先长参考，没有再退回 TTS 现役 ref.wav。
REF_WAV = os.path.join(REF_DIR, "ref_loli_xiaoyi_long.wav")
if not os.path.exists(REF_WAV):
    REF_WAV = os.path.join(REF_DIR, "ref_loli_xiaoyi.wav")
if not os.path.exists(REF_WAV):
    REF_WAV = os.path.join(REF_DIR, "ref.wav")

# 当前声线档案（/声线 切换后，唱歌也跟着换参考音频）
PROFILE_ROOT = os.path.join(REF_DIR, "profiles")
ACTIVE_FILE = os.path.join(PROFILE_ROOT, ".active")


def _active_name() -> str:
    try:
        with open(ACTIVE_FILE, encoding="utf-8-sig") as f:
            return f.read().strip()
    except OSError:
        return ""


def _active_ref(long: bool = True) -> str:
    try:
        with open(ACTIVE_FILE, encoding="utf-8-sig") as f:
            name = f.read().strip()
    except OSError:
        return ""
    d = os.path.join(PROFILE_ROOT, name)
    if not os.path.exists(os.path.join(d, "ref.wav")):
        return ""
    if long and os.path.exists(os.path.join(d, "long.wav")):
        return os.path.join(d, "long.wav")
    return os.path.join(d, "ref.wav")


def _ckpt_step(path: str) -> int:
    m = re.search(r"ft_model_(\d+)\.pth$", os.path.basename(path))
    return int(m.group(1)) if m else -1


def _active_ckpt() -> str:
    """当前声线的 Seed-VC 权重：优先 ckpt.txt，其次档案目录里的 ft_model_*.pth。

    没有这一步时，大肥鱼/萝莉会静默退回 xiaoyi_ft 的小忆权重，唱出来串音色。
    """
    try:
        with open(ACTIVE_FILE, encoding="utf-8-sig") as f:
            name = f.read().strip()
    except OSError:
        return ""
    path = os.path.join(PROFILE_ROOT, name, "ckpt.txt")
    try:
        with open(path, encoding="utf-8-sig") as f:
            ckpt = f.read().strip()
    except OSError:
        ckpt = ""
    # ckpt.txt 里存的是相对安装根的路径：进程 cwd 不同 os.path.exists 就失败，
    # 会静默退到别的权重（缓存键那边又是另一套挑法），所以相对路径按文件名回档案目录找
    if ckpt:
        for cand in (os.path.join(PROFILE_ROOT, name, os.path.basename(ckpt)),
                     ckpt):
            if os.path.isfile(cand):
                return cand
    cands = glob.glob(os.path.join(PROFILE_ROOT, name, "ft_model*.pth"))
    # 步数最大的那个（ft_model.pth 没有步数，排最后）
    cands.sort(key=_ckpt_step, reverse=True)
    return cands[0] if cands else ""

# 用这条说话嗓子微调过的 Seed-VC 权重（不是原唱、也不是唱歌数据训的）
FT_DIR = os.path.join(SEEDVC_REPO, "runs", "xiaoyi_ft")
FT_CKPT = os.path.join(FT_DIR, "ft_model.pth")
# 唱歌优先：渲染前立旗并停续训，渲染完撤旗（见 train_supervisor.py）
TRAIN_PAUSE_FLAG = os.path.join(SEEDVC_REPO, "runs", ".pause_training")
_FT_CFG = sorted(glob.glob(os.path.join(FT_DIR, "*.yml")))

# 分轨结果缓存：同一首原曲 3 天内再点，直接复用 vocals/no_vocals，省整首 demucs
STEM_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "plugin_data", "stem_cache")

DEFAULT_STEPS = 40      # 150→40 步：速度约快 3 倍，声纹差异听感很小
# cfg 1.0：1.3 声纹只高 0.013（0.751 vs 0.738），但咬字被拽糊（"失掉"听成"失丢"），
# 所以清晰度优先，用 1.0
DEFAULT_CFG_RATE = 1.0
# 自动把原唱音高对到说话嗓子的音域：干声中位基频 217Hz → 311Hz
# （说话参考 327Hz）。全量对齐咬字容易糊，只对齐 70%（约 3 个半音）
DEFAULT_AUTO_F0 = True
DEFAULT_F0_MIX = 0.7
# 抬升上限：原唱音域本来就高（女声歌）时，把这个说话嗓子硬抬到原唱音高
# 会变成「喊」。最多抬 3 个半音，降调方向不限制。
DEFAULT_F0_MAX_UP = 3.0
# 歌词核验：ASR 对「唱」本身就不准（原唱干声都能转成乱码），所以拿
# 原唱 ASR 当基准时，80% 这种阈值会每次都误触发重唱、白等 5 分钟。
# 这里只在几乎崩掉时才重唱，平时只记录文本和相似度供诊断。
VERIFY_SIM_MIN = 0.35
# 翻唱出来的人声高频基本被砍掉（参考音频 16k），同等 RMS 下听着比原唱轻，
# 混音时给人声补 2.5dB，免得被原版伴奏盖住。
VOCAL_BOOST = 1.33
# 咬字（擦音/爆破音）都在 4k 以上。实测换声干声的 4-8k 比原唱少约 2dB、
# 8-16k 少约 10dB（微调权重是 16k 语音数据训的），混进伴奏后字就糊了。
# 按原唱这一段的长期平均频谱把 4k 以上补回来：只补形状不补电平。
# 封顶从 +10dB 收到 +5dB：实测 +10dB（叠搁架后 12.5dB）会把峰值推到 1.1 以上，
# 后面 _save_wav 硬削波，听感就是爆破音/咔哒声。
CLARITY_LOW_HZ = 3500
CLARITY_MAX_GAIN_DB = 5.0
CLARITY_MAX_CUT_DB = -2.0
CLARITY_SHELF_DB = 1.0
# 9k 以上不再补：模型那一段基本只有噪声底，抬上去只会变成嘶嘶声和齿音；
# 到 13.5k 渐变归零
CLARITY_TAPER_HZ = 9000


def log(msg: str) -> None:
    print(f"[cover] {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], cwd: str | None = None, timeout: int = 900,
        env: dict | None = None) -> None:
    log("run: " + " ".join(os.path.basename(c) for c in cmd[:4]))
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=timeout, env=env)
    if p.returncode != 0:
        raise RuntimeError(
            f"{os.path.basename(cmd[0])} 退出码 {p.returncode}: "
            f"{(p.stderr or '')[-800:]}")


def unload_ollama() -> None:
    """腾显存：把 Ollama 里加载着的模型卸掉，跑完它自己会按需重载。

    走本地 HTTP，不调 `ollama ps`：守护进程没起时那条命令会拉起 Ollama
    桌面程序，子进程继承管道不退出，把翻唱卡死在第一步。
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps",
                                    timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return
    for m in data.get("models", []):
        name = m.get("name") or m.get("model")
        if not name:
            continue
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/generate",
                data=json.dumps({"model": name, "keep_alive": 0}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            log(f"unloaded ollama {name}")
        except Exception as e:  # noqa: BLE001
            log(f"unload {name} skipped: {type(e).__name__}")


def _gpu_free_mb() -> int:
    try:
        p = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        return int((p.stdout or "0").strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return 0


def pause_training() -> None:
    """唱歌优先：立暂停旗并杀掉正在跑的续训，等显存腾出来再渲染。

    续训由 seed-vc-main/train_supervisor.py 监督，撤旗后它会从最新检查点续上。
    """
    try:
        os.makedirs(os.path.dirname(TRAIN_PAUSE_FLAG), exist_ok=True)
        with open(TRAIN_PAUSE_FLAG, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        cmd = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
               "Where-Object { $_.CommandLine -match 'train\\.py' } | "
               "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
        p = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=40)
        killed = len([x for x in (p.stdout or "").splitlines() if x.strip()])
        if killed:
            log(f"续训已让路（停了 {killed} 个 train.py）")
        for _ in range(30):
            if _gpu_free_mb() >= 4500:
                return
            time.sleep(1)
        log(f"续训停了但显存仍紧：free {_gpu_free_mb()}MB，直接试渲染")
    except Exception as e:  # noqa: BLE001
        log(f"暂停续训失败（照常渲染）: {type(e).__name__}: {e}")


def resume_training() -> None:
    try:
        os.remove(TRAIN_PAUSE_FLAG)
        log("撤续训暂停旗，监督进程会继续训练")
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"撤续训暂停旗失败: {type(e).__name__}")


def _stem_cache_key(src: str) -> str:
    st = os.stat(src)
    raw = f"{os.path.abspath(src)}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _vocals_rms_peak(path: str, windows: int = 5, span: float = 10.0) -> float:
    """人声存在性：全曲均匀取几窗的 RMS，取最大值。

    假音源（水印音轨/纯伴奏）demucs 分出来的 vocals 全程接近静音；
    真歌就算前奏没唱，高潮段也能到 0.05+。取峰值避免被纯前奏误伤。
    """
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        frames = w.getnframes()
        total = frames / max(sr, 1)
        step = max((total - span) / max(windows, 1), 0.0)
        peak = 0.0
        for i in range(windows):
            w.setpos(min(int((i * step + step / 2) * sr), max(frames - 1, 0)))
            y = np.frombuffer(w.readframes(int(span * sr)), dtype=np.int16)
            y = y.astype(np.float32) / 32768.0
            peak = max(peak, float(np.sqrt(np.mean(y ** 2))) if y.size else 0.0)
    return peak


VOCALS_RMS_MIN = 0.012  # 真歌高潮段远高于它（实测好歌 0.05+），假音源 0.004


def _source_rolloff(path: str, span: float = 30.0) -> float:
    """音源高频滚降点：中段 span 秒里 99.9% 能量所在的频率（Hz）。

    320k 全频源在 14k 上下，低码率/试听货会被切到 11k 左右，
    整首发闷，Seed-VC 拿糊干声转出来咬字也跟着糊。实测：
    素颜（好）14.0k，坏女孩（咪咕，难听）10.9k。
    """
    try:
        import soundfile as sf  # seedvc_env 里有；缺了就跳过闸门不拦人
    except ImportError:
        return 0.0
    try:
        data, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001 - 解不开就不拦，交给后面的 RMS 闸
        return 0.0
    mono = data.mean(axis=1)
    n = min(int(span * sr), mono.size)
    if n < sr * 5:
        return 0.0
    seg = mono[max(0, (mono.size - n) // 2):][:n]
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
    power = spec ** 2
    freqs = np.fft.rfftfreq(seg.size, 1.0 / sr)
    cum = np.cumsum(power) / max(power.sum(), 1e-20)
    return float(freqs[min(np.searchsorted(cum, 0.999), freqs.size - 1)])


SOURCE_ROLLOFF_MIN = 13500  # 实测：坏女孩(难听)源 13.4k，素颜(好听)源 15k+；低于 13.5k 拒绝


def demucs_split(src: str, workdir: str, allow_low: bool = False) -> tuple[str, str]:
    cache = os.path.join(STEM_CACHE_DIR, _stem_cache_key(src))
    voc_c = os.path.join(cache, "vocals.wav")
    acc_c = os.path.join(cache, "no_vocals.wav")
    if os.path.exists(voc_c) and os.path.exists(acc_c) \
            and os.path.getsize(voc_c) > 10000 and os.path.getsize(acc_c) > 10000:
        log("分轨缓存命中，跳过 demucs")
        return voc_c, acc_c
    # 音源码率闸门：分轨前先看高频滚降，低于 13.5k 的源唱出来必闷，
    # 别浪费两分钟 demucs + 五分钟转换。源不删（普通听歌还能用），只拒绝唱。
    rolloff = _source_rolloff(src)
    log(f"音源高频滚降 {rolloff / 1000:.1f}kHz（下限 {SOURCE_ROLLOFF_MIN / 1000:.1f}k）")
    if 0.0 < rolloff < SOURCE_ROLLOFF_MIN:
        if allow_low:
            # 扒歌时已经全网挑过最好的源，这条就是不达标的最佳可用：硬着头皮唱
            log("音源码率不达标，但已是能扒到的最好源，继续唱（成品会偏闷）")
        else:
            raise RuntimeError(
                f"这个音源是低码率货（高频只到 {rolloff / 1000:.1f}kHz，好源在 14k+），"
                "唱出来会又闷又糊，换个源或换首再唱")
    # demucs 固定用 seedvc_env（随翻唱环境走）：AstrBot 运行时解释器的 torch
    # 会被 AstrBot 升级动掉，分轨这种重依赖不跟着它漂。两边实测分轨结果一致。
    run([SEEDVC_PY, "-m", "demucs", "-n", "htdemucs", "--two-stems", "vocals",
         "-o", workdir, src], timeout=900)
    stem = os.path.splitext(os.path.basename(src))[0]
    base = os.path.join(workdir, "htdemucs", stem)
    voc = os.path.join(base, "vocals.wav")
    acc = os.path.join(base, "no_vocals.wav")
    if not (os.path.exists(voc) and os.path.exists(acc)):
        raise RuntimeError(f"demucs 没出分轨: {base}")
    # 人声存在性闸门：扒歌验货的 ASR 抽样窗口可能撞不上水印段放过假音源，
    # 这里在分轨后直接量 vocals 的 RMS——水印人声会被归进伴奏轨，vocals 全程静音。
    peak = _vocals_rms_peak(voc)
    if peak < VOCALS_RMS_MIN:
        try:
            os.remove(src)
        except OSError:
            pass
        try:
            shutil.rmtree(cache, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(
            f"扒到的源没有人声（vocals 峰值 RMS {peak:.4f}），是假音源/纯伴奏，"
            "已删掉缓存，换一首试试")
    log(f"vocals 峰值 RMS {peak:.4f}，人声正常")
    try:
        os.makedirs(cache, exist_ok=True)
        shutil.copy2(voc, voc_c)
        shutil.copy2(acc, acc_c)
    except OSError as e:
        log(f"分轨缓存写入失败: {type(e).__name__}")
    return voc, acc


def seedvc(vocal: str, outdir: str, semi: int, auto_f0: bool = DEFAULT_AUTO_F0,
           steps: int = DEFAULT_STEPS, cfg_rate: float = DEFAULT_CFG_RATE,
           target: str = "", ctx_sec: float = 0.0,
           f0_mix: float = DEFAULT_F0_MIX,
           f0_max_up: float = DEFAULT_F0_MAX_UP) -> str:
    """把原唱的干声换成机器人说话的音色；旋律/歌词/节奏沿用 source。"""
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    # 上下文窗口盖住整段：Seed-VC 每 30 秒切一次，接缝处会断音/丢帧
    if ctx_sec > 0:
        os.environ["SEEDVC_CTX_SEC"] = str(int(np.ceil(ctx_sec)))
    os.environ["SEEDVC_F0_MIX"] = str(float(f0_mix))
    os.environ["SEEDVC_F0_MAX_UP"] = str(float(f0_max_up))
    ref = target if target and os.path.exists(target) else (_active_ref() or REF_WAV)
    cmd = [SEEDVC_PY, "inference.py",
           "--source", vocal, "--target", ref, "--output", outdir,
           "--f0-condition", "True", "--semi-tone-shift", str(semi),
           "--auto-f0-adjust", "True" if auto_f0 else "False",
           "--diffusion-steps", str(steps),
           "--inference-cfg-rate", str(cfg_rate),
           "--fp16", "True"]
    # 说话嗓子微调权重：有就用，没有才退回基座（基座是唱歌数据训的，容易偏原唱歌手）
    ft_ckpt = os.environ.get("COVER_FT_CKPT") or _active_ckpt()
    if not ft_ckpt and _active_name() in ("", "小忆"):
        # 小忆没有自己的 ft_model，xiaoyi_ft 就是她的权重；别的声线没权重就该走基座，
        # 套上小忆的微调权重会把音色拽过去（御姐/低音炮 唱出来像小忆 的根因）
        ft_ckpt = FT_CKPT
    if os.path.exists(ft_ckpt) and _FT_CFG:
        cmd += ["--checkpoint", ft_ckpt, "--config", _FT_CFG[0]]
    log(f"音色参考 {os.path.basename(ref)}｜微调 {os.path.basename(ft_ckpt) if os.path.exists(ft_ckpt) else '无'}"
        f"｜{steps} 步 cfg {cfg_rate}｜auto-f0 {'开' if auto_f0 else '关'}"
        f"（对齐 {f0_mix:.0%}，抬升上限 {f0_max_up:.1f} 半音）"
        f"｜窗口 {ctx_sec or 30:.0f}s｜半音 {semi}")
    run(cmd, cwd=SEEDVC_REPO, timeout=1800)
    wavs = glob.glob(os.path.join(outdir, "**", "*.wav"), recursive=True)
    if not wavs:
        raise RuntimeError(f"Seed-VC 没输出 wav: {outdir}")
    return max(wavs, key=os.path.getsize)


def samples(seg: AudioSegment) -> np.ndarray:
    """按真实位深取 [-1,1] 浮点（32-bit 样本 pydub 解码后是 int32）。"""
    if seg.sample_width == 4:
        a = np.frombuffer(seg.raw_data, dtype=np.int32).astype(np.float64)
        return a / float(1 << 31)
    a = np.array(seg.get_array_of_samples(), dtype=np.float64)
    return a / float(1 << (8 * seg.sample_width - 1))


def _energy_curve(path: str) -> np.ndarray:
    """每秒一个 RMS（8kHz 单声道，3 秒平滑），用来按「谁在唱」找高潮。"""
    seg = AudioSegment.from_file(path).set_channels(1).set_frame_rate(8000)
    a = samples(seg)
    n = len(a) // 8000
    if n == 0:
        return np.zeros(0)
    e = np.sqrt(np.mean(a[: n * 8000].reshape(n, 8000) ** 2, axis=1))
    return np.convolve(e, np.ones(3) / 3.0, mode="same")


def _snap_to_gap(e: np.ndarray, i: int, radius: int = 2) -> int:
    """把切点挪到附近能量最低的一秒（换气/句间停顿），免得把一句歌词切成两半。"""
    lo = max(0, i - radius)
    hi = min(int(e.size) - 1, i + radius)
    return lo + int(np.argmin(e[lo:hi + 1]))


def hook_window(vocal_wav: str, acc_wav: str, total: float) -> tuple[float, float]:
    """找高潮段：按干声能量挑，固定 40 秒，切点对齐句间停顿。

    以前按整体音量找，伴奏很响但没人唱的段落会被选中，发出去就像没唱；
    所以这里以干声为准，纯伴奏的歌才退回伴奏能量。
    """
    ev = _energy_curve(vocal_wav)
    if ev.size == 0:
        return 0.0, total
    ea = _energy_curve(acc_wav)
    e = ev if float(np.mean(ev)) >= 0.01 else ev * 0.35 + ea
    n = int(e.size)
    # 固定 40 秒：以前 34~50 秒随机，同一首歌每次时长都不一样，听着像「时好时坏」
    dur = min(total - 6.0, 40.0)
    win = max(8, int(dur))
    if n <= win + 8:
        return 0.0, min(total, float(n))
    cs = np.concatenate([[0.0], np.cumsum(e)])
    lo, hi = 4, n - win - 4
    if hi <= lo:
        lo, hi = 0, n - win
    scores = cs[lo + win:hi + win] - cs[lo:hi]
    best = float(np.max(scores))
    # 分数接近最好的窗口里随机挑一个：同一首歌每次点，起始位置和内容都有变化
    cands = [lo + int(i) for i, s in enumerate(scores) if s >= best * 0.9]
    start = float(random.choice(cands))
    s0 = _snap_to_gap(e, int(start), 2)
    s1 = min(s0 + win, n - 1)
    return float(s0), float(min(s1 - s0, total - s0))


def _frame_rms(seg: AudioSegment, frame_ms: int = 20) -> np.ndarray:
    step = int(seg.frame_rate * frame_ms / 1000)
    y = samples(seg.set_channels(1))
    n = y.size // step
    if n <= 0:
        return np.zeros(0)
    y = y[: n * step].reshape(n, step)
    return np.sqrt(np.mean(y ** 2, axis=1))


def _hole_count(src_wav: str, out_wav: str) -> tuple[int, float]:
    """量化断音：源在唱、输出却掉下去的连续片段（连续 2 帧以上算一处）。"""
    a = AudioSegment.from_file(src_wav).set_frame_rate(16000)
    b = AudioSegment.from_file(out_wav).set_frame_rate(16000)
    fa, fb = _frame_rms(a), _frame_rms(b)
    m = min(fa.size, fb.size)
    fa, fb = fa[:m], fb[:m]
    if m == 0:
        return 0, 0.0
    loud = fa > max(0.008, float(np.median(fa)) * 0.6)
    bad = loud & (fb < fa * 0.25)
    runs, run = 0, 0
    for i in range(m):
        if bad[i]:
            run += 1
            continue
        if run >= 2:
            runs += 1
        run = 0
    if run >= 2:
        runs += 1
    return runs, float(bad.sum()) / m


_ASR = None


def asr_text(path: str) -> str:
    """本地 faster-whisper 转写（唱歌咬字核验用）。失败返回空串，不阻断翻唱。

    先用 ffmpeg 转成 16k 单声道、再用 wave 读成 numpy 喂给模型：
    直接给路径会走 av 解码，装环境里的 av 19 与 faster-whisper 1.2 不兼容
    （av.open 的 metadata_errors 参数报 TypeError）。
    """
    global _ASR
    tmp = path + ".asr16k.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path,
             "-ar", "16000", "-ac", "1", tmp],
            check=True, capture_output=True,
        )
        with wave.open(tmp, "rb") as w:
            raw = w.readframes(w.getnframes())
        y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        from faster_whisper import WhisperModel
        if _ASR is None:
            # base 的缓存快照下载不完整（2.5MB 空壳）会让核验永远失效；
            # 本地已有 small 完整权重，唱歌咬字也更准。
            _ASR = WhisperModel("small", device="cpu", compute_type="int8",
                                local_files_only=True)
        segs, _ = _ASR.transcribe(y, language="zh", beam_size=5,
                                  vad_filter=False, initial_prompt="简体中文。",
                                  condition_on_previous_text=False)
        return "".join(s.text for s in segs).strip()
    except Exception as e:  # noqa: BLE001
        log(f"ASR 不可用：{type(e).__name__}: {str(e)[:120]}")
        return ""
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _char_sim(ref: str, hyp: str) -> float:
    """只比汉字/字母/数字的字符相似度，忽略标点、语气词和空格。"""
    a = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", ref or "")
    b = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", hyp or "")
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _save_wav(y: np.ndarray, path: str) -> None:
    # 硬削波会把瞬态变成爆音/咔哒；超满量程时等比收回来，保留波形形状
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 0.999:
        y = y * (0.999 / peak)
    pcm = (y * 32767).astype(np.int16)
    AudioSegment(data=pcm.tobytes(), sample_width=2, frame_rate=44100,
                 channels=1).export(path, format="wav")


def clarity_boost(src_voc: str, conv_wav: str, out_wav: str) -> str:
    """按原唱这一段的长期平均频谱，把换声干声 4k 以上的缺口补回来。

    Seed-VC 微调权重吃的是 16k 语音数据，换出来的嗓子在 8k 以上几乎为零，
    4-8k 也被压了一截；这就是「字糊/发闷」的主因。这里只改频谱形状
    （电平后面混音会重新归一），4k 以上自适应匹配，最大 +10dB。
    """
    try:
        from scipy import signal as sg
        fs = 44100
        a = samples(AudioSegment.from_file(src_voc).set_channels(1).set_frame_rate(fs))
        y = samples(AudioSegment.from_file(conv_wav).set_channels(1).set_frame_rate(fs))
        if a.size < fs // 2 or y.size < fs // 2:
            return conv_wav
        nperseg = 4096
        f, ps = sg.welch(a, fs=fs, nperseg=nperseg, noverlap=nperseg // 2)
        _, pt = sg.welch(y, fs=fs, nperseg=nperseg, noverlap=nperseg // 2)
        ps = ps / (ps.sum() + 1e-20)
        pt = pt / (pt.sum() + 1e-20)
        logf = np.log2(np.maximum(f, 1.0))
        grid = np.arange(logf[0], logf[-1], 1 / 6.0)
        src_s = np.interp(grid, logf, np.log10(ps + 1e-20))
        conv_s = np.interp(grid, logf, np.log10(pt + 1e-20))
        g_db = (src_s - conv_s) * 10.0
        # 3.5k 以下保持原样，半八度淡入，别把元音听感改掉
        fade = np.clip((grid - np.log2(CLARITY_LOW_HZ)) / 0.4, 0.0, 1.0)
        g_db = np.clip(g_db * fade, CLARITY_MAX_CUT_DB, CLARITY_MAX_GAIN_DB)
        # 固定搁架：模型 4-8k 也压了一点，光靠匹配补不满
        g_db = np.clip(
            g_db + CLARITY_SHELF_DB * np.clip((grid - np.log2(3000.0)) / 0.5, 0, 1),
            CLARITY_MAX_CUT_DB, CLARITY_MAX_GAIN_DB + CLARITY_SHELF_DB,
        )
        # 三点平滑：增益曲线折角会在时域留下前后振铃，听感像咔哒/爆破
        g_db = np.convolve(np.pad(g_db, 1, mode="edge"),
                           np.ones(3) / 3.0, mode="valid")
        # 9k 以上渐隐到 0dB（到 13.5k 完全不补），别把噪声底跟着抬起来
        taper = np.clip((grid - np.log2(CLARITY_TAPER_HZ)) / 0.585, 0.0, 1.0)
        g_db = g_db * (1.0 - taper)
        freqs = np.concatenate([[0.0], 2.0 ** grid, [fs / 2.0]])
        gains = np.concatenate([[1.0], 10 ** (g_db / 20.0),
                                [10 ** (g_db[-1] / 20.0)]])
        for i in range(1, freqs.size):
            if freqs[i] <= freqs[i - 1]:
                freqs[i] = freqs[i - 1] + 1e-6
        fir = sg.firwin2(2049, freqs, gains, fs=fs)
        out = sg.fftconvolve(y, fir, mode="same")
        # 高频抬升会把瞬态峰值推过满量程，_save_wav 再硬削波就成爆破音了；
        # 这里按输入峰值等比收回来（混音阶段反正会重新归一 RMS）
        pk_in = float(np.max(np.abs(y))) + 1e-9
        pk_out = float(np.max(np.abs(out))) + 1e-9
        if pk_out > pk_in:
            out = out * (pk_in / pk_out)
            log(f"高频增强后峰值 {pk_out:.3f}→{pk_in:.3f}（等比收回，避免削波）")
        _save_wav(out, out_wav)
        at = lambda hz: float(np.interp(np.log2(hz), grid, g_db))
        log(f"咬字增强 4k {at(4000):+.1f}dB｜8k {at(8000):+.1f}dB｜12k {at(12000):+.1f}dB")
        return out_wav
    except Exception as e:  # noqa: BLE001
        log(f"咬字增强跳过：{type(e).__name__}: {str(e)[:100]}")
        return conv_wav


def mix_to_mp3(vocal_wav: str, acc_path: str | None, out_mp3: str,
               ref_balance: float | None = None,
               ref_rms: float | None = None,
               ref_voc: float | None = None) -> float:
    """把人声和伴奏混成 mp3。

    ref_voc = 原曲这一段的干声 RMS。Seed-VC 输出的干声通常会比原唱响 2 倍以上，
    所以先把换出来的干声拉回原唱的电平，再和原版伴奏 1:1 叠；
    这样人声/伴奏的配比、整体响度都和原版一致。
    """
    voc = AudioSegment.from_file(vocal_wav).set_channels(1).set_frame_rate(44100)
    yv = samples(voc)
    if acc_path:
        acc = AudioSegment.from_file(acc_path).set_channels(1) \
            .set_frame_rate(44100)
        ya = samples(acc)
        n = min(len(yv), len(ya))
        yv, ya = yv[:n], ya[:n]
        rv = float(np.sqrt(np.mean(yv ** 2)) + 1e-9)
        ra = float(np.sqrt(np.mean(ya ** 2)) + 1e-9)
        if rv < 0.01:
            # 换出来的干声几乎是静音：别再发一条只有伴奏的“翻唱”
            raise RuntimeError(f"转换后没有人声（RMS {rv:.4f}）")
        if ref_voc and ref_voc > 0:
            # 干声先归到原唱电平，伴奏就是原版电平，直接 1:1 叠
            gain_v = float(ref_voc / rv)
            yv = yv * gain_v
            # 换出来的嗓子丢了 8k 以上，同等 RMS 下听感会比原唱轻一截，
            # 补 2.5dB 才不会被伴奏盖住
            yv = yv * VOCAL_BOOST
            g = 1.0
            log(f"人声RMS {rv:.4f} → {float(np.sqrt(np.mean(yv ** 2))):.4f}"
                f"（原唱 {ref_voc:.4f}，乘以 {gain_v:.2f}×{VOCAL_BOOST:.2f}）"
                f"｜伴奏RMS {ra:.4f} 增益 {g:.2f}")
            if ref_balance:
                log(f"原版伴奏/人声 {ref_balance:.2f}，人声已归到原唱电平")
        elif ref_balance and ref_balance > 0:
            # 没有干声参考时：照原版配比推伴奏增益（有上下限，防止跑飞）
            g = float(np.clip(ref_balance * rv / ra, 0.15, 1.4))
            log(f"人声RMS {rv:.4f} 伴奏RMS {ra:.4f} 增益 {g:.2f}"
                f"（照原版配比 伴奏/人声 {ref_balance:.2f}）")
        else:
            g = float(np.clip(rv / ra * 0.75, 0.2, 1.2))
            log(f"人声RMS {rv:.4f} 伴奏RMS {ra:.4f} 增益 {g:.2f}")
        mix = yv + ya * g
    else:
        mix = yv * 0.96

    # 响度：有原曲参考就对齐原曲，没参考才归到 -18 dBFS。
    # 峰值留 1dB 余量（0.89 ≈ -1dBFS）：0.95 只剩 0.45dB，MP3 编码/播放端
    # 的采样间峰值会顶到 0dBFS，听起来就是爆音。
    rms = float(np.sqrt(np.mean(mix ** 2)) + 1e-9)
    peak = float(np.max(np.abs(mix))) + 1e-9
    target = float(ref_rms) if ref_rms and ref_rms > 0 else 10 ** (-18 / 20.0)
    mix = mix * min(target / rms, 0.89 / peak)
    log(f"输出响度 {20 * np.log10(float(np.sqrt(np.mean(mix ** 2)) + 1e-9)):.1f}dB"
        + (f"（原曲 {20 * np.log10(target):.1f}dB）" if ref_rms else ""))
    pcm = (np.clip(mix, -1, 1) * 32767).astype(np.int16)
    seg = AudioSegment(data=pcm.tobytes(), sample_width=2, frame_rate=44100,
                       channels=1)
    seg.export(out_mp3, format="mp3", bitrate="160k")
    return len(seg) / 1000.0


def main() -> None:
    job = json.loads(open(sys.argv[1], encoding="utf-8").read())
    mode = str(job.get("mode") or "cover")
    src = str(job["src"])
    out = str(job["out"])
    semi = int(job.get("semi") or 0)
    auto_f0 = bool(job.get("auto_f0", DEFAULT_AUTO_F0))
    steps = int(job.get("steps") or DEFAULT_STEPS)
    cfg_rate = float(job.get("cfg_rate") or DEFAULT_CFG_RATE)
    target = str(job.get("target") or "")
    f0_mix = float(job.get("f0_mix", DEFAULT_F0_MIX))
    f0_max_up = float(job.get("f0_max_up", DEFAULT_F0_MAX_UP))
    verify = bool(job.get("verify", True))
    ckpt = str(job.get("ckpt") or "")
    if ckpt:
        os.environ["COVER_FT_CKPT"] = ckpt
    if not os.path.exists(src):
        raise RuntimeError(f"源文件不存在: {src}")
    work = tempfile.mkdtemp(prefix="cover_")
    t0 = __import__("time").time()
    # 唱歌优先：先卸聊天模型，再让续训停手，才等得到显存
    unload_ollama()
    pause_training()
    try:
        start, dur = 0.0, 0.0
        ref_bal, ref_rms, ref_voc = None, None, None
        if mode == "cover":
            voc, acc = demucs_split(src, work,
                                    allow_low=bool(job.get("best_effort_source")))
            start, dur = hook_window(voc, acc, len(AudioSegment.from_file(voc)) / 1000.0)
            # 先切片再转换：Seed-VC 只跑这一段，又快又不会长输入跑糊
            a = int(start * 1000)
            b = a + int(dur * 1000)
            vclip = os.path.join(work, "hook_voc.wav")
            aclip = os.path.join(work, "hook_acc.wav")
            AudioSegment.from_file(voc)[a:b].export(vclip, format="wav")
            AudioSegment.from_file(acc)[a:b].export(aclip, format="wav")
            v_ac = _energy_curve(vclip)
            log(f"高潮段 {start:.1f}s 起 {dur:.1f}s，切片干声 RMS "
                f"{float(np.mean(v_ac)):.4f}（全曲 {float(np.mean(_energy_curve(voc))):.4f}）")
            voc, acc = vclip, aclip
            # 原曲这一段人声/伴奏的真实电平比，混音时照搬，避免"歌和伴奏两个响度"
            rv0 = float(np.mean(_energy_curve(vclip)))
            ra0 = float(np.mean(_energy_curve(aclip)))
            if rv0 > 1e-6:
                ref_bal = ra0 / rv0
                ref_rms = float(np.sqrt(rv0 ** 2 + ra0 ** 2))
                ref_voc = rv0
                log(f"原版配比 伴奏/人声 {ref_bal:.2f}，总响度 "
                    f"{20 * np.log10(max(ref_rms, 1e-9)):.1f}dB")
        else:
            voc, acc = src, None
            dur = len(AudioSegment.from_file(src)) / 1000.0
        conv = seedvc(voc, os.path.join(work, "svc"), semi, auto_f0,
                      steps, cfg_rate, target,
                      ctx_sec=min(46.0, max(31.0, dur + 3.0)),
                      f0_mix=f0_mix, f0_max_up=f0_max_up)
        used_shift = None
        holes, hfrac = _hole_count(voc, conv)
        log(f"断音检测：{holes} 处（{hfrac:.1%} 帧）")
        # 歌词核验：转换后唱的还是不是原唱那几句（原干声 ASR ↔ 转换后 ASR 比对）
        lyric_sim, lyric_out, retried = None, "", False
        if verify:
            ref_txt = asr_text(voc)
            if ref_txt:
                lyric_out = asr_text(conv)
                lyric_sim = _char_sim(ref_txt, lyric_out)
                log(f"歌词核验 相似度 {lyric_sim:.1%}｜原唱「{ref_txt[:36]}」"
                    f"→ 转换「{lyric_out[:36]}」")
                if lyric_sim < VERIFY_SIM_MIN:
                    # 咬字退得太多：少对齐一点重唱一版，取相似度高的那版，
                    # 避免"唱是唱出来了但字全错"。
                    log(f"咬字相似度低于 {VERIFY_SIM_MIN:.0%}，换保守参数重唱")
                    conv2 = seedvc(voc, os.path.join(work, "svc2"), semi,
                                   auto_f0, steps, cfg_rate, target,
                                   ctx_sec=min(46.0, max(31.0, dur + 3.0)),
                                   f0_mix=min(f0_mix, 0.4),
                                   f0_max_up=f0_max_up)
                    shift2 = None
                    out2 = asr_text(conv2)
                    sim2 = _char_sim(ref_txt, out2)
                    retried = True
                    log(f"重唱后相似度 {sim2:.1%}（"
                        f"{'采用新版' if sim2 > lyric_sim else '保留原版'}）")
                    if sim2 > lyric_sim:
                        conv, lyric_out, lyric_sim = conv2, out2, sim2
                        used_shift = shift2
                        holes, hfrac = _hole_count(voc, conv)
        # 咬字清晰度：按原唱这一段补回 4k 以上的高频缺口（失败则原样用）
        conv = clarity_boost(voc, conv, os.path.join(work, "clarity.wav"))
        outdur = mix_to_mp3(conv, acc, out, ref_bal, ref_rms, ref_voc)
        log(f"完成 {outdur:.1f}s，用时 {__import__('time').time() - t0:.0f}s")
        print(json.dumps({"ok": True, "duration": outdur, "hook_start": start,
                          "seconds": round(__import__("time").time() - t0),
                          "holes": holes,
                          "lyric_sim": None if lyric_sim is None else round(lyric_sim, 3),
                          "lyric_cer": None if lyric_sim is None else round(1 - lyric_sim, 3),
                          "lyric_text": lyric_out[:120],
                          "retried": retried, "shift_semitones": used_shift},
                         ensure_ascii=False))
    finally:
        resume_training()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"失败: {type(e).__name__}: {e}")
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False))
