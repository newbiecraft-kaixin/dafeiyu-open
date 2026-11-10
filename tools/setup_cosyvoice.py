#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大肥鱼语音引擎（CosyVoice2-0.5B）一次性安装脚本 —— 在本机 Windows 跑一次即可。

做的事：
  1. 建一个独立 venv（不污染 AstrBot 的 Python 3.13 运行时）
  2. 装 torch(CUDA) + CosyVoice2 + modelscope + torchaudio
  3. 从 ModelScope 拉 CosyVoice2-0.5B 模型到 dafeiyu/cosyvoice_models/
  4. 启动本地 TTS 服务（127.0.0.1:8714），AstrBot 的 voice_engine 会自动调用

用法（需要联网 + N 卡；在本机用 py 启动）：
  py setup_cosyvoice.py              # 安装 + 拉模型 + 启动服务
  py setup_cosyvoice.py --no-start   # 只装不启动
  py setup_cosyvoice.py --only-model # 已装好依赖，只补拉模型
  py setup_cosyvoice.py --only-start # 已装好，只启动服务

注意：
  - 默认优先用 py -3.11 建 venv（CosyVoice 在 3.10/3.11 上最稳）；没有就退回当前 Python。
  - torch 走 CUDA 12.1 官方轮子，下载约 2~3GB，请耐心等。
  - 想要「声音更好听」，把一段 3~10 秒你喜欢的干净人声 wav 放到
    dafeiyu/cosyvoice_models/zero_shot_prompt.wav（或在启动服务时设 COSYVOICE_PROMPT_WAV），
    克隆出来的就是那个声音。不放就用模型自带的参考音。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
_AUDIO = os.path.join(os.path.dirname(ROOT), "audio")  # 音频资产统一归 audio/
os.makedirs(_AUDIO, exist_ok=True)
VENV = os.path.join(_AUDIO, "cosyvoice_env")
MODEL_DIR = os.path.join(_AUDIO, "cosyvoice_models", "CosyVoice2-0.5B")
MODEL_ID = "iic/CosyVoice2-0.5B"
# RTX 50 系（Blackwell, sm_120）必须 torch>=2.7 + cu128；
# 老的 2.3.1+cu121 在 5070 上会直接报「no kernel image available」
TORCH_VER = "2.7.1"
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
# CosyVoice2 源码：没装 git 的机器走 codeload 的 zip（不需要 git），
# 装了 git 就优先 git clone（能增量更新）
COSY_REPO_URL = "https://github.com/QwenAudio/CosyVoice.git"
COSY_ZIP_URL = "https://codeload.github.com/QwenAudio/CosyVoice/zip/refs/heads/main"
# Matcha-TTS 是 CosyVoice 的 git 子模块，真身在 shivammehta25 名下（见 .gitmodules）
MATCHA_ZIP_URL = "https://codeload.github.com/shivammehta25/Matcha-TTS/zip/refs/heads/main"
COSY_DIR = os.path.join(_AUDIO, "CosyVoice")
# CosyVoice2 装不上的坑基本都在 WeTextProcessing（pynini 编译）——
# Windows 上用 wetext（纯 Python 的正则规整）替代，CosyVoice 的 frontend 两者都认。
# matcha 的 monotonic_align 只在训练路径（matcha_tts.py）用到，推理链不碰，
# 所以不需要 MSVC 去编译 .pyx。
RUNTIME_DEPS = [
    "numpy<2", "transformers>=4.45,<5", "hyperpyyaml", "omegaconf",
    "conformer==0.3.2", "diffusers", "x-transformers", "lightning",
    "librosa", "soundfile", "scipy", "einops", "inflect", "Unidecode",
    "wetext", "modelscope", "tqdm", "pydantic", "accelerate", "wget", "gdown",
]


def log(*a):
    print("[setup]", *a, flush=True)


