#!/usr/bin/env python3
"""CosyVoice2-0.5B 本地 TTS 服务（独立进程，给 AstrBot 的 voice_engine 调用）。

为什么要独立进程：CosyVoice 要 torch CUDA + 几 GB 显存，塞进 AstrBot 的运行时既重又容易把
机器人带崩；单独跑一个 HTTP 服务，模型常驻显存，AstrBot 只发 HTTP 请求拿音频，互不影响。

端点：
  GET  /health            -> {"ok": true, "ready": <bool>, "provider": "cosyvoice|openai", "err": ...}
  POST /tts               -> JSON {text, instruct?, prompt_wav?, prompt_text?, fmt?}
                             返回音频字节（默认 wav；fmt=mp3 时本机有 ffmpeg 则转 mp3）
  GET  /v1/models         -> OpenAI 兼容模型列表
  GET  /v1/audio/voices   -> 可用音色（默认 alloy/echo/... + cosyvoice_ref/*.wav 的名字）
  POST /v1/audio/speech   -> OpenAI 兼容 TTS：{model, input, voice, response_format?, speed?}

换第三方语音（火山方舟 / 硅基流动 / OpenAI 等任何 OpenAI 兼容服务）只改环境变量：
  TTS_PROVIDER=openai
  TTS_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
  TTS_API_KEY=xxx
  TTS_MODEL=doubao-tts
  TTS_VOICE=zh_female_...
此时 /v1/audio/speech 原样转发给上游，本机不再加载 CosyVoice（省显存）。
不设置时默认就是本机自己，AstrBot 等调用方照旧。

情绪/音调：靠 instruct 自然语言指令（"用开心的语气说"），比 edge-tts 的整体升降调自然得多。
声音好听：靠 prompt_wav 克隆一段好听的参考声线（默认用模型自带 asset/zero_shot_prompt.wav，
可换成你自己的 3~10 秒 wav —— 设 COSYVOICE_PROMPT_WAV 环境变量）。
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# ---- 配置（环境变量可覆盖）----
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "shared"))
from infra import find_dir  # noqa: E402

# 模型目录按「仓库根 → ~/dafeiyu → 浅层搜索」自动找，找不到时用环境变量指定
_MODELS_DIR = find_dir(["cosyvoice_models"])
MODEL_DIR = os.environ.get("COSYVOICE_MODEL_DIR") or (
    os.path.join(str(_MODELS_DIR), "CosyVoice2-0.5B") if _MODELS_DIR else ""
)
PROMPT_WAV = os.environ.get("COSYVOICE_PROMPT_WAV", "")
PROMPT_TEXT = os.environ.get("COSYVOICE_PROMPT_TEXT", "希望你以后能够做的比我还好呦。")
PORT = int(os.environ.get("COSYVOICE_PORT", "8714"))
# 参考声线：放在 cosyvoice_ref/ref.wav（3~10 秒干净人声）就自动用上
REF_DIR = os.environ.get("COSYVOICE_REF_DIR") or str(
    find_dir(["cosyvoice_ref"]) or os.path.join(os.path.expanduser("~"), "dafeiyu", "cosyvoice_ref")
)
# 空闲卸载模型的代价是「下一次首句要冷加载 ~100s」，桌宠/QQ 语音会因超时直接静音，
# 所以默认常驻显存；显存紧张时再通过 COSYVOICE_IDLE_UNLOAD 设成 900 等值。
IDLE_UNLOAD = int(os.environ.get("COSYVOICE_IDLE_UNLOAD", "0"))  # 空闲多少秒释放显存，0=不释放
# cosyvoice 源码仓库不在 site-packages 里，默认用 find_dir 自动找（可被环境变量覆盖）
_REPO_DIR_FOUND = find_dir(["CosyVoice"])
REPO_DIR = os.environ.get("COSYVOICE_REPO_DIR") or (
    str(_REPO_DIR_FOUND) if _REPO_DIR_FOUND else ""
)
LOAD_JIT = os.environ.get("COSYVOICE_LOAD_JIT", "0") == "1"
LOAD_TRT = os.environ.get("COSYVOICE_LOAD_TRT", "0") == "1"

# 本机 torch 是 +cpu 版，推理默认开满所有核（24）——台词批量预热时会把整机 CPU 打到 100%。
# 这里限制线程数，默认「核数/4」，可用 COSYVOICE_NUM_THREADS 覆盖。
NUM_THREADS = int(os.environ.get("COSYVOICE_NUM_THREADS") or max(1, (os.cpu_count() or 8) // 4))
for _k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_k, str(NUM_THREADS))

# ---- OpenAI 兼容：(TTS_BASE_URL / TTS_API_KEY / TTS_MODEL / TTS_VOICE) ----
# 统一沿用 OpenAI 的 base_url + api_key + model 三段式；默认值就是本机自己（8714），
# 想换火山方舟/硅基流动/OpenAI 只改环境变量或 config/endpoints.json 的 voice 段，
# 调用方无需改代码。
def _endpoint_cfg(name: str) -> dict:
    """读 config/endpoints.json 里某个服务的配置（不存在就返回空）。"""
    try:
        path = Path(__file__).resolve().parents[1] / "config" / "endpoints.json"
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entry = data.get(name) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else {}


_VOICE_CFG = _endpoint_cfg("voice")
TTS_BASE_URL = (os.environ.get("TTS_BASE_URL") or _VOICE_CFG.get("base_url") or "").strip().rstrip("/")
TTS_API_KEY = (os.environ.get("TTS_API_KEY") or _VOICE_CFG.get("api_key") or "").strip()
TTS_MODEL = (os.environ.get("TTS_MODEL") or _VOICE_CFG.get("model") or "cosyvoice2-0.5b").strip()
TTS_VOICE = (os.environ.get("TTS_VOICE") or _VOICE_CFG.get("voice") or "alloy").strip()
TTS_PROVIDER = (os.environ.get("TTS_PROVIDER") or ("openai" if TTS_BASE_URL else "")).strip().lower()
OPENAI_VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]


def _provider() -> str:
    """openai 表示走上游 OpenAI 兼容服务；否则用本地 CosyVoice 合成。"""
    if TTS_PROVIDER in ("openai", "upstream", "remote"):
        return "openai"
    if TTS_PROVIDER in ("cosyvoice", "local", "off", ""):
        return "cosyvoice"
    return TTS_PROVIDER

# 固定采样种子：CosyVoice 每次采样音高会飘几十 Hz（同一句话能从 180Hz 飘到 228Hz），
# 固定种子让「同一个角色的嗓音」稳定，也方便 A/B 对比。设 -1 关闭。
SEED = int(os.environ.get("COSYVOICE_SEED", "20240501"))
# 输出端降调系数：1.0 不改；0.88 ≈ 降 1.8 个半音（年轻女声 → 御姐的低音区）。
PITCH = float(os.environ.get("COSYVOICE_PITCH", "1.0"))
# 输出后处理（只作用于 mp3）：去掉低频闷响 + 补一点 6kHz 高频，让声音「透气」不那么闷。
# 不用压缩/限幅，避免把 Cosy 自带的呼吸起伏抹平（那才是机械感来源之一）。
POST_FILTER = os.environ.get(
    "COSYVOICE_AF", "highpass=f=80,highshelf=f=6000:gain=2.0"
)

_COSY = None
_ERR = None
_LOCK = threading.Lock()
_LAST_USE = 0.0  # 最后一次推理时间（懒加载 + 空闲释放显存）


def _load_model():
    """懒加载 CosyVoice2（首次 /tts 时），避免启动即占满显存。"""
    global _COSY, _ERR
    if _COSY is not None:
        return _COSY
    if _ERR is not None:
        raise RuntimeError(_ERR)
    with _LOCK:
        if _COSY is not None:
            return _COSY
        if _ERR is not None:
            raise RuntimeError(_ERR)
        try:
            if REPO_DIR:
                # cosyvoice 包本体：源码目录直接进 sys.path（不走 pip install）
                if os.path.isdir(os.path.join(REPO_DIR, "cosyvoice")):
                    sys.path.append(REPO_DIR)
                # Matcha-TTS 是 git 子模块，zip 发行里没有，单独放在 third_party
                sys.path.append(os.path.join(REPO_DIR, "third_party", "Matcha-TTS"))
            import torch  # noqa: F401

            torch.set_num_threads(NUM_THREADS)
            from cosyvoice.cli.cosyvoice import CosyVoice2

            _COSY = CosyVoice2(
                MODEL_DIR,
                load_jit=LOAD_JIT,
                load_trt=LOAD_TRT,
                fp16=torch.cuda.is_available(),
            )
        except Exception as e:  # noqa: BLE001
            _ERR = f"{type(e).__name__}: {e}"
            raise
    return _COSY


def _as_wav(path: str) -> str:
    """参考音统一转成 16k 单声道 wav（CosyVoice 的 load_wav 走 soundfile，mp3 不一定读得了）。"""
    if path.lower().endswith(".wav"):
        return path
    dst = os.path.join(REF_DIR, "ref_16k.wav")
    try:
        if os.path.getmtime(dst) >= os.path.getmtime(path):
            return dst
    except OSError:
        pass
    ff = _ffmpeg()
    if not ff:
        return path
    import subprocess

    try:
        subprocess.run(
            [ff, "-y", "-i", path, "-ac", "1", "-ar", "16000", dst],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        return dst
    except Exception:  # noqa: BLE001
        return path


def _default_prompt_wav() -> str:
    if PROMPT_WAV and os.path.exists(PROMPT_WAV):
        return _as_wav(PROMPT_WAV)
    for name in ("ref.wav", "ref.mp3", "ref.m4a"):
        p = os.path.join(REF_DIR, name)
        if os.path.exists(p):
            return _as_wav(p)
    cand = os.path.join(MODEL_DIR, "asset", "zero_shot_prompt.wav")
    return cand if os.path.exists(cand) else ""


def synthesize(
    text: str,
    instruct: str | None = None,
    prompt_wav: str | None = None,
    prompt_text: str | None = None,
    fmt: str = "wav",
    pitch: float | None = None,
    speed: float = 1.0,
    af: str | None = None,
) -> bytes:
    cosy = _load_model()

    # 调用方可能带着已被删除的声线档案路径；不要因此整句静音，回落到默认参考音。
    if prompt_wav and not os.path.exists(prompt_wav):
        print(f"[CosyVoice TTS] 参考音频不存在，改用默认声线: {prompt_wav}", flush=True)
        prompt_wav = None
    pw = prompt_wav or _default_prompt_wav()
    if not pw or not os.path.exists(pw):
        raise RuntimeError(
            "没有可用的参考音频（克隆声线）。放一个 3~10 秒的 wav 到 "
            "COSYVOICE_PROMPT_WAV，或放到模型目录的 asset/zero_shot_prompt.wav"
        )
    ptxt = prompt_text or PROMPT_TEXT
    with _LOCK:  # CosyVoice 推理非线程安全，串行化
        if SEED >= 0:
            try:
                import torch

                torch.manual_seed(SEED)
            except Exception:  # noqa: BLE001
                pass
        # 注意：当前 CosyVoice 版本的 API 收的 prompt_wav 是「文件路径」，
        # 内部自己 load_wav；传预加载的 tensor 会报 soundfile Invalid file。
        # 不要再走 instruct2：它内部会把「指令」当作参考文本喂给模型，
        # 参考音和指令对不上时，模型会把「用开心甜美的声音快速说」直接念出来。
        # 有参考文本 → zero_shot；只有参考音 → cross_lingual 克隆音色。
        if prompt_text:
            gen = cosy.inference_zero_shot(
                text, prompt_text, pw, stream=False, speed=speed
            )
        elif prompt_wav:
            gen = cosy.inference_cross_lingual(
                text, pw, stream=False, speed=speed
            )
        else:
            gen = cosy.inference_zero_shot(
                text, ptxt, pw, stream=False, speed=speed
            )
        # 模型会把长文本按句切开分块 yield，必须全部拼起来，否则只念第一句
        chunks = [o["tts_speech"] for o in gen]
        # 短句 / 并发时 zero_shot 偶发一个 chunk 都不吐（前端返回 500，调用方就静音了）；
        # 退化成「只给参考音色、不给参考转写」的 cross_lingual 再合成一次。
        if not chunks and prompt_text:
            print(f"[CosyVoice TTS] zero_shot 无音频，改用 cross_lingual 重试: {text[:40]}", flush=True)
            gen = cosy.inference_cross_lingual(
                text, pw, stream=False, speed=speed
            )
            chunks = [o["tts_speech"] for o in gen]
    if not chunks:
        raise RuntimeError(f"CosyVoice 没有返回音频（text={text[:40]!r}）")
    audio = chunks[0] if len(chunks) == 1 else _torch_cat(chunks)
    global _LAST_USE
    _LAST_USE = time.time()
    if PITCH != 1.0:
        audio = _shift_pitch(audio, cosy.sample_rate, PITCH)
    # 单次请求可以临时指定音高系数（1.0 不改，>1 更明亮，<1 更低沉），方便 A/B
    if pitch and abs(pitch - 1.0) > 1e-3:
        audio = _shift_pitch(audio, cosy.sample_rate, pitch)
    wav_bytes = _to_wav_bytes(audio, cosy.sample_rate)
    if fmt == "mp3":
        return _wav_to_mp3(wav_bytes, POST_FILTER if af is None else af) or wav_bytes
    return wav_bytes


def _torch_cat(chunks):
    import torch

    return torch.cat(chunks, dim=1)


def _shift_pitch(audio, sr: int, ratio: float):
    """只降基频、不动共振峰：保留 Cosy 的语调起伏（std 不变），把音色压到御姐的低音区。

    用 pyworld 分析-重合成，比整段重采样不会变慢、也不会变成男声。
    """
    try:
        import numpy as np
        import pyworld
        import torch

        y = audio.detach().cpu().numpy().astype(np.float64).reshape(-1)
        f0, t = pyworld.harvest(y, sr, f0_floor=60.0, f0_ceil=500.0, frame_period=5.0)
        sp = pyworld.cheaptrick(y, f0, t, sr)
        ap = pyworld.d4c(y, f0, t, sr)
        f0n = f0.copy()
        f0n[f0n > 0] *= ratio
        out = pyworld.synthesize(f0n, sp, ap, sr, frame_period=5.0)
        return torch.from_numpy(out.astype(np.float32)).unsqueeze(0)
    except Exception as e:  # noqa: BLE001
        print(f"[pitch] 降调失败，按原音输出: {e}", flush=True)
        return audio


def _to_wav_bytes(audio, sr: int) -> bytes:
    import soundfile as sf

    # 新版 torchaudio 去掉了 soundfile 后端（需 torchcodec），直接用 soundfile 写
    tf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tf.close()
    try:
        data = audio.detach().cpu().float().numpy()
        sf.write(tf.name, data.T if data.ndim == 2 else data, sr)
        with open(tf.name, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass


def _ffmpeg() -> str:
    """找 ffmpeg：PATH 里没有就用 WinGet 装的那份（服务进程 PATH 常缺）。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    cand = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"
    )
    return cand if os.path.exists(cand) else ""


