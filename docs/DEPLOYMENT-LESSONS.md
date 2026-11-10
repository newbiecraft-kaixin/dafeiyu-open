# 部署与踩坑全记录

> 这份文档只写「真机跑过、真出过事」的东西：部署顺序、每一步的验收命令、每一个开源组件的坑，
> 以及出问题时的症状 → 根因 → 处理。文中路径、端口、错误串都来自本机（`C:\Program Files\FlatFish`，
> RTX 5070 Laptop 8GB，Windows 11）。
>
> 通用安装步骤看 [`GETTING-STARTED.md`](GETTING-STARTED.md)；本机既有配置看 [`WINDOWS-DEPLOY.md`](WINDOWS-DEPLOY.md)；
> 本文用于**复盘和排障**，建议每次踩坑后往第 7、8 节补一条。

---

## 1. 总览：进程、端口、数据落在哪

| 服务 | 端口 | 入口 | 说明 |
|---|---|---|---|
| hub 中枢 | 23408 | `/ui`、`/view?target=`、`/capabilities` | 统一面板、服务管理、i18n |
| video 视频 | 23368 | `/ui/parse`、`/ui/feed` | 链接解析 + B 站推荐流 + 扫码登录 |
| audio 音频 | 23366 | `/ui/songs` | 音源、歌曲库、翻唱、ASR、画面描述 |
| subtitle（已并入 audio） | 23405 | — | 语音识别 / 视觉描述，独立端口但同进程 |
| brain 记忆 | 23402 | `/capabilities` | 记忆查询编排 |
| io-wechat | 23392 | — | 微信收发、推桌宠 |
| pet-relay | 23334 | — | 桌宠消息中转 |
| 桌宠宿主 | 3080 | `/dsh-pet-7340/*` | 桌面宠物页面 |
| AstrBot | 6185 / 6199 | 面板 / 反向 WS | 聊天机器人主程序 |
| NapCat | 3000 / 6099 | HTTP / WebUI | QQ 协议端 |
| CosyVoice TTS | 8714 | `/health` | 本地情感 TTS |
| Ollama | 11434 | — | 识图 `qwen2.5vl:3b` |

数据落盘位置（排查时先看这些）：

- `services/video/data/parse.json`：各平台 Cookie（`cookie.douyin` / `kuaishou` / `bilibili`）
- `services/video/data/browser/<platform>/`：扫码登录的持久化浏览器配置
- `services/audio/data/sources.json`：音源开关与登录要求
- `services/audio/data/source_cookies.json`：音乐平台 Cookie
- `services/audio/data/browser/<source>/`：音乐扫码登录的浏览器配置
- `astrbot-win/AstrBot-4.29.3/data/logs/astrbot.log`：AstrBot 主日志（翻唱、插件报错都在这）
- `astrbot-win/AstrBot-4.29.3/data/plugin_data/astrbot_plugin_basic_tools/`：曲库、翻唱缓存、声线
- `astrbot-win/AstrBot-4.29.3/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db`：真实记忆（约 23MB）
- `~/.dsh/sessions/`：桌宠会话记录
- `services/hub/data/manage-*.log`：手动重启服务时重定向出来的日志

---

## 2. 从零部署顺序（按这个顺序做，验收不过不往下走）

### 2.1 机器前置

- Windows 10/11，NVIDIA 驱动（50 系必须 570+，否则 cu128 轮子起不来）
- Python 3.12（本机是 Microsoft Store 版，`C:\Users\<用户>\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_*\python.exe`）
- `ffmpeg` 在 PATH（demucs 分轨、pydub 都依赖它）
- Ollama（识图、embedding）
- Git（拉取上游源码；没有 git 时用 codeload zip）

验收：

```powershell
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
ffmpeg -version | Select-Object -First 1
python -V
```

### 2.2 目录与权限

整个套件放在 `C:\Program Files\FlatFish`。这个位置的副作用：

- 写文件需要管理员权限，服务进程要**以管理员身份启动**，否则改配置会静默失败；
- 配置写入一律用「临时文件 + `os.replace`」，避免半截 JSON：
  ```python
  tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
  os.replace(tmp, path)
  ```

### 2.3 两个 Python 环境（关键）

这套系统**不是一个环境跑到底**，至少两层：

| 环境 | 路径 | 用途 |
|---|---|---|
| 运行时 | Store Python 的 user site-packages | AstrBot、各微服务、Faster-Whisper、CosyVoice、Playwright |
| 推理环境 | `C:\Program Files\FlatFish\seedvc_env` | SoulX-Singer 换声、Seed-VC（CUDA） |

推理环境单独放的原因，见第 3 节和第 4.2 节：SoulX 钉死 `numpy<2`、`transformers` 特定版本，
混进运行时会把 AstrBot / TTS 的依赖顶掉；而且 CUDA 版 torch 单包就 2.5GB。

> **状态（2026-11-09）**：SoulX-Singer 已下线，唱歌换声只保留 Seed-VC（音色相似度优先）。
> 本节及下文 SoulX 相关内容仅作历史排障记录，不代表当前链路。

重建推理环境（本机实测流程）：

```powershell
$py = "C:\Users\<用户>\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe"
& $py -m venv "C:\Program Files\FlatFish\seedvc_env"

$venvPy = "C:\Program Files\FlatFish\seedvc_env\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip setuptools wheel
& $venvPy -m pip install torch==2.7.1 torchaudio==2.7.1 `
    --index-url https://download.pytorch.org/whl/cu128
& $venvPy -m pip install accelerate einops omegaconf "numpy<2" soundfile librosa scipy transformers tqdm
```

验收：

```powershell
& $venvPy -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

必须打出 `2.7.1+cu128 True NVIDIA GeForce RTX 5070 Laptop GPU`。打出 `+cpu` 或 `False` 就等于没装好。

### 2.3.1 第二次「翻唱失败」：找错副本 + 路径根过期 + 空环境（2026-11-08 实复现）

