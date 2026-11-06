# 从零部署（Windows）

这份文档假设你拿到的是一台**干净的 Windows 电脑**，仓库里只有「大肥鱼」自己的代码，AstrBot、NapCat、CosyVoice、DeepSeek Harness 这些上游程序都需要你**从官方渠道另外下载**。

> 如果你是把这份文档交给 AI 代理来部署：直接读[第 2 节](#2-仓库里有什么没什么)和[第 11 节](#11-给-ai-代理的非交互清单)，所有命令都可以在仓库根目录直接执行。

## 1. 你需要先准备什么

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | Windows 10 / 11 x64 | 微信桥依赖 Windows UIA，Linux / macOS 只能用核心与部分网页服务 |
| Python | **3.12+**（AstrBot 硬性要求）；只跑核心 3.11+ 即可 | 装完在终端执行 `python -V` 确认真实版本 |
| Node.js | 20 LTS 或更高 | 桌宠安装脚本、部分前端资源需要 |
| Git | 可选 | 只有用源码方式装 AstrBot / 拉本仓库时才需要 |
| 显卡 | 可选 | CosyVoice2 约 2~4G 显存；无 GPU 时语音自动回退 edge-tts |
| 网络 | 能访问 GitHub、PyPI、ModelScope | 依赖和模型都要在线下载 |

> Windows 自带的 `python.exe` 应用别名会打开微软商店，不算安装。`python -V` 打印出 `3.12.x` 才算通过。

### 1.1 每个组件对运行环境的要求

| 组件 | Python | Node | 其他 |
|---|---|---|---|
| 核心四服务（hub / model / store / brain） | 3.11+，**只用标准库** | — | 无 |
| AstrBot + 18 个插件 | **3.12+**（4.29.x 硬性要求） | — | 无 |
| NapCat | — | — | 本机安装并登录 QQ |
| 微信桥 `io-wechat` | 3.12+ | — | Windows 桌面微信、UIA 会话 |
| CosyVoice2 | 3.10 / 3.11（脚本自动建独立 venv） | — | NVIDIA 显卡 + CUDA，约 2~4G 显存 |
| 桌宠（DSH 版） | — | 20+ | 已安装 DeepSeek Harness |

### 1.2 Python 包依赖

AstrBot 自己的 `requirements.txt` 会装齐宿主依赖（aiohttp、httpx、pillow、pydub、fastapi、sqlalchemy、jieba 等）。**大肥鱼插件在这之外还要用到下面这些**，安装器第 3 项会自动装进 `astrbot-win\venv`：

| 包 | 谁在用 | 用途 | 不装会怎样 |
|---|---|---|---|
| `edge-tts` | `basic_tools`、语音回退 | 免费在线 TTS | 没有 GPU / CosyVoice 没启动时完全没声音 |
| `imageio-ffmpeg` | `face_convert`、`web_lookup`、`video` | 自带 ffmpeg 二进制，抽帧与转码 | 视频、语音消息解析失败 |
| `faster-whisper` | `face_convert`、`web_lookup`、`basic_tools`、`subtitle`、`video` | 本地语音转文字，默认 `small` int8，首次运行下载约 500MB 权重 | 语音转写、字幕、唱歌咬字核验不可用 |
| `numpy` | `basic_tools`、`face_convert`、`web_lookup` | 音频与图像数组运算 | 翻唱、封面、视觉识别报错 |
| `requests` | `basic_tools` 曲库 | HTTP 请求 | 点歌 / 曲库功能失败 |
| `jieba` | `web_lookup` | 中文分词 | AstrBot 已自带，通常无需单独装 |
| `uiautomation` | `io-wechat` | 桌面微信控件自动化 | 微信桥完全不可用 |
| `wechatauto` | `io-wechat` | 微信会话读写封装 | 微信桥完全不可用 |
| `torch` + `torchaudio`（cu128） | `tools/cosyvoice_tts_server.py` | CosyVoice2 推理 | 本地语音不可用 |
| `Pillow` | 表情包、空间配图 | 图片处理 | AstrBot 已自带 |

手动补齐（安装器已自动执行）：

```powershell
# 装进 AstrBot 的 venv
.\astrbot-win\venv\Scripts\python.exe -m pip install edge-tts imageio-ffmpeg faster-whisper numpy requests jieba

# 微信桥依赖
.\astrbot-win\venv\Scripts\python.exe -m pip install uiautomation
.\astrbot-win\venv\Scripts\python.exe -m pip install "https://codeload.github.com/fanyuantaier/wechatauto-replica/zip/refs/heads/main"
```

CosyVoice 的 torch 由 `tools/setup_cosyvoice.py` 自己挑版本安装，**不要**把 torch 装进 AstrBot 的 venv，两者环境是分开的。

### 1.3 Node 依赖

| 包 | 谁在用 | 说明 |
|---|---|---|
| `electron` | DSH 桌宠 | `pet/dsh-pet/install.ps1` 会自动下载 |
| 无 | `relay/pet-relay.js` | 只用 Node 内置模块，直接 `node relay\pet-relay.js` |

### 1.4 模型与外部程序

| 东西 | 大小（约） | 来源 | 用途 | 必需性 |
|---|---|---|---|---|
| Ollama `qwen2.5vl:3b` | 3.2GB | <https://ollama.com> | 表情包 / 图片认图 | 想做视觉识别才装 |
| CosyVoice2 `iic/CosyVoice2-0.5B` | 3GB | ModelScope | 本地情感语音 | 想要本地语音才装 |
| faster-whisper `small` | 0.5GB | 首次运行自动下载 | 语音转文字 | 装了就自动下 |
| QQ 客户端 | — | 官方 | NapCat 的壳 | 接 QQ 必需 |
| 桌面微信 | — | 官方 | 微信桥附着 | 接微信才需要 |
| DeepSeek Harness | — | **未公开发布** | 桌宠宿主 | 想用桌宠且已有 DSH |

### 1.5 磁盘与显存

全套装完大约占 8~10GB（CosyVoice 占一半），建议预留 15GB。8GB 显存可以同时跑 3B 视觉模型和 0.5B 语音；只有 4GB 时建议二选一，或让语音走 `edge-tts`。

## 2. 仓库里有什么、没什么

**仓库包含（克隆下来就有）**

| 路径 | 内容 |
|---|---|
| `services/hub`、`model`、`store`、`brain` | 核心四服务（控制台、模型路由、记忆、任务编排） |
| `services/io-wechat`、`voice`、`audio`、`video`、`subtitle`、`stickers`、`pet` | 能力服务（按需启动） |
| `astrbot_plugins/` | 18 个自研 AstrBot 插件，每个含 `metadata.yaml`、`main.py`、`config.json` |
| `astrbot-win/AstrBot-4.29.3/data/` | 示例数据：插件配置模板、472 张表情包、文生图模板、本地工具名册 |
| `cosyvoice_ref/profiles/` | 5 条声线的参考音 `ref.wav` + `prompt.txt`（`.pth` 微调权重不入库） |
| `pet/dsh-pet/` | 桌宠安装包 `dsh-pet-0.4.1.tgz` + `install.ps1` + `config.example.jsonc`（个人配置示例） |
| `relay/` | 桌宠与大脑中转脚本 |
| `tools/windows/` | 安装、启动、装插件脚本（`install.bat` 入口在仓库根目录） |
| `stack/` | Docker 部署参考（compose / Dockerfile / 人设） |

**仓库不包含（必须自己从官方装）**

| 组件 | 为什么不在仓库里 |
|---|---|
| AstrBot 主程序 | 上游 AGPL 项目，体积大、版本迭代快，按官方方式安装 |
| NapCat（QQ 协议端） | 上游项目，且需要与你本机 QQ 版本匹配 |
| CosyVoice 源码与模型权重 | 模型约 3GB，且受上游许可约束 |
| DeepSeek Harness（DSH） | **尚未公开发布**，没有官方下载地址，仓库也无法分发 |
| Ollama 与本地模型 | 按需安装，模型动辄数 GB |
| 运行期数据 | `data/plugin_data`、`data/logs`、数据库、Token、Cookie、聊天记录一律不入库 |

## 3. 上游组件：官方地址与落地位置

这张表是整份文档的核心。**不要用别人电脑上的现成目录，按下面的官方地址自己下载。**

| 组件 | 官方下载 / 文档 | 本文验证过的版本 | 装到哪里 | 验证方式 |
|---|---|---|---|---|
| AstrBot | <https://github.com/AstrBotDevs/AstrBot> · 文档 <https://docs.astrbot.app/deploy/astrbot/windows.html> | `v4.29.3`（安装器固定；`v4.29.4` 也可用） | 安装器：`third_party\AstrBot`；Launcher：它自己的安装目录 | 浏览器打开 <http://127.0.0.1:6185> |
| AstrBot Launcher（官方 Windows 安装器） | <https://github.com/AstrBotDevs/astrbot-launcher/releases> → `AstrBot.Launcher_<ver>_x64-setup.exe` | `0.3.14` | 默认安装目录（安装时可改） | 启动器窗口里能看到 AstrBot 运行状态 |
| NapCat | <https://github.com/NapNeko/NapCatQQ/releases> → `NapCat.Shell.Windows.OneKey.zip` · 文档 <https://napcat.napneko.icu/> | `v4.18.42` | 安装器：`third_party\NapCat` | 浏览器打开 <http://127.0.0.1:6099/webui> |
| CosyVoice（语音） | <https://github.com/QwenAudio/CosyVoice>（原 `FunAudioLLM/CosyVoice` 已跳转） | 仓库 `main` | `tools\cosyvoice_env`（venv）+ `tools\cosyvoice_models` | <http://127.0.0.1:8714/health> |
| CosyVoice2 权重 | <https://www.modelscope.cn/models/iic/CosyVoice2-0.5B> | `iic/CosyVoice2-0.5B` | `tools\cosyvoice_models\CosyVoice2-0.5B` | 目录里有 `cosyvoice2.yaml` 等文件 |
| Ollama（可选） | <https://ollama.com/download/windows> | 最新版 | 默认安装目录 | `ollama -v` |
| 微信自动化库 | <https://github.com/fanyuantaier/wechatauto-replica> | `main` | 装进 `astrbot-win\venv`（pip 安装） | `python -c "import wechatauto"` |
| DeepSeek Harness | **未公开发布，无官方地址** | — | 仅限你已经装过 DSH 的机器 | `%USERPROFILE%\.dsh\profiles\web` 是否存在 |

> 文档里出现的 `astrbot-win/AstrBot-4.29.3` 只是**示例数据目录名**。你自己用官方 Launcher 装的 AstrBot 可能是 4.29.4 或更新版本，插件与数据都装到你实际那份 AstrBot 的 `data/` 下即可，不需要强行改名。

### 3.1 AstrBot 在这套系统里做什么

AstrBot 是开源的多平台聊天机器人框架（AGPL-3.0）。它不提供人格，也不实现大肥鱼的业务，只负责四件事：

1. 通过 `aiocqhttp`（OneBot v11）接收 QQ 事件 —— NapCat 把消息用反向 WebSocket 推到它这里；
2. 选一个 LLM provider（DeepSeek 云端 / Ollama / 本地 harness）生成回复；
3. 把消息交给插件链：`human_sim` 决定要不要说话，`provider_router` 决定用哪个模型，`basic_tools`、`web_lookup` 提供工具，`face_convert` 在发送前把 `[旺柴]` 换成真表情；
4. 通过 NapCat 把结果发回 QQ。

完整链路是 **QQ ⇄ NapCat ⇄ AstrBot（+18 个插件）⇄ 核心四服务 / 模型**。AstrBot 停了 QQ 侧完全没反应；核心服务停了机器人还能回，但记忆、语音、工具会降级。

### 3.2 版本与接口

- 本仓库插件按 **AstrBot 4.x** 的 `astrbot.api`（`Star` + `register` + `filter`）编写，`v4.29.3` / `v4.29.4` 都可用。
- **不要用 3.x**：`filter` 与 `Context` 接口不兼容，插件会直接加载失败。
- 安装器固定 `v4.29.3` 是为了和仓库自带的示例 `data/` 对齐；用官方 Launcher 装更新版本也没问题，把插件放进它的 `data/plugins` 即可。

### 3.3 目录结构：你只需要关心 `data/`

```text
<AstrBot>/
├─ main.py                 入口，python main.py 启动
├─ requirements.txt        宿主依赖
└─ data/
   ├─ cmd_config.json      平台、provider、人格等主配置
   ├─ plugins/             插件目录，一个子目录一个插件
   │  └─ astrbot_plugin_xxx/
   │     ├─ metadata.yaml  插件元信息（名称 / 作者 / 版本）
   │     ├─ main.py        @register 的插件类
   │     └─ config.json    这个插件自己的配置
   ├─ plugin_data/         插件运行期数据，不入库
   ├─ stickers/            表情包图库（示例数据 472 张）
   ├─ t2i_templates/       文生图模板
   └─ logs/                运行日志
```

本仓库的 18 个插件把配置放在**各自目录**的 `config.json`，不是 `data/config/`；只有 AstrBot 自己的主配置在 `data/cmd_config.json`。找配置时以「插件目录里的 config.json」为准。

### 3.4 插件是怎么被加载的

- `metadata.yaml` 负责展示信息；`main.py` 用 `@register(name, author, desc, version)` 标记插件类，类继承 `Star`。
- 事件入口通过 `astrbot.api.event.filter` 注册（命令、消息类型、LLM 请求前后、发送前等），插件用 `Context` 拿 provider、会话与发送能力。
- 把插件目录放进 `data/plugins` 后，在 WebUI「插件管理」点重载（或重启 AstrBot）即生效；改 `config.json` 也要重载。
- 插件之间有关系：`face_convert` 用 `sticker_pack` 的图库，`human_sim` 依赖 `provider_router` 选模型，`dafeiyu_pet` 依赖 relay。逐个说明见 [`docs/PLUGINS.md`](PLUGINS.md)。

### 3.5 第一次进 WebUI 必做四件事

1. **改密码**：默认账号密码都是 `astrbot`，登录后立刻改。
2. **加平台**：平台 → 新增 `aiocqhttp`，监听 `127.0.0.1:6199`，token 与 NapCat 反向 WS 完全一致；NapCat 侧填 `ws://127.0.0.1:6199/ws`。同时在 NapCat 里开 **HTTP 服务器 `127.0.0.1:3000`**（见[第 6 节](#6-napcatqq-登录)），否则机器人不会主动说话。
3. **配 provider**：填 DeepSeek API Key（或把地址指向本地 Ollama / 本地 harness）；需要自动分流就启用 `provider_router`。
4. **重载插件**：插件管理 → 重载，确认列表里有 18 个 `astrbot_plugin_*`；先启用 `human_sim`、`provider_router`、`basic_tools` 试跑，其余按需开。

### 3.6 升级、降级与备份

- 源码版升级：`git -C third_party\AstrBot fetch --tags` 后 `git checkout v4.29.4`，再重装一次 `requirements.txt`；`data/` 不会被改动。
- Launcher 版：用启动器自带的更新功能；升级后把插件重新复制进新的 `data/plugins`。
- 升级前备份整个 `data/`（配置、表情包、插件数据），**不要**备份 `venv`。
- 重跑 `install-plugins.ps1` 会覆盖同名插件目录，改过 `config.json` 的先留一份。

### 3.7 AstrBot 常见报错对照

| 现象 | 原因 | 处理 |
|---|---|---|
| 6185 打不开 | 没启动或端口被占 | `netstat -ano \| findstr 6185`，杀掉占用进程或换端口 |
| `ModuleNotFoundError: astrbot` | 用了系统 Python 而不是 venv | 用 `astrbot-win\venv\Scripts\python.exe main.py` |
| 插件列表里没有大肥鱼插件 | 目录多套了一层，或缺 `main.py` | 必须是 `data/plugins/<插件名>/main.py` |
| 插件报 `No module named edge_tts` | 附加依赖没装 | 按 [1.2](#12-python-包依赖) 补装 |
| `aiocqhttp` 反复重连 | 端口或 token 与 NapCat 不一致 | 两边逐字符核对 |
| 模型 401 / 余额不足 | provider 密钥或额度 | WebUI → Provider 检查 |
| 启动提示 Python 版本不符 | 低于 3.12 | 重装 3.12 并重建 venv |

## 4. 方式一：一键安装（推荐）

```powershell
git clone https://github.com/newbiecraft-kaixin/dafeiyu-open.git
cd dafeiyu-open
.\install.bat
```

菜单会逐项询问，输入编号（可多选，如 `2,4`）：

| 编号 | 内容 | 会做什么 |
|---|---|---|
| 1 | 运行环境 | 检测 Python 3.12+ / Node 20+，缺失时用 winget 安装；创建 `astrbot-win\venv` |
| 2 | 核心四服务 | 校验目录，可选把 `DEEPSEEK_API_KEY` 写进 `.env` |
| 3 | AstrBot | 从 GitHub 下载 **v4.29.3** 源码到 `third_party\AstrBot`，装 AstrBot 依赖**和插件附加依赖**（edge-tts / imageio-ffmpeg / faster-whisper / numpy / requests），带入示例 `data` |
| 4 | 大肥鱼插件包 | 把 18 个插件复制进 AstrBot 的 `data/plugins` |
| 5 | NapCat | 从 GitHub Releases 下载最新的 `NapCat.Shell.Windows.OneKey.zip`，解压到 `third_party\NapCat` |
| 6 | 微信桥 | 在 venv 里安装 `uiautomation` 与 `wechatauto` |
| 7 | CosyVoice2 | 调用 `tools/setup_cosyvoice.py`，拉取 torch 与模型（约 3GB，需 NVIDIA 显卡） |
| 8 | 桌宠 | 调用 `pet/dsh-pet/install.ps1`（要求本机已装 DSH） |
| 9 | 启动核心 | 启动四服务并打开 <http://127.0.0.1:23408> |

直接回车等于 `1,2`，输入 `a` 全装。非交互用法：

```powershell
.\install.bat -Check          # 只体检，不改任何文件
.\install.bat -Select 2,4     # 只装核心 + 插件
.\install.bat -Select all -Yes
.\install.bat -DryRun         # 预演流程
```

安装器**只负责从官方源下载**，不携带第三方二进制。装完后按[第 9 节](#9-逐项验收)验收。

## 5. 方式二：手动装 AstrBot（两种官方途径）

### 5.1 官方 Launcher（装好即用）

1. 打开 <https://github.com/AstrBotDevs/astrbot-launcher/releases>，下载 `AstrBot.Launcher_<ver>_x64-setup.exe`。
2. 安装并启动，按向导让它下载 AstrBot 本体。
3. 浏览器打开 <http://127.0.0.1:6185>，首次登录账号密码都是 `astrbot`，登录后立刻改密码。
4. 记下它的数据目录：源码版是 AstrBot 根目录下的 `data`；Launcher 版采用官方默认位置，可在启动器里看到安装路径，`data` 就在其安装目录下。

### 5.2 官方源码（版本固定，便于复现）

```powershell
# 需要 Python 3.12+ 与 Git
git clone --branch v4.29.3 --depth 1 https://github.com/AstrBotDevs/AstrBot.git third_party\AstrBot
cd third_party\AstrBot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

同样访问 <http://127.0.0.1:6185>。想换版本就把 `--branch v4.29.3` 换成别的 tag（如 `v4.29.4`），插件接口一致。

### 5.3 带入示例数据与插件

```powershell
# 可选：示例表情包 / 配置模板 / 文生图模板
Copy-Item ".\astrbot-win\AstrBot-4.29.3\data\*" "<你的AstrBot>\data" -Recurse -Force

# 必做：安装 18 个插件
powershell -ExecutionPolicy Bypass -File .\tools\windows\install-plugins.ps1 -AstrBotData "<你的AstrBot>\data"
```

`<你的AstrBot>` 换成你实际的 AstrBot 目录。装完在 WebUI → 插件管理 → 重新加载，插件列表里应出现 18 个 `astrbot_plugin_*`。先只启用 `human_sim`、`provider_router`、`basic_tools` 熟悉行为，其余按需开启，指令与配置见 [`docs/PLUGINS.md`](PLUGINS.md)。

## 6. NapCat：接 QQ

1. 打开 <https://github.com/NapNeko/NapCatQQ/releases>，下载 `NapCat.Shell.Windows.OneKey.zip`（`v4.18.42` 及以后均可）。
2. 解压到 `third_party\NapCat`，双击其中的 `NapCatInstaller.exe`，按提示选择你本机 QQ 的版本完成安装。
3. 启动：`tools\windows\start-napcat-win.bat`。
4. 浏览器打开 <http://127.0.0.1:6099/webui>，先看 WebUI 里显示的 token，再扫码登录 QQ。
5. 配置 **OneBot v11 两条通道**（都在 WebUI → 网络配置里新增，token 两边保持一致）：
   - **反向 WebSocket**：`ws://127.0.0.1:6199/ws` —— 必开。QQ 消息收发的主通道，断了机器人就完全不响应。
   - **HTTP 服务器**：监听 `127.0.0.1:3000` —— 必开。机器人主动发消息、插件出站调用都走它（hub 的「发消息」工具直接 `POST http://127.0.0.1:3000/send_group_msg`）。不开这一项，被动回复正常，主动说话会报「napcat 不在线」。
6. 在 AstrBot WebUI 里添加 `aiocqhttp` 平台，端口与 token 跟第 5 步保持一致。

验证两条通道都通：

```powershell
Test-NetConnection 127.0.0.1 -Port 6199   # 反向 WS
Test-NetConnection 127.0.0.1 -Port 3000   # OneBot HTTP
```

NapCat 的配置随 QQ 版本变化，遇到控件或字段对不上时以官方文档 <https://napcat.napneko.icu/> 为准。

> 与 AstrBot 一样，仓库里找不到 NapCat 时脚本会复用本机已装的那份：优先 `~/dafeiyu\napcat-win`，再全局浅层搜索。

## 7. CosyVoice2：本地情感语音

```powershell
python tools\setup_cosyvoice.py
```

脚本会做三件事：从官方仓库 <https://github.com/QwenAudio/CosyVoice> 拉代码、创建 `tools\cosyvoice_env`、从 ModelScope 下载 `iic/CosyVoice2-0.5B` 到 `tools\cosyvoice_models`。

- 只装不启动：`python tools\setup_cosyvoice.py --no-start`
- 手动启动：`tools\windows\start-cosyvoice-tts.bat`（监听 8714）
- 验证：<http://127.0.0.1:8714/health>

没有 NVIDIA 显卡时不要装，语音会自动回退到 edge-tts。

## 8. 可选组件

**Ollama**：从 <https://ollama.com/download/windows> 安装，识图模型可执行 `ollama pull qwen2.5vl:3b`。不装则所有模型调用走云端 API。

**微信桥**：需要一台已登录、窗口可见的桌面微信。

```powershell
.\services\io-wechat\start-wechat.bat
```

服务端口见 `services\io-wechat\service.json`。锁屏、最小化到托盘、换微信版本都可能导致 UIA 找不到控件。

**桌宠**：`pet/dsh-pet` 是 DeepSeek Harness（DSH）的插件，而 **DSH 目前未公开发布**——没有官方下载页，仓库也不能分发。所以：

- 已经有 DSH 的机器：确认 `%USERPROFILE%\.dsh\profiles\web` 存在，然后运行
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\pet\dsh-pet\install.ps1
  ```
  脚本会把仓库自带的 `dsh-pet-0.4.1.tgz` 装进 DSH profile、下载 Electron 运行时、写入 bundles。重启 DSH 后在插件列表启用 `dsh-pet`。
- 没有 DSH 的机器：桌宠这条路暂时走不通（等 DSH 公开发布后重试本节即可）。
- 想让桌宠和大脑联动，再启动 `node relay\pet-relay.js`（23334）与 `services\pet`。

个人数据在 `%USERPROFILE%\.dsh\dsh-pet`，不会进仓库。

## 9. 逐项验收

全部启动后，逐条执行，返回 JSON 或页面即算通过：

```powershell
Invoke-RestMethod http://127.0.0.1:23408/health   # hub，控制台 http://127.0.0.1:23408
Invoke-RestMethod http://127.0.0.1:23406/health   # model
Invoke-RestMethod http://127.0.0.1:23407/health   # store
Invoke-RestMethod http://127.0.0.1:23402/health   # brain
Invoke-RestMethod http://127.0.0.1:8714/health    # CosyVoice（装了才有）
Invoke-RestMethod http://127.0.0.1:23366/health   # audio
Invoke-RestMethod http://127.0.0.1:23368/health   # video
Invoke-RestMethod http://127.0.0.1:23405/health   # subtitle
```

| 检查点 | 通过标准 |
|---|---|
| 核心 | 上面 4 个 `/health` 都有响应，hub 页面里四个服务显示在线 |
| AstrBot | <http://127.0.0.1:6185> 能登录，插件管理里 18 个插件 |
| NapCat | <http://127.0.0.1:6099/webui> 能打开，QQ 显示已登录 |
| QQ 链路 | AstrBot 日志出现 `aiocqhttp` 已连接；群里发消息机器人有反应 |
| 语音 | `8714/health` 正常；关闭 CosyVoice 后回复仍有声（回退 edge-tts） |
| 微信 | `io-wechat` 的 `/health` 显示已附着窗口；收发一条消息验证 |
| 桌宠 | DSH 插件列表里有 `dsh-pet`，启用后桌面出现桌宠 |

## 10. 核心四服务的启动与配置

```powershell
.\tools\windows\start-core.bat
```

脚本按自身位置定位仓库根目录，依次检查并启动 `hub 23408`、`model 23406`、`store 23407`、`brain 23402`，已在监听的端口会跳过。也可以单独启动：`services\brain\start-brain.bat`、`services\model\start-model.bat`、`services\store\start-store.bat`。

模型密钥通过环境变量注入：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
```

长期生效请写进系统环境变量，然后重启服务。其他提供方见 [`services/model/providers.json`](../services/model/providers.json)，支持 OpenAI 兼容接口、Ollama 与本地 harness。

配置里的路径默认都按**仓库根**解析，不需要改成本机绝对路径；只有把组件装在仓库外时，才把对应字段填成绝对路径：

| 文件 | 字段 | 说明 |
|---|---|---|
| [`services/model/providers.json`](../services/model/providers.json) | `dotenv`、`dsh-account.dsh_root` | `dotenv` 相对 `providers.json` 解析；`dsh_root` 留空时用 `%USERPROFILE%\dafeiyu\dsh`，不用本地 harness 就删掉 `dsh-account` |
| [`services/brain/roles.json`](../services/brain/roles.json) | `dsh_root`、`dotenv` | 默认 `dsh`、`.env`、`services/brain/.env`，相对仓库根；只影响本地 harness |
| [`services/subtitle/service.json`](../services/subtitle/service.json) | `python` | 已移除该字段；不填时按 `astrbot-win/venv` → 系统 `python` 顺序回退 |
| `astrbot_plugins/astrbot_plugin_human_sim/config.json` | `recharge_script`、`recharge_python` | 默认空；只有用「动态出码」功能才填，相对路径按仓库根解析 |

AstrBot 目录按 `astrbot-win/AstrBot-*` 自动识别，版本从 4.29.x 升到 4.30.x 也不用改配置；仓库里找不到时会退到 `third_party/AstrBot`，再找不到就复用本机已装的那份（`~/dafeiyu` 下，或在 `~/dafeiyu`、`~/workspace`、`~/Desktop` 浅层搜索 `AstrBot*`），并优先用它旁边的 `venv`。Python / Node / NapCat 也会在仓库里按版本号搜索，Node 与 NapCat 找不到时同样复用 `~/dafeiyu\napcat-win`，最后才用系统命令。

云端提供方（如 `deepseek-remote`）只依赖环境变量，不改路径也能直接用。

## 11. 给 AI 代理的非交互清单

按顺序执行，每步都有可判定的结果：

1. `python -V` → 必须是 3.12 及以上；否则 `winget install --id Python.Python.3.12 -e`。
2. `node -v` → 必须是 v20 及以上；否则 `winget install --id OpenJS.NodeJS.LTS -e`。
3. `.\install.bat -Check` → 打印各组件状态，不修改文件。
4. `.\install.bat -Select 1,2,3,4,5 -Yes` → 装环境、核心、AstrBot、插件、NapCat。
   - 装完先验证附加依赖：`.\astrbot-win\venv\Scripts\python.exe -c "import edge_tts, imageio_ffmpeg, faster_whisper, numpy, requests"`，有 `ModuleNotFoundError` 就按 [1.2](#12-python-包依赖) 补装。
5. 需要语音时追加 `.\install.bat -Select 7 -Yes`（要求有 NVIDIA 显卡）。
6. 需要桌宠时先确认 `Test-Path "$env:USERPROFILE\.dsh\profiles\web"`，再 `.\install.bat -Select 8 -Yes`。
7. `.\install.bat -Select 9 -Yes` → 启动核心四服务。
8. 按[第 9 节](#9-逐项验收)逐个请求 `/health`，全部返回后才算部署成功。
9. 检查端口：`6199`（AstrBot 反向 WS）、`3000`（NapCat OneBot HTTP）、`6099`（NapCat WebUI）三个都要通；缺 `3000` 时机器人只能被动回复。跨机或公网访问按 [12.1](#121-端口放行与反向代理) 处理。

禁止事项：不要复制其他机器上的 `astrbot-win\AstrBot-4.29.3` 整个目录当作 AstrBot 安装（那只是示例数据）；不要把 Token、Cookie、聊天记录写进仓库；不要为 DSH 编造下载地址。

## 12. 端口速查

| 服务 | 端口 | 说明 |
|---|---|---|
| hub | 23408 | 统一控制台，从这里开始看 |
| model | 23406 | 模型路由 |
| store | 23407 | 记忆 / 存储 |
| brain | 23402 | 任务编排 |
| AstrBot | 6185 | WebUI，默认账号 `astrbot/astrbot` |
| AstrBot aiocqhttp | 6199 | 接收 NapCat 反向 WS |
| NapCat WebUI | 6099 | 登录 QQ、看 token |
| **NapCat OneBot HTTP** | **3000** | **机器人主动发消息走这里**，hub 的「发消息」工具直连 `http://127.0.0.1:3000/{action}`；没开就报「napcat 不在线」 |
| io-wechat | 23392 | 微信桌面桥 |
| audio | 23366 | 音色训练与歌曲库，`/ui/train` |
| video | 23368 | 视频解析与信息流 |
| subtitle | 23405 | whisper 字幕 / 画面描述 |
| stickers | 8712 | 表情包存储 |
| voice | 8714 | CosyVoice2 TTS |
| pet（pet-relay） | 23334 | 桌宠侧信道 |
| dsh | 3080 | DSH 桌宠渲染面 |

端口冲突时用环境变量覆盖（如 `HUB_PORT`、`AUDIO_PORT`、`VIDEO_PORT`）。

### 12.1 端口放行与反向代理

所有服务默认只监听 `127.0.0.1`，同一台机器上互相访问不需要额外放行。按场景开端口：

| 场景 | 要开的端口 | 做法 |
|---|---|---|
| 只在本机用 | 无 | 浏览器直接开 <http://127.0.0.1:23408> |
| 局域网另一台机器看控制台 | `23408`（按需 `23366`/`23368`/`23405`） | Windows 防火墙 → 高级设置 → 入站规则 → 新建规则 → 端口 |
| NapCat 与 AstrBot 不在同一台机器 | NapCat 所在机器的 `3000`（OneBot HTTP）、`6199`（反向 WS） | 同上；反向 WS 地址改成对方内网 IP |
| 远程 / 公网访问 | 只开**反向代理端口**（示例 `8443`） | 用 Nginx / Caddy 反代到 `127.0.0.1:23408` 并加认证 |

NapCat 的 `3000` 端口由它自己的 OneBot v11 HTTP 服务提供：WebUI（6099）→ 网络配置 → 新增 HTTP 服务器，端口填 `3000`、Host 填 `127.0.0.1`，token 与 AstrBot 侧保持一致。不启用这一项时，QQ 收发照常（走 `6199` 反向 WS），但 hub 的主动发消息和部分插件的出站调用会失败。

远程访问不要直接把端口映射到公网，`3000`、`6199`、`6099`、`6185` 一律保持本机闭环，对外只暴露反向代理那一个端口。Nginx 最小示例：

```nginx
server {
    listen 8443 ssl;
    server_name your-domain;
    location / {
        auth_basic "dafeiyu";
        auth_basic_user_file /etc/nginx/.htpasswd;
        proxy_pass http://127.0.0.1:23408;
    }
}
```

## 13. 常见问题

**`python` 不是内部或外部命令**：Python 没进 PATH，重装时勾选 “Add python.exe to PATH”。

**AstrBot 启动报 Python 版本错误**：AstrBot 要求 3.12+，把 `astrbot-win\venv` 删掉后重跑 `install.bat -Select 1,3`。

**插件列表是空的**：`-AstrBotData` 指错了目录。它必须是 AstrBot 根目录下的 `data`，不是 `data\plugins`。

**6185 打不开**：AstrBot 还没启动，或端口被别的程序占了。用 `netstat -ano | findstr 6185` 查占用。

**QQ 登录后机器人不回**：先看 AstrBot 日志有没有 `aiocqhttp` 连接；再核对 NapCat 的反向 WS 地址与 token 是否和 AstrBot 平台配置完全一致。

**机器人能被动回复，但不会主动说话 / 提示「napcat 不在线」**：NapCat 没开 OneBot v11 的 **HTTP 服务器（3000）**，只配了反向 WS 6199。WebUI → 网络配置新增 HTTP 服务器，端口 `3000`，token 与反向 WS 一致；然后 `Test-NetConnection 127.0.0.1 -Port 3000` 确认通。

**防火墙拦住了跨机访问**：本机默认只监听 `127.0.0.1`，局域网另一台机器访问要按 [12.1 端口放行与反向代理](#121-端口放行与反向代理) 开入站规则；公网访问一律走反向代理，不要直接映射端口。

**页面能开但模型报错**：密钥没注入到运行进程的环境变量里，重启该服务。

**语音变成机器音**：CosyVoice2 没起来或显存不足，已自动回退 edge-tts，检查 `8714/health`。

**微信不动**：微信必须在前台可见的桌面会话里运行，锁屏或最小化到托盘可能失败；不同微信版本控件会变化。

**训练报显存不足**：把 `batch_size` 降到 1，或减少 `steps`、关闭 `bf16`。

## 14. 合规提醒

- 本项目以 AGPL-3.0 发布，二次分发需保留上游（AstrBot、NapCat、CosyVoice、dsh-pet 等）的版权与许可声明。
- QQ / 微信自动化可能违反平台条款，存在账号风控风险，请自行评估。
- 仓库不含任何真实 Token、Cookie、账号或聊天记录，首次运行需自行登录生成配置。不要把这些文件提交到公开仓库。

本机部署的原始记录（含作者机器上的路径与版本）见 [`docs/WINDOWS-DEPLOY.md`](WINDOWS-DEPLOY.md)，架构说明见 [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)。