def _wav_to_mp3(wav_bytes: bytes, af: str = ""):
    ff = _ffmpeg()
    if not ff:
        return None
    try:
        import subprocess

        cmd = [ff, "-i", "pipe:0"]
        if af:
            cmd += ["-af", af]
        cmd += ["-f", "mp3", "-b:a", "160k", "-ac", "1", "pipe:1"]
        p = subprocess.run(
            cmd,
            input=wav_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return p.stdout
    except Exception:  # noqa: BLE001
        if af:
            return _wav_to_mp3(wav_bytes, "")
        return None


# ----------------- HTTP 服务 -----------------
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _voice_names() -> list:
    """OpenAI 常见音色 + cosyvoice_ref 下自带的参考声线文件名。"""
    names = list(OPENAI_VOICES)
    try:
        for entry in sorted(os.listdir(REF_DIR)):
            stem, ext = os.path.splitext(entry)
            if ext.lower() in (".wav", ".mp3", ".flac") and stem not in names:
                names.append(stem)
    except OSError:
        pass
    return names


def _ref_for_voice(voice: str):
    """cosyvoice_ref/<voice>.wav 存在就当参考声线，否则用默认 prompt_wav。"""
    if not voice:
        return None
    for ext in (".wav", ".mp3", ".flac"):
        path = os.path.join(REF_DIR, voice + ext)
        if os.path.isfile(path):
            return path
    return None


def _upstream_speech(raw: bytes):
    """把 OpenAI 兼容请求原样转给上游，拿到音频字节。"""
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if TTS_API_KEY:
        headers["Authorization"] = f"Bearer {TTS_API_KEY}"
    req = urllib.request.Request(
        TTS_BASE_URL + "/audio/speech", data=raw, method="POST", headers=headers
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read(), resp.headers.get("Content-Type") or "audio/mpeg"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass  # 调用方提前断开（超时/取消），不算服务错误

    def _send_bytes(self, body: bytes, ctype: str) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/health"):
            self._send_json(200, {
                "ok": True,
                "ready": _COSY is not None,
                "provider": _provider(),
                "upstream": TTS_BASE_URL,
                "model": TTS_MODEL,
                "err": _ERR,
            })
        elif path.rstrip("/") == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{
                    "id": TTS_MODEL,
                    "object": "model",
                    "owned_by": "dafeiyu" if _provider() != "openai" else TTS_BASE_URL,
                }],
            })
        elif path.rstrip("/") in ("/v1/audio/voices", "/v1/voices"):
            voices = _voice_names()
            self._send_json(200, {
                "object": "list",
                "data": [{"id": v, "object": "voice", "default": v == TTS_VOICE} for v in voices],
            })
        else:
            self._send_json(404, {"ok": False, "msg": "not found"})

    def _openai_speech(self, data: dict) -> None:
        text = str(data.get("input") or "").strip()
        if not text:
            self._send_json(400, {
                "error": {"message": "input 为空", "type": "invalid_request_error"},
            })
            return
        if _provider() == "openai":
            if not TTS_BASE_URL:
                self._send_json(500, {
                    "error": {"message": "TTS_PROVIDER=openai 但未配置 TTS_BASE_URL",
                              "type": "server_error"},
                })
                return
            payload = dict(data)
            payload["model"] = TTS_MODEL or payload.get("model") or "tts-1"
            payload.setdefault("voice", TTS_VOICE)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                audio, ctype = _upstream_speech(body)
            except Exception as e:  # noqa: BLE001
                self._send_json(502, {
                    "error": {"message": f"上游 TTS 失败：{type(e).__name__}: {e}",
                              "type": "upstream_error"},
                })
                return
            self._send_bytes(audio, ctype)
            return

        fmt = str(data.get("response_format") or "mp3").lower()
        if fmt not in ("mp3", "wav"):
            fmt = "wav"
        voice = str(data.get("voice") or TTS_VOICE)
        try:
            speed = float(data.get("speed") or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        try:
            audio = synthesize(
                text,
                prompt_wav=_ref_for_voice(voice),
                fmt=fmt,
                speed=max(0.5, min(1.6, speed)),
            )
            self._send_bytes(audio, "audio/mpeg" if fmt == "mp3" else "audio/wav")
        except Exception as e:  # noqa: BLE001
            print(f"[CosyVoice TTS] 合成失败: {type(e).__name__}: {e}", flush=True)
            self._send_json(503, {
                "error": {"message": f"{type(e).__name__}: {e}", "type": "server_error"},
            })

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/tts", "/v1/audio/speech"):
            self._send_json(404, {"ok": False, "msg": "not found"})
            return
        try:
            ln = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(ln) if ln else b"{}"
            data = json.loads(raw or b"{}")
            if path == "/v1/audio/speech":
                self._openai_speech(data)
                return
            text = (data.get("text") or "").strip()
            if not text:
                self._send_json(400, {"ok": False, "msg": "text 为空"})
                return
            instruct = data.get("instruct") or None
            prompt_wav = data.get("prompt_wav") or None
            # 支持 base64 内联参考音频
            if isinstance(prompt_wav, str) and (
                prompt_wav.startswith("data:") or len(prompt_wav) > 300
            ):
                b64 = prompt_wav.split(",", 1)[-1]
                tf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tf.write(base64.b64decode(b64))
                tf.close()
                prompt_wav = tf.name
            prompt_text = data.get("prompt_text") or None
            fmt = data.get("fmt") or "wav"
            try:
                pitch = float(data["pitch"]) if data.get("pitch") else None
            except (TypeError, ValueError):
                pitch = None
            try:
                speed = float(data.get("speed") or 1.0)
            except (TypeError, ValueError):
                speed = 1.0
            speed = max(0.5, min(1.6, speed))
            af = data.get("af") if "af" in data else None
            audio = synthesize(
                text, instruct=instruct, prompt_wav=prompt_wav,
                prompt_text=prompt_text, fmt=fmt, pitch=pitch, speed=speed,
                af=af,
            )
            self._send_bytes(
                audio, "audio/mpeg" if fmt == "mp3" else "audio/wav"
            )
        except ConnectionError:
            pass  # 调用方断开，静默；不要让 traceback 刷日志
        except Exception as e:  # noqa: BLE001
            print(f"[CosyVoice TTS] 合成失败: {type(e).__name__}: {e}", flush=True)
            self._send_json(503, {
                "ok": False,
                "msg": f"{type(e).__name__}: {e}",
            })

    def log_message(self, *a):  # 静默默认访问日志
        pass


def _idle_keeper() -> None:
    """空闲到 IDLE_UNLOAD 秒就卸模型、放显存，下次 /tts 再自动加载（懒加载）。"""
    global _COSY, _ERR
    if IDLE_UNLOAD <= 0:
        return
    while True:
        time.sleep(30)
        if _COSY is None or not _LAST_USE:
            continue
        if time.time() - _LAST_USE < IDLE_UNLOAD:
            continue
        with _LOCK:
            if _COSY is None:
                continue
            _COSY = None
            _ERR = None
            try:
                import gc

                import torch

                gc.collect()
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            print(f"[CosyVoice TTS] 空闲 {IDLE_UNLOAD}s，已释放显存", flush=True)


class _QuietHTTPServer(ThreadingHTTPServer):
    """客户端提前断开（WinError 10053/10054）是常态，不打整段 traceback。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def _already_running() -> bool:
    """本端口已有实例就退出。

    面板层虽然有互斥量，但脚本被直接双击/被旧快捷方式拉起时绕过了面板；
    而 Windows 下 allow_reuse_address 允许两个进程绑同一端口，会互相抢请求。
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        return probe.connect_ex(("127.0.0.1", PORT)) == 0
    except OSError:
        return False
    finally:
        probe.close()


def main() -> None:
    if _already_running():
        print(f"[CosyVoice TTS] 端口 {PORT} 已在监听，本进程退出（避免双实例抢请求）")
        return
    server = _QuietHTTPServer(("127.0.0.1", PORT), _Handler)
    print(f"[CosyVoice TTS] listening on http://127.0.0.1:{PORT}")
    print(f"[CosyVoice TTS] model_dir={MODEL_DIR}")
    print(f"[CosyVoice TTS] ref_dir={REF_DIR}（放 ref.wav 可换声线）")
    if _provider() == "openai":
        print(f"[CosyVoice TTS] provider=openai -> {TTS_BASE_URL} model={TTS_MODEL}（不加载本地模型）")
    else:
        print("[CosyVoice TTS] provider=cosyvoice（本地合成）")
    print("[CosyVoice TTS] 首次 /tts 时懒加载模型，请稍候…（加载完前 /health 返回 ready=false）")
    threading.Thread(target=_idle_keeper, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