症状：QQ 里翻唱秒失败，日志只有 `[cover] run:  -m demucs -n` 和 `OSError: [WinError 87] 参数错误。`

三个原因叠在一起：

1. **改错了副本**。AstrBot 真正加载的是 `astrbot-win\AstrBot-4.29.3\data\plugins\astrbot_plugin_basic_tools\`；
   仓库同步目录是 `astrbot_plugins\function\astrbot_plugin_basic_tools\`。只改后者，运行中的插件还是旧代码。
2. **`_find` 的查找根过期**。旧代码只查「插件目录上三级 / `~/dafeiyu` / 用户目录」，
   迁移到 `C:\Program Files\FlatFish` 后 `SoulX-Singer`、`seed-vc`、`cosyvoice_ref`、`seedvc_env` 全找不到，
   `COSY_PY` 变成空字符串，子进程直接 WinError 87（日志里 `run:` 后面第一个参数是空的）。
3. **`seedvc_env` 被清空**。只剩 pip/setuptools，没有 torch，即使路径对了 SoulX 也起不来。

修法（已落地）：

- `_find` 改成**从插件目录逐级向上**收集所有上级目录（自然包含 `C:\Program Files\FlatFish`），
  再补 `FLATFISH_HOME`、`~/dafeiyu`、`~` 兜底；
- `COSY_PY` 最后回退 `sys.executable`（运行时解释器里有 demucs，歌曲分离也用它）；
- 按 2.3 重建 `seedvc_env`，然后把 `cover_render.py` **同步到运行目录**。

验收（四行都应指向 FlatFish，`REF_WAV` 必须存在）：

```powershell
$py = "C:\Users\<用户>\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe"
$env:PYTHONPATH = "C:\Program Files\FlatFish\astrbot-win\AstrBot-4.29.3\data\plugins\astrbot_plugin_basic_tools"
& $py -c "import cover_render as c; print(c.COSY_PY); print(c.SEEDVC_PY); print(c.SOULX_ROOT); print(c.REF_WAV)"
```

注意：`cover_render.py` 是**每个翻唱任务临时起一个子进程**跑的，改完下一次翻唱立即生效，
不需要重启 AstrBot；改的是 `helpers.py` / `main.py` 才需要重启。翻唱不到，先跑上面的验收再怀疑模型。

### 2.3.2 第三次「翻唱失败」：demucs 权重缺失 + transformers 版本过高（2026-11-08 实复现）

2.3.1 修完后已经能正常启动 `python -m demucs`，但翻唱还是失败，日志尾部是：

```
urllib.error.URLError: <urlopen error [WinError 10054] 远程主机强迫关闭了一个现有的连接。>
```

两个原因：

1. **demucs 权重没落地**。`~/.cache/torch/hub/checkpoints/` 是空的，demucs 现去
   `dl.fbaipublicfiles.com` 下载 `htdemucs`，被网络重置。
   权重文件名是 `955717e8-8726e21a.th`（84,141,911 字节），`torch.hub` 会按文件名里的哈希做
   `check_hash`（sha256 前 8 位必须是 `8726e21a`），改错名或文件残缺都会被判失败。
2. **`seedvc_env` 里 `transformers` 太新**。SoulX 自带一份老版 `LlamaAttention` 调用方式：
   `transformers 4.57` 起 `LlamaAttention.forward()` 强制要求 `position_embeddings`，直接
   `TypeError: LlamaAttention.forward() missing 1 required positional argument: 'position_embeddings'`；
   `transformers 5.x` 更早，在建模型阶段就 `LlamaConfig accepts only keyword arguments`。

修法（已落地）：

```powershell
# 1) 离线放权重（源站被重置时用 hf-mirror 上同一份文件）
$dst = "$env:USERPROFILE\.cache\torch\hub\checkpoints\955717e8-8726e21a.th"
New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
curl.exe -L -o $dst "https://hf-mirror.com/anchor-flux/demucs-htdemucs/resolve/main/htdemucs.th"
(Get-FileHash $dst -Algorithm SHA256).Hash   # 必须以 8726E21A 开头
```

```powershell
# 2) 推理环境降到 SoulX 能跑的组合，并补齐 Seed-VC 回退缺的包
$venvPy = "C:\Program Files\FlatFish\seedvc_env\Scripts\python.exe"
& $venvPy -m pip install "transformers==4.46.3" munch descript-audio-codec
& $venvPy -m pip install "numpy==1.26.4"
```

注意：`descript-audio-codec` 会把 `numpy` 抬到 2.x 并塞进一堆绘图/notebook 依赖；
装完必须把 `numpy` 钉回 1.26.4，并删掉 `site-packages` 下 pip 留下的 `~umpy`、`~umpy.libs` 残留目录，
否则下次装包还会踩到 numpy 2 的坑。

验收（SoulX 官方 SVC 示例，进度条 4 段应全部跑完）：

```powershell
$env:SOULX_WHISPER_PATH = "C:\Program Files\FlatFish\SoulX-Singer\pretrained_models\whisper-base"
$env:PYTHONPATH = "C:\Program Files\FlatFish\SoulX-Singer"
& $venvPy -m cli.inference_svc --device cuda --model_path pretrained_models\SoulX-Singer\model-svc.pt `
  --config soulxsinger\config\soulxsinger.yaml `
  --prompt_wav_path example\audio\zh_prompt.mp3 --target_wav_path example\audio\music.mp3 `
  --prompt_f0_path example\audio\zh_prompt_f0.npy --target_f0_path example\audio\music_f0.npy `
  --save_dir example\generated\music_svc --auto_shift --pitch_shift 0 --fp16
