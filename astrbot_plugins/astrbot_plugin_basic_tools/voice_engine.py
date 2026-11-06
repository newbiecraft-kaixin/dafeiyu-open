"""中文语音「句调」引擎：把一段话按句子分别变调再拼起来。

为什么需要这一层
----------------
edge-tts 是云端固定音色，只能给**整段**一个 rate/pitch。整段同一个音高读下来，
中文听起来就是「念稿」——中文的语调信息有一大半在**句调**上：
疑问句尾音上扬、感叹句起音高、委屈时整体压低、撒娇时稍高稍慢。

这里的做法（零模型、零 GPU、即时可用）：
  1. 按句末标点切句（问号/感叹号/句号/省略号/逗号长句）
  2. 每句单独算 pitch 偏移：疑问 +6Hz、感叹 +4Hz、软尾（啦呀嘛呢）+3Hz、
     省略号 -4Hz、超长句 -2Hz（避免越说越高）
  3. 每句单独让 edge-tts 合成，再用 pydub 拼回去，中间插 70~130ms 停顿
  4. 停顿长度也按标点走：句号 130ms、逗号 70ms、省略号 220ms

上限说明
--------
这条路的天花板是「整体升降调 + 停顿节奏」，做不出真正的情感音色变化。
要「用撒娇的语气说这句话」这种自然语言控制，得换本地情感 TTS
（CosyVoice2 / IndexTTS2），见 README 的对比。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import re
import shutil
import tempfile
import threading
from urllib.parse import urlsplit

# 情绪预设：(pitch 基准 Hz, rate 百分比, 说明)
MOOD_PRESETS: dict[str, tuple[int, int]] = {
    "乖巧": (0, 6),
    "开心": (8, 12),
    "撒娇": (10, -4),
    "委屈": (-8, -8),
    "认真": (-2, 0),
    "困了": (-10, -14),
    "惊讶": (14, 10),
    "生气": (-4, 8),
    "害羞": (6, -6),
}

DEFAULT_MOOD = "乖巧"

# ---------------------------------------------------------------------------
# CosyVoice2-0.5B 本地服务（独立进程，见 dafeiyu/cosyvoice_tts_server.py）
# 中文情感 TTS：用「用开心的语气说」这类自然语言指令做音调/情绪控制，
# 并克隆一段参考声线 = 声音更好听。比 edge-tts 的整体升降调自然得多。
# ---------------------------------------------------------------------------
COSY_URL = os.environ.get("COSYVOICE_URL", "http://127.0.0.1:8714")

# 音色定调：少女/萝莉嗓（2026-10-30 两轮迭代，主人先嫌御姐老、又嫌 +6st 是小屁孩）。
# 实测结论（语音 A/B，2026-10-30）：
#   1) CosyVoice 的 instruct2（「用开心的语气说」这类自然语言指令）会把同一句话
#      拖长到约 2 倍，并且把气声（呼吸感）抹掉 —— 听感就是「拖沓 + 干硬 + 机械」，
#      音高还会被压到 190Hz 附近变成大妈音。所以这里**不再用 instruct**。
#   2) 改走 zero-shot 克隆：只给参考音频 + speed。历史候选都在 cosyvoice_ref/：
#      版本1=ref_loli_fw2.wav(+2st)、版本2=ref_loli_fw3.wav(+3st)、
#      版本3=ref_loli_xiaoyi.wav(真少女，2026-11-01 A/B 主人改选它，现役)。
#      教训：librosa 整体升调会连共振峰一起升，声道「缩小」→ 小屁孩；
#      pyworld 只升基频是「变甜」不是「变小」；但最后真少女音色完胜两条 DSP 路。
#      现役 ref.wav = ref_loli_xiaoyi.wav，唱歌（sing_render）同款。
#   3) 情绪不靠指令，靠语速（见 MOOD_SPEED）+ 台词里的语气词/标点，
#      情绪音频由人自己听得出，而不是把音色一起改掉。
#   4) 「听不清」修复：合成请求带 af 清晰度链（2.8kHz 临场感 + 响度归一），
#      QQ 语音压缩后依然清楚。

# 参考音的转写文本不写死：每个声线档案的 prompt.txt 就是它那段 ref.wav 的转写，
# zero-shot 克隆要求两者一致；没有转写（或只有“无需参考文本”备注）就走 cross_lingual。
# 服务端按请求读，改完热重载本插件即可，不用重启 TTS 服务。

# 声线档案：cosyvoice_ref/profiles/<名称>/{ref.wav,long.wav,prompt.txt,meta.txt}
# .active 里存当前声线名；说话和唱歌都读它，改完即时生效，不用重启 TTS 服务。
REF_DIR = r"C:\path\to\dafeiyu\cosyvoice_ref"
PROFILE_ROOT = os.path.join(REF_DIR, "profiles")
ACTIVE_FILE = os.path.join(PROFILE_ROOT, ".active")
DEFAULT_PROFILE = "小忆"


def list_profiles() -> list[tuple[str, str]]:
    if not os.path.isdir(PROFILE_ROOT):
        return []
    out: list[tuple[str, str]] = []
    for name in sorted(os.listdir(PROFILE_ROOT)):
        d = os.path.join(PROFILE_ROOT, name)
        if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "ref.wav")):
            continue
        meta = ""
        try:
            with open(os.path.join(d, "meta.txt"), encoding="utf-8-sig") as f:
                meta = f.read().strip()
        except OSError:
            pass
        out.append((name, meta))
    return out


def active_profile() -> str:
    try:
        with open(ACTIVE_FILE, encoding="utf-8-sig") as f:
            name = f.read().strip()
    except OSError:
        return DEFAULT_PROFILE
    if not os.path.exists(os.path.join(PROFILE_ROOT, name, "ref.wav")):
        return DEFAULT_PROFILE
    return name


def set_profile(name: str) -> bool:
    name = (name or "").strip()
    if not name or not os.path.exists(os.path.join(PROFILE_ROOT, name, "ref.wav")):
        return False
    with open(ACTIVE_FILE, "w", encoding="utf-8") as f:
        f.write(name)
    return True


def profile_ref(long: bool = False) -> str:
    d = os.path.join(PROFILE_ROOT, active_profile())
    if long and os.path.exists(os.path.join(d, "long.wav")):
        return os.path.join(d, "long.wav")
    return os.path.join(d, "ref.wav")


def profile_prompt() -> str:
    """参考音对应的转写文本；空字符串 = 只做 cross_lingual 音色克隆。

    prompt.txt 里如果写的是「（使用 instruct 模式，无需参考文本）」这类备注，
    它不是 ref.wav 的转写，传给 zero_shot 会让参考音和文本对不上，音色反而走样。
    """
    d = os.path.join(PROFILE_ROOT, active_profile())
    try:
        with open(os.path.join(d, "prompt.txt"), encoding="utf-8-sig") as f:
            text = f.read().strip()
        if (text and not text.startswith(("（", "("))
                and "无需参考文本" not in text and "instruct" not in text.lower()):
            return text
    except OSError:
        pass
    return ""

# 清晰度后处理（ffmpeg af）：去低频闷响 + 2.8kHz 临场感 + 响度归一
CLARITY_AF = "highpass=f=90,equalizer=f=2800:t=q:w=1.0:g=3,loudnorm=I=-17:TP=-1.5:LRA=11"

MOOD_SPEED: dict[str, float] = {
    "乖巧": 1.08,
    "开心": 1.18,
    "撒娇": 1.02,
    "委屈": 0.96,
    "认真": 1.05,
    "困了": 0.92,
    "惊讶": 1.15,
    "生气": 1.12,
    "害羞": 1.00,
}

# edge 回退音色：Xiaoyi（年轻女声）+ 整体升调，贴萝莉音色（仅 CosyVoice 服务挂了才走这里）
EDGE_VOICE = "zh-CN-XiaoyiNeural"
EDGE_PITCH = 6
EDGE_RATE = -2


_TOKEN_RE = re.compile(r":[^:\s]{1,12}:")
_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+")
_MD_RE = re.compile(r"[*_`#>\[\]()【】]")
_EMOJI_RE = re.compile(
    "[" "\U0001F000-\U0001FAFF" "\u2600-\u27BF" "\uFE0F\u200D\u20E3" "]"
)


def clean_for_tts(text: str) -> str:
    """把「聊天文本」洗成「能念的文本」。

    表情占位符 :吃瓜:、CQ 码、URL、markdown 符号、emoji 念出来都是噪音，
    而且 CosyVoice 遇到它们容易断句怪异，所以合成前先清掉。
    """
    text = _CQ_RE.sub("", text or "")
    text = _URL_RE.sub("", text)
    text = _TOKEN_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = _MD_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    # 结尾没标点时补一个句号，避免最后一个字被吞掉
    if text and text[-1] not in "。！？!?…~～":
        text += "。"
    return text


_COSY_URL_PARTS = urlsplit(COSY_URL)
_COSY_HOST = _COSY_URL_PARTS.hostname or "127.0.0.1"
_COSY_PORT = _COSY_URL_PARTS.port or 8714
_COSY_LOCAL = threading.local()


def _cosy_connection(timeout: float = 300.0) -> http.client.HTTPConnection:
    """每线程复用自己的 HTTP/1.1 长连接，语音合成不用每条都重新握手。"""
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


def cosy_health() -> bool:
    """本地 CosyVoice 服务是否在线。

    注意：服务是「懒加载」的，模型没加载时 ready=false 但这个服务仍然可用
    （/tts 会触发加载）。所以这里只看 ok，不看 ready，否则永远回退 edge-tts。
    """
    import urllib.request

    try:
        with urllib.request.urlopen(COSY_URL + "/health", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("ok"))
    except Exception:  # noqa: BLE001
        return False


def cosy_ready() -> bool:
    """模型是否已经在显存里（true 时首包很快，false 时第一次请求要等加载）。"""
    import urllib.request

    try:
        with urllib.request.urlopen(COSY_URL + "/health", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("ready"))
    except Exception:  # noqa: BLE001
        return False


def synth_cosy(text: str, out_path: str, mood: str = DEFAULT_MOOD) -> str:
    """用本地 CosyVoice 服务合成（克隆少女声线 + 语速控情绪），写入 out_path 并返回。

    prompt_text 为空时服务端走 cross_lingual（只克隆音色，不要求转写），
    有转写时走 zero-shot，两者都必须与 ref.wav 一致（见 profile_prompt）。
    """
    speed = MOOD_SPEED.get(mood, MOOD_SPEED[DEFAULT_MOOD])
    payload = json.dumps(
        {
            "text": text,
            "speed": speed,
            "fmt": "mp3",
            "prompt_wav": profile_ref(),
            "prompt_text": profile_prompt(),
            "af": CLARITY_AF,
        }
    ).encode("utf-8")
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
    return out_path

# 句末标点 -> (pitch 修正, 停顿毫秒)
_END_RULES: list[tuple[str, int, int]] = [
    ("？", 6, 150),
    ("?", 6, 150),
    ("！", 4, 130),
    ("!", 4, 130),
    ("…", -4, 220),
    ("。", 0, 130),
    (".", 0, 130),
    ("，", -1, 70),
    (",", -1, 70),
    ("；", -1, 90),
    (";", -1, 90),
    ("、", -1, 60),
]

_SOFT_TAIL = ("啦", "呀", "嘛", "呢", "哦", "耶", "咯", "嗷", "呜")
_LIVELY = "呀啦嘛呢诶嘿呜呜好耶嗷贴贴"
_STRONG = ("。", "！", "？", "!", "?", "…", "\n")


def group_by_sentence(
    segs: list[tuple[str, int]],
) -> list[tuple[str, list[tuple[str, int]]]]:
    """把逗号级切片按「完整句子」分组。

    逐段单独算 pitch 会让同一句话内部的音高反复跳（听起来一顿一顿的机械味），
    所以音高按整句算，停顿仍按原标点 —— 句子中间有自然停顿，但音高连续。
    """
    groups: list[list[tuple[str, int]]] = []
    cur: list[tuple[str, int]] = []
    for seg, pause in segs:
        cur.append((seg, pause))
        if (seg.rstrip()[-1:] or "") in _STRONG:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return [("".join(s for s, _ in g), g) for g in groups]


def _ffmpeg() -> str:
    """找 ffmpeg：PATH 里没有就用 WinGet 装的那份（AstrBot 进程 PATH 常缺）。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    cand = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"
    )
    return cand if os.path.exists(cand) else ""