def pick_python() -> str:
    """优先 py -3.12/-3.11/-3.10；机器上没有 py 启动器就退回当前解释器。"""
    for ver in ("3.12", "3.11", "3.10"):
        try:
            r = subprocess.run(
                ["py", f"-{ver}", "-c", "import sys; print(sys.version_info[1])"],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            break  # 机器上没有 py 启动器，别再试了
        if r.returncode == 0 and r.stdout.strip():
            log(f"使用 Python {ver} 建 venv")
            return f"py -{ver}"
    log(f"没有 py 启动器，退回当前 Python（{sys.version.split()[0]}）")
    return f'"{sys.executable}"'


def step_repo() -> bool:
    """拿到 CosyVoice 源码 + Matcha-TTS 子模块（third_party）。

    有 git 就 clone（顺带拉子模块）；没有 git 就从 codeload 下 zip 解压 ——
    Matcha-TTS 是子模块，zip 里没有，必须单独下一份放进 third_party/。
    """
    if os.path.isdir(os.path.join(COSY_DIR, "third_party", "Matcha-TTS", "matcha")):
        log("CosyVoice 源码已就位:", COSY_DIR)
        return True
    git = shutil.which("git")
    if git:
        log("git clone CosyVoice（含子模块）...")
        rc = subprocess.run(
            [git, "clone", "--recursive", "--depth", "1", COSY_REPO_URL, COSY_DIR],
            check=False,
        ).returncode
        if rc == 0:
            return True
        log("git clone 失败，改走 zip 下载")
    import urllib.request
    import zipfile

    def fetch_zip(url: str, dest: str, inner_need: str, final_dir: str) -> bool:
        log("下载:", url)
        tmp = dest + ".zip"
        try:
            urllib.request.urlretrieve(url, tmp)
        except Exception as e:  # noqa: BLE001
            log(f"下载失败: {e}")
            return False
        with zipfile.ZipFile(tmp) as z:
            names = z.namelist()
            top = names[0].split("/")[0]
            z.extractall(dest + "_tmp")
        os.rename(os.path.join(dest + "_tmp", top), final_dir)
        os.rmdir(dest + "_tmp")
        os.remove(tmp)
        log("已就位:", final_dir)
        return True

    if not os.path.isdir(COSY_DIR):
        if not fetch_zip(COSY_ZIP_URL, COSY_DIR, "", COSY_DIR):
            return False
    matcha_dir = os.path.join(COSY_DIR, "third_party", "Matcha-TTS")
    if not os.path.isdir(os.path.join(matcha_dir, "matcha")):
        os.makedirs(os.path.dirname(matcha_dir), exist_ok=True)
        if not fetch_zip(MATCHA_ZIP_URL, matcha_dir, "", matcha_dir):
            return False
    return True


def step_torchaudio() -> bool:
    """torchaudio 必须跟 torch 同版本同 CUDA 构建，本地有轮子就用本地。"""
    vpy = os.path.join(VENV, "Scripts", "python.exe")
    for d in (ROOT, os.environ.get("TEMP", "")):
        if not d or not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            lf = f.lower()
            if "torchaudio" in lf and "cu128" in lf and lf.endswith(".whl"):
                p = os.path.join(d, f)
                log("复用本地 torchaudio 轮子:", p)
                return subprocess.run([vpy, "-m", "pip", "install", "-U", p],
                                      check=False).returncode == 0
    log("安装 torchaudio ...")
    return subprocess.run(
        [vpy, "-m", "pip", "install", "-U", f"torchaudio=={TORCH_VER}",
         "--index-url", TORCH_INDEX],
        check=False,
    ).returncode == 0


def find_local_wheel() -> str:
    """已手动下好的 torch 轮子（3GB 重下一次很贵，能复用就复用）。

    优先级：环境变量 COSYVOICE_TORCH_WHEEL > 脚本目录 > Temp。
    文件名不挑，只要是 torch-2.7.1+cu128 的 win_amd64 轮子就行。
    """
    cands = []
    env_p = os.environ.get("COSYVOICE_TORCH_WHEEL", "")
    if env_p:
        cands.append(env_p)
    for d in (ROOT, os.environ.get("TEMP", ""), os.environ.get("TMP", "")):
        if not d or not os.path.isdir(d):
            continue
        try:
            for f in os.listdir(d):
                if f.lower().startswith("torch2") and f.lower().endswith(".whl"):
                    cands.append(os.path.join(d, f))
                elif f.lower().startswith("torch-") and "cu128" in f.lower() and f.endswith(".whl"):
                    cands.append(os.path.join(d, f))
        except OSError:
            continue
    for p in cands:
        if p and os.path.isfile(p) and os.path.getsize(p) > 100 * 1024 * 1024:
            return p
    return ""


def step_install(py_launcher: str) -> bool:
    # ---- 1. 建 venv ----
    if not os.path.isdir(VENV):
        log("创建 venv:", VENV)
        rc = subprocess.run(f'{py_launcher} -m venv "{VENV}"', shell=True).returncode
        if rc != 0:
            log("venv 创建失败")
            return False
    vpy = os.path.join(VENV, "Scripts", "python.exe")
    # ---- 2. 装 torch（CUDA 12.8 轮子，RTX 50 系必须）----
    subprocess.run(
        [vpy, "-m", "pip", "install", "-U", "pip"], check=False
    )
    wheel = find_local_wheel()
    if wheel:
        log(f"复用已下载的 torch 轮子（不再联网重下）：{wheel}")
        subprocess.run([vpy, "-m", "pip", "install", "-U", wheel], check=True)
    else:
        log(f"安装 torch {TORCH_VER}+cu128（下载约 3GB，耐心等）...")
        subprocess.run(
            [vpy, "-m", "pip", "install", "-U",
             f"torch=={TORCH_VER}", f"torchaudio=={TORCH_VER}",
             "--index-url", TORCH_INDEX],
            check=True,
        )
    subprocess.run([vpy, "-m", "pip", "install", "numpy<2"], check=False)
    if not step_torchaudio():
        log("torchaudio 安装失败")
        return False
    # ---- 3. 源码 ----
    if not step_repo():
        log("CosyVoice 源码获取失败")
        return False
    # ---- 4. 运行时依赖（不装 CosyVoice 自带 requirements.txt：里面的
    #      WeTextProcessing/pynini 在 Windows 上编不过，wetext 能顶上）----
    log("安装 CosyVoice 运行时依赖 ...")
    subprocess.run([vpy, "-m", "pip", "install", *RUNTIME_DEPS], check=True)
    return True


def step_model() -> bool:
    vpy = os.path.join(VENV, "Scripts", "python.exe")
    if not os.path.isdir(VENV):
        log("venv 不存在，先跑完整安装（去掉 --only-model）")
        return False
    log("从 ModelScope 拉 CosyVoice2-0.5B 模型 ...")
    code = subprocess.run(
        [vpy, "-c", (
            "from modelscope import snapshot_download; "
            f"snapshot_download('{MODEL_ID}', local_dir=r'{MODEL_DIR}')"
        )],
        check=False,
    ).returncode
    if code != 0:
        log("模型拉取失败（可能网络/CUDA 代理问题），可手动：")
        log(f"  {vpy} -c \"from modelscope import snapshot_download; "
            f"snapshot_download('{MODEL_ID}', local_dir=r'{MODEL_DIR}')\"")
        return False
    log("模型已就位:", MODEL_DIR)
    return True


def step_start() -> bool:
    vpy = os.path.join(VENV, "Scripts", "python.exe")
    if not os.path.isdir(VENV):
        log("venv 不存在，跳过启动（先跑完整安装）")
        return False
    if not os.path.isdir(MODEL_DIR):
        log("模型目录不存在，跳过启动（先拉模型）")
        return False
    log("启动 CosyVoice TTS 服务（127.0.0.1:8714，后台常驻）...")
    # 剥离代理，避免子进程继承 WorkBuddy/系统代理导致连不上本地
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["COSYVOICE_REPO_DIR"] = COSY_DIR  # 服务要靠它找 cosyvoice 源码和 Matcha-TTS
    subprocess.Popen(
        [vpy, "cosyvoice_tts_server.py"],
        cwd=ROOT, env=env,
        creationflags=0x00000008,  # DETACHED_PROCESS-ish；Windows 下用 start 更稳
    )
    # 用 start 更稳，退而求其次直接 Popen
    log("服务已在后台启动；用 http://127.0.0.1:8714/health 查状态")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-start", action="store_true")
    ap.add_argument("--only-model", action="store_true")
    ap.add_argument("--only-start", action="store_true")
    args = ap.parse_args()

    if args.only_start:
        step_start()
        return
    if args.only_model:
        step_model()
        if not args.no_start:
            step_start()
        return

    py_launcher = pick_python()
    if not step_install(py_launcher):
        sys.exit(1)
    if not step_model():
        sys.exit(1)
    if not args.no_start:
        step_start()


if __name__ == "__main__":
    main()