```

出现 `example\generated\music_svc\generated.wav` 即算过。真实翻唱的最终验收是 job 输出
`{"ok": true, "duration": 40.0, "seconds": 54, "holes": 0}`（40 秒高潮段 54 秒左右出成品）。

`SOULX_WHISPER_PATH` 一定要设：不设时 `WhisperFeatureExtractor` 会去 HuggingFace 拉
`openai/whisper-base`，国内网络表现为「模型加载到最后一步才失败」（SSL 证书校验错误）。

### 2.4 模型与素材

- `SoulX-Singer/pretrained_models/`：`SoulX-Singer/model-svc.pt`、`config.yaml`、`SoulX-Singer-Preprocess/rmvpe/rmvpe.pt`、`whisper-base`
- `CosyVoice` + `cosyvoice_models`（TTS 权重）
- `audio/cosyvoice_ref/profiles/<声线>/ref.wav + prompt.txt`（5 组参考音）
- Ollama `qwen2.5vl:3b`（约 3.2GB）
- `demucs` 权重（`htdemucs`，国内必须离线放，见 2.3.2；首次运行自动下载会失败）

### 2.5 启动顺序

```bat
installer\start-astrbot-win.bat
installer\start-napcat-win.bat
installer\start-cosyvoice-tts.bat
```

微服务由 hub 拉起（或手动起，见 6.2）。验收用一条命令扫全部端口：

```powershell
23366,23368,23402,23405,23392,23334,3080,6185,6199,3000,6099,8714 |
  ForEach-Object {
    $ok = (Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue) -ne $null
    "{0} {1}" -f $_, $(if ($ok) { "LISTEN" } else { "MISSING" })
  }
```

### 2.6 首次登录

- QQ：`http://127.0.0.1:6099/webui`，token 见 `napcat/config/webui.json`
- 抖音/快手/B 站：视频页点登录，**一定选带窗口的方式**（见 4.9）
- 酷狗 / QQ 音乐 / 网易云：歌曲页音源卡片点「扫码登录」（咪咕不需要）

---

## 3. 迁移事故复盘：venv 没跟着搬家，翻唱全挂

### 3.1 现象

用户提问「翻唱为何失败」。AstrBot 日志（`data/logs/astrbot.log`，01:13:33）：

```
[cover] unloaded ollama qwen2.5vl:3b
[cover] run:  -m demucs -n
[cover] 失败: OSError: [WinError 87] 参数错误。
```

`翻唱失败: {'ok': False, 'error': 'OSError: [WinError 87] 参数错误。'}`

### 3.2 根因

`run:  -m demucs -n` 里**第一个参数是空的**——本该是 Python 解释器路径。
`cover_render.py` 里：

```python
COSY_PY = _find("cosyvoice_env/Scripts/python.exe") or os.environ.get("COSYVOICE_PY", "")
SEEDVC_PY = _find("seedvc_env/Scripts/python.exe") or COSY_PY
SOULX_PY = SEEDVC_PY
```

项目从 `C:\Users\<用户>\dafeiyu` 迁到 `C:\Program Files\FlatFish` 时，两个 venv 目录没搬过来
（venv 里有几 GB 的 torch，通常被清理或忽略）。于是：

- `_find(...)` 返回空 → `COSY_PY` 为空 → `run(["", "-m", "demucs", ...])` → WinError 87；
- 就算分轨好了，`SOULX_PY` 也是空；而 SoulX 的调用里 `--device cuda` 是硬编码的；
- 运行时那份 torch 是 `2.14.0+cpu`，且缺 `accelerate`。

### 3.3 修复

1. `COSY_PY` 增加兜底（已改，[cover_render.py](../astrbot_plugins/function/astrbot_plugin_basic_tools/cover_render.py)）：
   ```python
   COSY_PY = (
       _find("cosyvoice_env/Scripts/python.exe")
       or os.environ.get("COSYVOICE_PY", "")
       or sys.executable  # 独立环境缺失时退回当前解释器
   )
   ```
2. 运行时补 `demucs`（CPU 也能分轨，只是慢）：
   ```powershell
   & $py -m pip install demucs
   ```
3. 重建 `seedvc_env`（见 2.3），并把 SoulX 的 device 改成自适应：
   ```python
   use_cuda = torch.cuda.is_available()
   cmd = [SOULX_PY, "-m", "cli.inference_svc",
          "--device", "cuda" if use_cuda else "cpu", ...]
   if use_cuda:
       cmd.append("--fp16")
   ```

### 3.4 教训

- **venv 不可迁移**：换机器/换目录必须重建，别指望复制粘贴能用（路径写死在 `pyvenv.cfg` 和脚本里）。
- **启动自检要校验解释器存在**，不能等用户在业务路径上撞 `WinError 87`。
- **报错要能看懂**：空的命令第一个参数在日志里只表现为 `run:  -m demucs`，很难第一眼看出问题。
  子进程封装里应该在 `cmd[0]` 为空时直接抛「推理环境缺失：seedvc_env 未安装」。
- 部署文档必须写清「哪几个 venv、分别装什么、怎么验收」。

---

## 4. 开源组件：逐个说坑

### 4.1 CosyVoice2（TTS）