def split_sentences(text: str, max_len: int = 28) -> list[tuple[str, int]]:
    """切成 [(句子, 停顿毫秒)]。过长且没标点的句子按逗号再切。"""
    text = (text or "").strip()
    if not text:
        return []
    # 先按句末/停顿标点切，保留标点
    parts = re.findall(r"[^。！？!?…，,；;、\n]+[。！？!?…，,；;、]?", text)
    out: list[tuple[str, int]] = []
    for part in parts:
        seg = part.strip()
        if not seg:
            continue
        tail = seg[-1]
        pause = 130
        for mark, _dp, ms in _END_RULES:
            if tail == mark:
                pause = ms
                break
        # 超长无标点段：按字数硬切（这里切的是合成单元，不影响显示文本）
        while len(seg) > max_len * 2:
            cut = max(
                seg.rfind("，", 0, max_len), seg.rfind(" ", 0, max_len), max_len
            )
            if cut <= 0:
                cut = max_len
            out.append((seg[:cut], 70))
            seg = seg[cut:].lstrip("，, ")
        if seg:
            out.append((seg, pause))
    return out


def sentence_pitch(seg: str, base: int) -> int:
    """单句 pitch 偏移 = 基准 + 句式修正。范围卡在 ±45Hz（edge-tts 上限附近）。"""
    p = base
    tail = seg.rstrip()[-1:] or ""
    for mark, dp, _ms in _END_RULES:
        if tail == mark:
            p += dp
            break
    if tail in _SOFT_TAIL:
        p += 3
    if sum(seg.count(w) for w in _LIVELY) >= 2:
        p += 3
    if len(seg) > 30:
        p -= 2  # 长句说久了会往上飘，压一点
    return max(-45, min(45, p))