- **WeTextProcessing 在 Windows 编不过**（依赖 `pynini`/OpenFst）。官方脚本里已改用 `wetext`。
- **Matcha-TTS 子仓库**不会被 codeload zip 带上，需要单独下到 `tools\` 下再指路径。
- 50 系显卡必须 **torch 2.7.1 + cu128**；cu121 轮子直接报 `no kernel image is available for execution on the device`。
- 服务独立进程（8714）、模型常驻显存；AstrBot 侧只做 HTTP 调用，失败自动回退 edge-tts（不报错）。
- 声线克隆只需要 3~10 秒干净人声；参考音放 `audio\cosyvoice_models\zero_shot_prompt.wav` 或设 `COSYVOICE_PROMPT_WAV`。

### 4.2 SoulX-Singer（换声/翻唱核心）

- `requirements.txt` 钉的是 `torch==2.2.0`，**和 50 系不兼容**；实际用 `torch 2.7.1+cu128` 跑通。
- `--device` 默认按 cuda 写死；没有 CUDA 时必须显式 `cpu`，且要**去掉 `--fp16`**（CPU 不支持半精度）。
- `numpy<2` 是硬要求，numpy 2.x 会在音频处理里直接炸。
- `transformers` 版本敏感：**必须 ≤ 4.46.x**（仓库钉 4.41.2，实测 4.46.3 可用）。
  4.57 起 `LlamaAttention.forward()` 强制要求 `position_embeddings`，SoulX 自带的老调用方式直接 `TypeError`；
  5.x 在建模型阶段就 `LlamaConfig accepts only keyword arguments`。升级前先跑 8.2 的验收。
- `SOULX_WHISPER_PATH` 必须指向本地 `pretrained_models\whisper-base`，否则会去 HuggingFace 拉模型并 SSL 失败。
- 缺 `accelerate` 会在 `soulxsinger/models/modules/vocoder.py` 直接 `ModuleNotFoundError`。
- 8GB 显存够用，但要先卸 Ollama（见 4.8），否则 OOM。
- 推理会把成品的 `generated.wav` 写在 `outdir/svc/`，没这个文件就是失败（源码里不做兜底）。

### 4.3 Seed-VC

- `ft_model_*.pth` 是 **Seed-VC 的训练产物**，不是 TTS 权重；TTS（CosyVoice）根本不加载它们。
- 之前面板上标「Seed-VC 权重（TTS 未接入）」属于语义误导，已整条移除：前端不渲染、后端不返回、i18n 词条删除。
- 教训：**面板上的技术提示要么准确要么不显示**，含糊的标注会被当成 bug 反复追问。

### 4.4 demucs

- 包名 `demucs`，用法 `python -m demucs`；不要指望 `demucs.exe` 在 PATH 里（Windows 上它装在 `...\Scripts`，默认不在 PATH）。
- 分轨命令：`-n htdemucs --two-stems vocals`，产出 `htdemucs/<曲名>/vocals.wav` 与 `no_vocals.wav`。
- 依赖 ffmpeg 解码；没有 ffmpeg 会在 mp3 输入上失败。
- CPU 也能跑，但一首歌分钟级起步；有缓存（`STEM_CACHE_DIR`）命中会跳过，别轻易清缓存。
- 4.1.0 在当前 torch 上可用；装完记得 `import demucs.separate` 验收。
- 权重 `htdemucs` 首次运行会联网下载，国内常被重置（`urllib.error.URLError: [WinError 10054]`）。
  离线放法：`~\.cache\torch\hub\checkpoints\955717e8-8726e21a.th`，84,141,911 字节，
  sha256 以 `8726e21a` 开头（`torch.hub` 按文件名做 `check_hash`，名字不对就不认）。
  镜像地址：`https://hf-mirror.com/anchor-flux/demucs-htdemucs/resolve/main/htdemucs.th`，详见 2.3.2。

### 4.5 faster-whisper（ASR / 歌词对齐）

- 统一用 `device="cpu", compute_type="int8"`，别和翻唱抢显存。
- 模型首次使用会从 HuggingFace 下载，国内网络要配镜像或预置模型目录，否则表现为「卡住不动」。

### 4.6 AstrBot

- **`No module named 'data.plugins'`**：site-packages 里存在第三方 `data` 包，遮蔽了 AstrBot 自己的 `data/` 目录，
  导致所有插件导入失败、QQ/微信消息不再推桌宠。
  修复：在 `AstrBot-4.29.3\data\` 和 `data\plugins\` 各放一个 `__init__.py`，让本地包优先。
- 插件加载失败是**静默灾难**：消息照收，但所有插件能力消失。面板和日志都要看。
- 重启方式：不要依赖中枢 `/manage restart`（见 6.1），直接杀掉进程再起，**必须带工作目录**：
  ```powershell
  Start-Process -FilePath $py -ArgumentList 'main.py' `
    -WorkingDirectory 'C:\Program Files\FlatFish\astrbot-win\AstrBot-4.29.3' -WindowStyle Hidden
  ```
- 日志是 cp936 写入的；用 PowerShell 直接 `Get-Content` 看中文可能乱码，必要时指定 `-Encoding Default`。

### 4.7 NapCat

- 只配反向 WS（6199）时机器人只能被动回复；hub 主动发消息会报「napcat 不在线」。
  必须同时配 **HTTP 服务器 3000 + 同一个 token**。
- 登录态靠 QQ 扫码；换号要清 `napcat/config` 下的登录缓存。

### 4.8 Ollama

- 翻唱/换声前必须腾显存。`cover_render.py` 的做法值得抄：**走本地 HTTP 卸载模型**，不要执行 `ollama ps`。
  原因：守护进程没起时 `ollama ps` 会把 Ollama 桌面程序拉起来，子进程继承管道不退出，把翻唱卡死在第一步。
- 卸载后不用手动重载，Ollama 会按需加载。

### 4.9 Playwright 扫码登录（抖音 / 快手 / B 站 / 酷狗 / QQ 音乐 / 网易云）

这是踩得最惨的一块，结论先给：

> **扫码登录一律用「有头 + 持久化 profile」，不要 headless，不要伪造 User-Agent。**

- 抖音实测：扫码后跳转「身份验证/安全验证」，需要人点。headless 没有窗口 → 永远等不到
  `sessionid / sessionid_ss / sid_tt`，表现就是「扫了码没反应」，日志里 `cookies=44` 但一个登录态都没有。
  改 `launch_persistent_context(headless=False, ...)` 后一次就过。
- 持久化目录：`data/browser/<platform>`。第二次登录能直接沿用 Cookie，不用重扫。
- 浏览器回退顺序：`msedge` → `chrome` → 默认 chromium。企业/精简系统里未必有前两者。
- **不要覆盖 UA**：伪造 UA 和真实指纹不一致会加重风控，也会让某些站点的 Cookie 判定逻辑错乱。
- 登录判据看 Cookie 名，不要看页面上有没有头像：
  - 抖音：`sessionid` / `sessionid_ss` / `sid_tt`
  - 酷狗：`token` / `userid` / `kg_music_token`
  - QQ 音乐：`qm_keyst` / `qqmusic_key` / `wxskey` / `p_skey`
  - 网易云：`MUSIC_U`
- 二维码抓取：遍历 frame，找边长 110~360、宽高差 <24 的 `img/canvas`，**截元素图**而不是读 `src`。
  这样 `data:`、远程 URL、canvas 三种情况都能覆盖。
- 二维码有效期给 300 秒，期间每 10 秒打一次 `cookies=N url=...`，出问题一眼能看出卡在哪一步。
- 同一平台重复点登录要先取消旧会话，否则两个浏览器抢同一个 profile 会报锁文件占用。

### 4.10 音乐平台接口

- **QQ 音乐**：新版 `client_search_cp` 对未登录请求返回空列表（`data.song.list` 为空，不报错）。
  改用仍可用的 `search_for_qq_cp`；字段是 `songmid / songname / singer[] / albumname / interval`。
  播放直链要 `vkey.GetVkeyServer` + `uin`（从 Cookie 里解析），未登录拿不到。
- **网易云**：`api/search/get/web` 搜，`api/song/enhance/player/url` 取直链；`MUSIC_U` 决定能不能拿到 VIP 曲。
  `duration` 是毫秒，注意换算。
- **酷狗**：`songsearch.kugou.com/song_search_v2` + `m.kugou.com/app/i/getSongInfo.php?cmd=playInfo` 二次取链。
  免费曲不登录也有直链，但按产品要求仍统一走扫码门禁。
- **咪咕**：不要求登录，但直链受版权限制，经常为空——这是正常的，不要当 bug 修。
- 统一门禁：`login=required` 的音源在搜索前检查 Cookie，没有就返回 `403 音源需要先扫码登录：xxx`，
  前端提示去卡片点「扫码登录」。

### 4.11 微信（wechatauto）

- `contact.db` 是 SQLCipher 加密库，第三方库只暴露昵称/备注，不带头像。
- 头像缓存在 `db_storage\head_image\head_image.db`，同样加密，需要同一把密钥才能解，目前没实现。
- 现状：QQ 用 `q1.qlogo.cn` 真实头像，微信只能用昵称首字占位。别在 UI 上假装有头像。

---

## 5. Windows 平台专属坑

| 坑 | 现象 | 处理 |
|---|---|---|
| Program Files 写权限 | 配置改了没生效、无报错 | 管理员启动；写入用 tmp + `os.replace` |
| Store Python 路径 | `sys.executable` 指向 WindowsApps 目录 | 一律用 `sys.executable`，别拼 `python.exe` |
| 中文编码 | 子进程输出乱码、日志里中文变问号 | `subprocess` 显式 `encoding`；读日志带编码 |
| 空格路径 | `Start-Process` 参数被拆开 | `-WorkingDirectory` 传工作目录，路径加引号 |
| 端口占用不报错 | 新进程起来但旧进程还在服务 | 起之前 `Get-NetTCPConnection -LocalPort` 查占用 |
| 浏览器 profile 锁 | 第二次登录 `Target page, context or browser has been closed` | 先取消旧会话，必要时清 `SingletonLock` |
| 长路径 | 模型/缓存路径过长报错 | 保持根目录短路径，别嵌套太深 |
| 杀毒/索引 | 浏览器 profile 文件被占用 | 把 `data/browser` 加白名单 |

---

## 6. 服务编排的坑

### 6.1 中枢 `/manage` 不可靠

- `POST /manage restart` 报 `'NoneType' object has no attribute 'splitlines'`（拿不到子进程输出）。
- `stop` 返回 400 且不回错误正文。
- 结论：**重启服务用手动方式**，并把 stdout/stderr 重定向到文件，否则新增的 `print` 看不到。

### 6.2 手动重启模板

```powershell
$py = "C:\Users\<用户>\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe"

# 找到现有监听进程并杀掉
$pid = (Get-NetTCPConnection -LocalPort 23366 -State Listen).OwningProcess
Stop-Process -Id $pid -Force

# 带工作目录重启，日志重定向
Start-Process -FilePath $py -ArgumentList 'service.py' `
  -WorkingDirectory 'C:\Program Files\FlatFish\services\audio' `
  -RedirectStandardOutput 'C:\Program Files\FlatFish\services\hub\data\manage-audio.log' `
  -RedirectStandardError  'C:\Program Files\FlatFish\services\hub\data\manage-audio.err.log' `
  -WindowStyle Hidden

# 等 5~8 秒再验收
(Invoke-WebRequest 'http://127.0.0.1:23366/health' -UseBasicParsing).Content
```

video（23368）、audio（23366）、brain（23402）、io-wechat（23392）都套这个模板，只换目录、日志名和端口。

### 6.3 服务发现与 `/view`

- hub 通过扫服务日志里的 token 探活；`/view?target=` 会补 token 后跳转。
- 旧的桌宠窗口服务 23335 已下线，现由 3080 承载；`/view?target=...:23335/health` 打不开是**正常**，
  不是故障。

### 6.4 端口/接口变更要同步三处

1. `services/<svc>/service.json` 的 `endpoints` / `responsibility`；
2. hub 面板卡片文案与链接；
3. 本文第 1 节的总览表和验收脚本。

漏掉任何一处，下次排障都会被过期信息误导。

---

## 7. 症状 → 根因 → 处理（速查）