async def synth(
    text: str,
    out_path: str,
    voice: str = EDGE_VOICE,
    mood: str = DEFAULT_MOOD,
) -> str:
    """合成带语气变化的语音，返回输出文件路径（失败抛异常）。

    优先用本地 CosyVoice2-0.5B（zero-shot 克隆少女声线 + 语速控情绪，最自然）；
    服务没起时回退 edge-tts + 句调引擎（仅整体升降调，无情感音色）。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("文本为空")
    text = clean_for_tts(text)
    if not text.strip():
        raise ValueError("文本为空")
    # 优先本地 CosyVoice2-0.5B
    if cosy_health():
        try:
            return await asyncio.to_thread(synth_cosy, text, out_path, mood)
        except Exception as e:  # noqa: BLE001
            print(f"[voice_engine] CosyVoice 合成失败，回退 edge-tts: {type(e).__name__}: {e}")
    # 回退：edge-tts 句调引擎（原实现）
    import edge_tts
    from pydub import AudioSegment

    base_pitch, base_rate = MOOD_PRESETS.get(mood, MOOD_PRESETS[DEFAULT_MOOD])
    base_pitch += EDGE_PITCH
    base_rate += EDGE_RATE
    segs = split_sentences(text)
    if not segs:
        raise ValueError("文本为空")

    # 音高按整句算（同一句话内不跳调），停顿仍按逗号/句号走
    units: list[tuple[str, int, int, int]] = []
    for sent_text, parts in group_by_sentence(segs):
        pitch = sentence_pitch(sent_text, base_pitch)
        rate = base_rate + (2 if pitch >= base_pitch + 6 else 0)
        units.extend((seg, pause, pitch, rate) for seg, pause in parts)

    tmpdir = tempfile.mkdtemp(prefix="voice_")
    try:
        async def _one(i: int, seg_text: str, pitch: int, rate: int) -> str:
            f = os.path.join(tmpdir, f"s{i:03d}.mp3")
            await edge_tts.Communicate(
                seg_text,
                voice,
                rate=f"{rate:+d}%",
                pitch=f"{pitch:+d}Hz",
            ).save(f)
            return f

        # 并发合成（edge-tts 单次 300~600ms，串行会明显卡）
        files = await asyncio.gather(
            *[_one(i, u[0], u[2], u[3]) for i, u in enumerate(units)],
            return_exceptions=True,
        )

        ff = _ffmpeg()
        if ff:
            AudioSegment.converter = ff
            AudioSegment.ffprobe = os.path.join(os.path.dirname(ff), "ffprobe.exe")

        merged: AudioSegment | None = None
        for i, (f, (_seg_text, pause, _p, _r)) in enumerate(zip(files, units)):
            if isinstance(f, Exception) or not os.path.exists(f):
                continue
            try:
                clip = AudioSegment.from_file(f, format="mp3")
            except Exception:
                continue
            if merged is None:
                merged = clip
            else:
                gap = AudioSegment.silent(duration=pause, frame_rate=clip.frame_rate)
                merged = merged + gap + clip
        if merged is None:
            raise RuntimeError("所有句子合成都失败了")

        merged.export(out_path, format="mp3", bitrate="128k")
        return out_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def describe(seg_text: str, mood: str = DEFAULT_MOOD) -> list[str]:
    """调试用：打印每一句会被怎么变调。"""
    base, rate = MOOD_PRESETS.get(mood, MOOD_PRESETS[DEFAULT_MOOD])
    return [
        f"{s[:16]!r:22} pitch={sentence_pitch(s, base):+d}Hz pause={ms}ms"
        for s, ms in split_sentences(seg_text)
    ]