| 症状 | 根因 | 处理 |
|---|---|---|
| 抖音扫码后没反应 | headless 无法完成安全验证，拿不到 sessionid | 改有头持久化浏览器，见 4.9 |
| 翻唱失败 `WinError 87 参数错误` | `COSY_PY` 为空，子进程第一个参数是空串 | 重建 `seedvc_env`；`COSY_PY` 兜底 `sys.executable` |
| 翻唱失败 `URLError: [WinError 10054]` | demucs 权重缺失，联网下载被重置 | 离线放 `955717e8-8726e21a.th` 到 torch 缓存，见 2.3.2 |
| SoulX 报 `LlamaAttention.forward() missing ... position_embeddings` | `transformers` ≥4.57 与 SoulX 自带旧实现不兼容 | `pip install transformers==4.46.3`，见 2.3.2 |
| `/余额` 发了完全没反应 | 路由插件按消息时序切 provider，命令先到 → 会话判定不是 DeepSeek → 静默吞掉 | 见第 12 章 |
| 翻唱卡在第一步不动 | `ollama ps` 拉起桌面程序，子进程继承管道不退出 | 改用 HTTP 卸载模型 |
| QQ/微信消息不推桌宠 | 插件全挂：`No module named 'data.plugins'` | 在 `data/`、`data/plugins/` 放 `__init__.py` 后重启 |
| 歌曲搜索 403 | 音源要求登录但没扫码 | 卡片点「扫码登录」，检查 `source_cookies.json` |
| 二维码接口 425 | 二维码还没截到 | 等 1~2 秒重试；超过 45 秒看浏览器窗口 |
| 二维码接口 410 | 会话过期/出错 | 重新点扫码登录 |
| 记忆中枢看不到数据 | brain 库为空，真实记忆在 livingmemory.db | 看 `plugin_data/astrbot_plugin_livingmemory/livingmemory.db` |
| `/view?...:23335/health` 打不开 | 23335 已下线 | 改用 3080 |
| 面板出现「Seed-VC 权重（TTS 未接入）」 | 语义误导 | 已移除，前后端与 i18n 都不再有该词条 |
| 重启后改动不生效 | 中枢 `/manage restart` 失效 | 手动 kill + `Start-Process`，确认 PID 变了 |
| 浏览器 profile 被占用 | 上一个扫码会话没退出 | 先取消旧会话再开新的 |
| 回复里仍有「。」 | 句号按 `drop_period_prob` 概率删、只处理句末，长文还整段跳过 | 改确定性清洗，见第 13 章 |

---

## 8. 自检脚本（出问题先跑这个）

### 8.1 环境自检

```powershell
$py = "C:\Users\<用户>\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe"
$venvPy = "C:\Program Files\FlatFish\seedvc_env\Scripts\python.exe"

"--- 运行时 ---"
& $py -c "import sys, importlib.util as u
print('exe', sys.executable)
for m in ('torch','torchaudio','transformers','demucs','soundfile','librosa','playwright'):
    print(m, bool(u.find_spec(m)))"

"--- 推理环境 ---"
if (Test-Path $venvPy) {
  & $venvPy -c "import torch; print(torch.__version__, torch.cuda.is_available())"
} else { "seedvc_env 缺失：翻唱会失败" }
```

### 8.2 翻唱链路自检

```powershell
$venvPy = "C:\Program Files\FlatFish\seedvc_env\Scripts\python.exe"
$env:PYTHONPATH = "C:\Program Files\FlatFish\SoulX-Singer"
& $venvPy -c "import accelerate, einops, omegaconf, numpy; print('deps ok', numpy.__version__)"
& $venvPy -c "import transformers; print('transformers', transformers.__version__)"   # 必须是 4.46.3
& $venvPy -c "import cli.inference_svc; print('soulx import ok')"
Test-Path "$env:USERPROFILE\.cache\torch\hub\checkpoints\955717e8-8726e21a.th"        # 必须是 True
```

四行都通过，翻唱才有可能跑通；哪一行报 `ModuleNotFoundError`，就把对应包补进 `seedvc_env`。
`numpy` 打出 2.x 或 `transformers` 打出 4.5x，都要按 2.3.2 钉回兼容版本。

### 8.3 端口/健康检查

```powershell
$map = @{ 23366='/health'; 23368='/health'; 23402='/health'; 23392='/health'; 3080='/health' }
foreach ($p in $map.Keys) {
  try { $r = Invoke-WebRequest "http://127.0.0.1:$p$($map[$p])" -UseBasicParsing -TimeoutSec 5
        "$p OK $($r.StatusCode)" }
  catch { "$p FAIL $($_.Exception.Message)" }
}
```

---

## 9. 已知限制与待办

- 微信好友真实头像需要解密 `head_image.db`（SQLCipher），暂未实现，UI 用首字占位。
- 咪咕直链受版权限制，经常拿不到可播放地址，属正常。
- QQ 音乐 / 网易云的 VIP 曲直链依赖登录态，Cookie 过期后需要重扫。
- 中枢 `/manage` 的 restart/stop 仍不可靠，属于待修项。
- 翻唱的「消息里多余换行」问题尚未定位到制造方，需要一条真实收到的原文样例。

---

## 10. 写文档的规矩

- 每修好一个坑，往第 7 节补一行「症状 → 根因 → 处理」，比只改代码有用。
- 错误串**原样贴**（`WinError 87`、`No module named 'data.plugins'`），别翻译成「报错了」。
- 涉及环境的改动，写清**在哪个解释器里装、装完怎么验收**，否则下次迁移还会重演第 3 节。
- 文案类问题（如 Seed-VC 标注）也要记：它们消耗的沟通成本不比代码 bug 低。

---

## 11. 中枢面板（dashboard.html）改版：部署与验收

面板是**纯静态单文件**：`services/hub/hubsrv/dashboard.html`，由 hub（23408）的 `/ui` 直接返回。
hub **每次请求都重新读盘**，所以改完样式不需要重启任何进程，浏览器刷新就能看到。

### 11.1 正确流程（按这个顺序做）

1. 认准**两份文件**，别改错地方：

   | 位置 | 作用 |
   |---|---|
   | `C:\Program Files\FlatFish\services\hub\hubsrv\dashboard.html` | 本机实际在跑的那份（写文件要管理员权限） |
   | `<仓库>\services\hub\hubsrv\dashboard.html` | GitHub 上的源码副本 |

   只改安装目录 → 本机生效但下次重装/覆盖安装会丢；只改仓库 → GitHub 有了但本机看不到。
   **改完必须把同一份文件同步回仓库**：

   ```powershell
   Copy-Item 'C:\Program Files\FlatFish\services\hub\hubsrv\dashboard.html' `
             'C:\Users\<用户>\IdeaProjects\dafeiyu-open\services\hub\hubsrv\dashboard.html' -Force
   ```

2. 只动前端这一个文件，不改 `/capabilities`、`/manage` 等后端接口和 DOM 结构里的 id；
   头像、群卡片「配置」、服务启停这些既有交互必须原样保留。

3. 改完先做语法自检（页面里有两段 `<script>`，取第一段内联 JS）：

   ```powershell
   $t = Get-Content 'C:\Program Files\FlatFish\services\hub\hubsrv\dashboard.html' -Raw
   $i = $t.IndexOf('<script>'); $j = $t.IndexOf('</script>')
   Set-Content "$env:TEMP\dash_check.js" -Value $t.Substring($i + 8, $j - $i - 8) -Encoding UTF8
   node --check "$env:TEMP\dash_check.js"
   ```

4. 再验「真的生效了」——看 `/ui` 的状态码和响应里有没有新标记：

   ```powershell
   $r = Invoke-WebRequest 'http://127.0.0.1:23408/ui' -UseBasicParsing
   $r.StatusCode                    # 200
   $r.Content -match 'bg-photo'     # True = 新样式已经发出去了
   ```

5. 浏览器 **`Ctrl+F5` 强刷**。静态页会被缓存，普通刷新经常还是旧样式，会误判成「改了没用」。

### 11.2 视觉改版的三条硬约束（都踩过）

- **别给会自动重绘的节点加入场动画**。页面每 5 秒拉一次 `/capabilities` 并整体重绘
  `.layer / .svc / .tool`；这些元素上的 `animation` 会每 5 秒重播一次，表现为「面板一直在闪」。
  动效只加在静态区：头部、统计卡、Logo、背景层。
- **`backdrop-filter` 只给大容器**（`.top / .bot / .layer / .toolbar / .modal-card`）。
  几十张小卡片同时磨砂，再叠一个还在动的背景，滚动会明显掉帧。
- **动画必须有降级**：全部动效放进 `@media (prefers-reduced-motion: reduce)` 统一关闭；
  跟随鼠标的光晕在 `(hover: none)` 或减少动效时直接不初始化。

### 11.3 背景图与离线兜底

- 网页用图统一走 `https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt=...&image_size=...`，
  `image_size` 只能取 `square_hd / square / portrait_4_3 / portrait_16_9 / landscape_4_3 / landscape_16_9`。
- 图片是外链，**必须给离线兜底**：底色仍然用 CSS 渐变，图片只作增强层（压暗 + 半透明）。
  否则断网/域名不可达时面板会退化成纯黑。
- 想确认图片本身可达，直接请求一次看 `Content-Type`：

  ```powershell
  (Invoke-WebRequest '<图片URL>' -UseBasicParsing).StatusCode   # 200
  ```

### 11.4 症状 → 根因 → 处理

| 症状 | 根因 | 处理 |
|---|---|---|
| 改完刷新没变化 | 浏览器缓存了 `/ui` | `Ctrl+F5`；并用 11.1 第 4 步确认响应里有新标记 |
| 面板每 5 秒闪一下 | 入场动画加在了自动重绘的卡片上 | 动画只放静态节点 |
| 滚动发卡、掉帧 | 小卡片也开了 `backdrop-filter` | 模糊只留大容器 |
| 背景一片黑 | 背景图域名不可达且没有兜底 | 保留 CSS 渐变兜底层 |
| `/ui` 404 或还是旧版 | hub 没起，或改到了另一份文件 | 先 `Invoke-WebRequest http://127.0.0.1:23408/ui` 验活，再核对 11.1 的两份路径 |
| 重装后样式全丢 | 只改了安装目录，没同步仓库 | 按 11.1 第 1 步把文件同步回仓库并提交 |

---

## 12. 插件实战坑：`/余额` 发了完全没反应（2026-11-08）

现象：管理员在 QQ 里发 `/余额`，机器人一个字都不回；同一时间别人发普通消息、别的命令都正常。

排查顺序：

1. 先看是不是被 AstrBot 的权限门拦了。日志里会有明确文案：
   `您(ID: xxx)的权限不足以使用此指令。通过 /sid 获取 ID 并请管理员添加。`
   有这句就是 `data/cmd_config.json` 的 `admins_id` 里没有这个人，跟插件无关。
2. 没有权限文案、也没有任何回复 → 是插件自己 `event.stop_event()` 静默了。
   `human_sim` 的 `cmd_balance` 有两道静默门：`_is_manager()`（读 `admins_id`）和 `_recharge_allowed()`。

根因：`_recharge_allowed()` 用 `_deepseek_session()` 判断「当前会话是不是走 DeepSeek 官方 key」，
取的是 `get_using_provider(umo)` 的**实时值**。但 `provider_router` 是「收到消息才切 provider」，
`/余额` 作为命令会先于路由插件到达，看到的是上一轮残留的 provider——日志里同一私聊
02:20:56 切到 `deepseek`、02:20:57 又切回 `ollama-chat/qwen3.5-9b`，于是判定失败、静默返回。

修法：

- `_deepseek_session()` 增加兜底：直接读
  `data/plugins/astrbot_plugin_provider_router/config.json`，私聊看 `private_provider_id`、
  群聊看 `group_provider_id` / `group_provider_chain`；并且**只对 `admins_id` 里的管理员放行**，
  非管理员行为不变，避免把余额暴露给普通群友。
- `cmd_balance` 真正查不了时回一句「这个会话没走 DeepSeek 官方 key，余额查不了（本地模型不烧钱）」，
  不再装死——**命令没有任何反馈是最难排查的 bug**。

改的是插件 Python 代码，不是子进程，**必须重载插件才生效**。不想重启 AstrBot 时可以直接打面板 API：

```powershell
# jwt_secret 在 data/cmd_config.json 里；面板默认 6185
$tok = & $py -c "import jwt,datetime,sys; print(jwt.encode({'username':'astrbot','exp':datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=1)}, sys.argv[1], algorithm='HS256'))" <jwt_secret>
Invoke-RestMethod -Uri "http://127.0.0.1:6185/api/v1/plugins/astrbot_plugin_human_sim/reload" `
  -Method Post -Headers @{ Authorization = "Bearer $tok" }
```

返回 `{"status":"ok","message":"重载成功。"}`，日志里出现
`Plugin astrbot_plugin_human_sim (v1.4.2) ...` 即生效。
注意接口前缀是 `/api/v1`（`/api/plugins/...` 会 405，那是另一套 legacy 路由）。

---

## 13. 插件实战坑：回复里仍然带句号（2026-11-10）

现象：`humanizer` 开着，机器人回复仍会冒出「。」——短句、多句、长文都出现过，而且不是每条都有。

排查：

1. 输出有两条链路，都要看：`humanizer` 的 `on_llm_response` / `on_decorating_result`；`human_sim` 主动插话走自己的 cleaner（`human_sim_core/runtime.py::_load_cleaner` 动态加载同一份 `cleaner.py`）。
2. `cleaner.py::_final_touches()` 里，句号是按 `drop_period_prob` 概率删的：默认 0.75，`human_sim` 配 0.85，运行时配置甚至被改成 1.0。
3. 两个盲区：正则只匹配句末 `。$`，句中句号从不处理；回复超过 `long_bypass_chars`（默认 150 字）走长文分支，整段跳过 `_final_touches`。

根因：句号清洗是「概率 + 只收尾」，不是确定性规则——同一段文字可能这次留、下次不留，长文则必留。

修法：

- `_final_touches()` 改成确定性：`。+` 一律换 `，`，再清掉终止标点后面多余的逗号，句尾的 `[，,、；;]+` 直接裁掉，开头孤立逗号也去掉。
- `humanize()` 在 `_restore` 之后补一条 `re.sub(r"。$", "", restored)`，长文也不拿句号收尾。
- 删除 `drop_period_prob`：`cleaner.py` 的 `DEFAULT_CFG`、`_conf_schema.json`、运行时 `data/config/astrbot_plugin_humanizer_config.json`、`human_sim_core/runtime.py` 四处同时去掉，避免旧配置继续生效。
- `:表情名:`、`[表情文字]`、URL、代码块、行内代码、@提及都在 vault 里受保护，替换句号不会波及。

验收（不启 AstrBot，直接 import cleaner）：

```text
今天有点累。              -> 今天有点累
先吃饭。再洗澡。你呢？     -> 先吃饭，再洗澡，你呢？
记住了 :害羞: 下次不犯。   -> 记住了 :害羞: 下次不犯
```

口语噪声是随机注入的，同一句可能多一个「诶 / 啊这」前缀，属正常。

生效：插件在启动时加载 `cleaner.py`，**必须重启 AstrBot**；不能重启时按第 12 章的面板 reload 接口分别重载 `astrbot_plugin_humanizer` 与 `astrbot_plugin_human_sim`。

## 第 14 章 语音转写全线 500：离线模型加载 + 取语音 schema（2026-11-10）

现象：私聊/群里的语音消息在上下文里是空串（`[ComponentType.Record]`），机器人只能回「（把耳朵凑过去）再来一遍」；管理员账号的聊天记录看起来全是无效内容。AstrBot 核心日志报 `Speech-to-text failed: HTTP Error 500`，插件侧报 `语音转写失败: ... Expected required property`。

排查：

1. 直接打转写服务复现：`POST http://127.0.0.1:23405/asr {"path": 本地wav}` 返回 500，`error` 里是 `SSLError: HTTPSConnectionPool(host='huggingface.co', port=443) ... /api/models/Systran`。
2. 此时 `/health` 是正常的（`asr_loaded: false`）——服务活着，只有**首次加载模型**那一步炸；权重其实早就缓存在本机 `~/.cache/huggingface/hub/models--Systran--faster-whisper-small/`。
3. `get_record` 那条：`media.py` 首次 `get_record(out_format="mp3")` 失败后，兜底又调了一次不带 `out_format` 的 `get_record`；NapCat 的 zod schema 里 `out_format` 是必填，于是抛出 `Schema compilation error: Expected required property`，把首次的真实错误整个盖掉。

根因：

- faster-whisper 用模型名（`small`）初始化时会先访问 huggingface 拉元数据，断网/被墙即 SSLError；它不是"下载完就彻底离线"的。
- 兜底调用本身不合法，还制造了误导性报错。

修法：

- `services/subtitle/subsvc/engines.py` 新增 `_local_model_dir()`：先在 HF 缓存里找 `models--Systran--faster-whisper-<name>/snapshots/*/model.bin`，命中就按目录路径加载，不再联网；没缓存才退回模型名。
- `face_convert/media.py`：`out_format` 必填不省略，改成 `mp3` → `wav` 依次重试；失败时把**首次**异常写进日志（`[face_convert] 取语音失败: <类型>: <信息>`）。另外若 `file_ref` 已经是本地可读文件，直接转写，不再绕 NapCat。

验收：

- 重启 audio 服务后 `POST /asr {"path": 任意 wav}` → `{"ok": true, "text": "…"}`（首次约 3.5s，之后更快），`/health` 里 `asr_loaded: true`。
- 真正算通，要看入站日志出现 `[face_convert] 收信翻译: [对方发来一条语音，你清楚地听到他说：…]`；仍出现「（你没听清）」就说明 `get_record` 还失败，按新日志里的真实异常继续查。

教训：

1. 语音有两条并行链路（AstrBot core 的 `subtitle-stt` provider 和 face_convert 里的本地 whisper），两者都指向 23405，服务一挂就是"记录里全空"，别只查一处。
2. 断网环境里凡是用模型名加载的推理库（whisper / HF 系）都要显式指定本地目录。
3. 给第三方 API 写兜底不能"少传参数再试"，schema 会先拦下来并掩盖真实错误；兜底要换合法取值，并且把首个异常留住。

