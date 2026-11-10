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
| AstrBot + 24 个插件 | **3.12+**（4.29.x 硬性要求） | — | 无 |
| NapCat | — | — | 本机安装并登录 QQ |
| 微信桥 `io-wechat` | 3.12+ | — | Windows 桌面微信、UIA 会话 |
| CosyVoice2 | 3.10 / 3.11（脚本自动建独立 venv） | — | NVIDIA 显卡 + CUDA，约 2~4G 显存 |
| 翻唱 Seed-VC | 3.12+（独立 venv `audio\seedvc_env`） | — | NVIDIA 显卡 + CUDA、FFmpeg；安装器不装，见 [7.2](#72-翻唱seed-vc的权重与素材) |
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
| `jieba` | `web_lookup`、`sticker_pack` | 中文分词：视频标题/正文切分，表情包按词做语义匹配 | 表情包会自动退回二元组分词，功能可用、精度略降 |
| `ffmpeg`（系统级，非 pip） | `demucs` 分轨 | 翻唱时剥离人声/伴奏 | 翻唱不可用；普通语音/视频转码有 `imageio-ffmpeg` 兜底 |
| `uiautomation` | `io-wechat` | 桌面微信控件自动化 | 微信桥完全不可用 |
| `wechatauto` | `io-wechat` | 微信会话读写封装 | 微信桥完全不可用 |
| `torch` + `torchaudio`（cu128） | `tools/cosyvoice_tts_server.py` | CosyVoice2 推理 | 本地语音不可用 |
| `Pillow` | 表情包、空间配图 | 图片处理 | AstrBot 已自带 |
| `playwright` + `qrcode`（可选） | `web_lookup`、`services/video` | 抖音/快手扫码登录、刷 B站/抖音/快手片单 | 单条链接仍能解析；扫码登录与「刷视频」不可用 |

手动补齐（安装器已自动执行）：

```powershell
# 装进 AstrBot 的 venv
.\astrbot-win\venv\Scripts\python.exe -m pip install edge-tts imageio-ffmpeg faster-whisper numpy requests jieba

# 微信桥依赖
.\astrbot-win\venv\Scripts\python.exe -m pip install uiautomation
.\astrbot-win\venv\Scripts\python.exe -m pip install "https://codeload.github.com/fanyuantaier/wechatauto-replica/zip/refs/heads/main"

# 可选：扫码登录 / 刷视频（插件和 video 服务各装一份；video 服务用的是启动 hub 的那个 Python）
.\astrbot-win\venv\Scripts\python.exe -m pip install playwright qrcode
.\astrbot-win\venv\Scripts\python.exe -m playwright install chromium
python -m pip install playwright qrcode
python -m playwright install chromium
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
| Ollama `qwen2.5vl:3b` | 3.2GB | <https://ollama.com> | 识图：表情包自动收录的粗筛、视频抽帧画面描述 | 不做视觉识别可不装 |
| Ollama `qwen3.5:9b` | 数 GB | <https://ollama.com> | 群聊/私聊的小模型档、桌宠旁白、识图备选 | 只用云端模型可不装 |
| Ollama `nomic-embed-text` | 约 300MB | <https://ollama.com> | 向量模型：长期记忆（livingmemory）与知识库检索 | 不用长期记忆可不装 |
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
| `astrbot_plugins/` | 24 个自研 AstrBot 插件，按 input（输入）/ output（输出）/ function（功能）三个子包组织，每个含 `metadata.yaml`、`main.py`、`config.json` |
| `astrbot-win/AstrBot-4.29.3/data/` | 示例数据：插件配置模板、472 张表情包、文生图模板、本地工具名册 |
| `audio/cosyvoice_ref/profiles/` | 5 条声线的参考音 `ref.wav` + `prompt.txt`（`.pth` 微调权重不入库） |
| `pet/dsh-pet/` | **本项目实际在用的桌宠**：安装包 `dsh-pet-0.4.1.tgz`（62.5 MB，已入库）+ `install.ps1` + `config.example.jsonc` + `patches/`，clone 后可直接装 |
| `pet/clawd-on-desk/` | 上游桌宠壳的源码，仅作改造参考，**不是**本项目在用的桌宠；不要另外去装它的官方 exe |
| `pet/dafeiyu-web/`、`pet/dafeiyu3d/` | 桌宠前端的两个实验版本，未接入主链路，部署时可以忽略 |
| `relay/` | 桌宠与大脑中转脚本 |
| `installer/` | 安装、启动、装插件脚本（`install.bat` 入口在仓库根目录） |
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
| CosyVoice（语音） | <https://github.com/QwenAudio/CosyVoice>（原 `FunAudioLLM/CosyVoice` 已跳转） | 仓库 `main` | `audio\cosyvoice_env`（venv）+ `audio\cosyvoice_models` | <http://127.0.0.1:8714/health> |
| CosyVoice2 权重 | <https://www.modelscope.cn/models/iic/CosyVoice2-0.5B> | `iic/CosyVoice2-0.5B` | `audio\cosyvoice_models\CosyVoice2-0.5B` | 目录里有 `cosyvoice2.yaml` 等文件 |
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

完整链路是 **QQ ⇄ NapCat ⇄ AstrBot（+24 个插件）⇄ 核心四服务 / 模型**。AstrBot 停了 QQ 侧完全没反应；核心服务停了机器人还能回，但记忆、语音、工具会降级。

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

本仓库的 24 个插件把配置放在**各自目录**的 `config.json`，不是 `data/config/`；只有 AstrBot 自己的主配置在 `data/cmd_config.json`。找配置时以「插件目录里的 config.json」为准。

### 3.4 插件是怎么被加载的

- `metadata.yaml` 负责展示信息；`main.py` 用 `@register(name, author, desc, version)` 标记插件类，类继承 `Star`。
- 事件入口通过 `astrbot.api.event.filter` 注册（命令、消息类型、LLM 请求前后、发送前等），插件用 `Context` 拿 provider、会话与发送能力。
- 把插件目录放进 `data/plugins` 后，在 WebUI「插件管理」点重载（或重启 AstrBot）即生效；改 `config.json` 也要重载。
- 插件之间有关系：`face_convert` 用 `sticker_pack` 的图库，`human_sim` 依赖 `provider_router` 选模型，`dafeiyu_pet` 依赖 relay。逐个说明见 [`docs/PLUGINS.md`](PLUGINS.md)。

**仓库里到底有哪些插件**：`astrbot_plugins/` 下按 `input/`（4 个）、`output/`（5 个）、`function/`（15 个）分三个子包，共 **24 个自研插件**。`install-plugins.ps1` 会把它们全量复制进 `data/plugins`，但**不需要全开**——下表「装不装」列写了建议。运行时 `data/plugins` 里可能还多一个 `qq_action`，它不随仓库分发。

| 插件 | 作用 | 额外依赖 | 装不装 |
|---|---|---|---|
| `human_sim` | 潜水插话、冷场开话头、拍一拍全链路（被拍必回/连拍回戳） | 无；表情包图库来自 `sticker_pack` | 要，群聊核心 |
| `humanizer` | 去 AI 腔、清洗 Markdown 与长句排比、防复读 | 无 | 要 |
| `group_window` | 被动记录群聊窗口，机器人发言时注入上下文 | 无 | 要 |
| `speaker_tag` | 补 `[昵称/时间]:` 标签，按会话存人物档案 | 无 | 要 |
| `basic_tools` | 时间/群成员/戳一戳/提醒/天气/头像 + `/听歌 /声线 /停` | CosyVoice2 TTS（可选，没有就回退 edge-tts） | 要 |
| `sticker_pack` | `:情绪词:` 按语义配图，自动配 QQ 黄脸 | `jieba`（可选，缺了匹配精度下降） | 要 |
| `face_convert` | `[流泪][doge]` → 真实 QQ 表情 | 无 | 要 |
| `web_lookup` | 联网查证、抓正文、B 站字幕/视频 | `faster-whisper`（只要视频字幕才用） | 要 |
| `provider_router` | 群聊/私聊/陌生人自动分模型，`/模型` `/转换` `/角色` | 无 | 要 |
| `tool_gate` | 按群决定模型/人设/工具集 | 无 | 要 |
| `local_tools` | 本机文件读写与命令执行，默认仅主人 | 无（注意权限） | 建议 |
| `self_growth` | 成长笔记，自动注入系统提示 | 无 | 建议 |
| `livingmemory` | 长期记忆：提取、召回、遗忘 | `networkx jieba pytz aiofiles aiosqlite`（自带 `requirements.txt`） | 建议 |
| `command_guard` | 命令类 handler 已回复就跳过默认 LLM | 无 | 建议，省调用 |
| `prompt_cache` | 稳定 system prompt 尾部，提高 DeepSeek 前缀缓存命中 | 无 | 建议（用 DeepSeek 时） |
| `qzone_channel` | 空间评论独立信道，`/看空间` | 无（需填 `manual_cookie`） | 可选 |
| `daily_qzone` | 每日自动发说说 | 无 | 可选 |
| `subtitle_stt` | 注册 `subtitle_stt` provider，语音转字幕 | `faster-whisper` | 可选 |
| `dafeiyu_pet` | 把消息/回复状态发给桌宠中转 | `relay/pet-relay.js` 在跑（23334） | 装了桌宠才要 |
| `wechat_oc_bridge` | 接入本机 PC 微信小号 | `services/io-wechat` 在跑 | 接微信才要 |
| `dsh_bridge` | 注册 `dsh_bridge` provider，把 DSH 当外部 agent | 本机 DSH + 桥接 18081 | 用 DSH 才要 |
| `qq_mcp` | 把发消息/查历史/戳一戳包成 MCP 工具 | 无（供 DSH 调用，18083） | 用 DSH 才要 |
| `prompt_audit` | 只读记录每段 prompt 字符数，调缓存用 | 无 | 调试用 |
| `zz_prompt_dump` | 全链路 dump prompt，配合 llm-monitor | 无 | 调试用，默认别开 |

### 3.5 第一次进 WebUI 必做四件事

1. **改密码**：默认账号密码都是 `astrbot`，登录后立刻改。
2. **加平台**：平台 → 新增 `aiocqhttp`，监听 `127.0.0.1:6199`，token 与 NapCat 反向 WS 完全一致；NapCat 侧填 `ws://127.0.0.1:6199/ws`。同时在 NapCat 里开 **HTTP 服务器 `127.0.0.1:3000`**（见[第 6 节](#6-napcatqq-登录)），否则机器人不会主动说话。
3. **配 provider**：到 <https://platform.deepseek.com> 申请 API Key → WebUI → 服务提供商 → 新增，类型选 **OpenAI 兼容**，API Base 填 `https://api.deepseek.com`，模型填 `deepseek-chat`（或 `deepseek-reasoner`），粘贴 Key 保存后点「测试」。想走本地 Ollama 就把 Base 换成 `http://127.0.0.1:11434/v1`、模型按 `ollama list` 的输出填；需要自动分流再启用 `provider_router`。**没有 Key 或 Ollama 时，机器人在第 3 步之前一句都不会回**。
4. **重载插件**：插件管理 → 重载，确认列表里有 24 个 `astrbot_plugin_*`；先启用 `human_sim`、`provider_router`、`basic_tools` 试跑，其余按需开。

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
| 4 | 大肥鱼插件包 | 把 24 个插件复制进 AstrBot 的 `data/plugins` |
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

### 4.1 不克隆仓库：一条命令下载部署

在 PowerShell（不是 cmd）里执行：

```powershell
irm https://raw.githubusercontent.com/newbiecraft-kaixin/dafeiyu-open/main/installer/bootstrap.ps1 | iex
```

它会下载仓库压缩包、解压到 `%USERPROFILE%\dafeiyu`、执行「运行环境 + 核心四服务」两步，然后打开管理面板。需要全量安装或换目录时，先把脚本存下来再带参数运行：

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/newbiecraft-kaixin/dafeiyu-open/main/installer/bootstrap.ps1 -OutFile "$env:TEMP\bootstrap.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\bootstrap.ps1" -Full -Dest D:\dafeiyu
```

目标目录已存在时只更新代码，保留 `.env`、`venv`、`data`、日志和第三方组件。

### 4.2 可视化面板（无控制台）

双击下面这个文件即可，不会出现黑色控制台窗口（`wscript` 会挑一个带 tkinter 的 `pythonw` 来跑）：

```
%USERPROFILE%\dafeiyu\installer\大肥鱼管理面板.vbs
```

面板读取 `services/*/service.json`，显示每个服务的在线状态，支持单个或批量启动 / 停止 / 重启，并能直接打开服务页面和启动日志。**关闭窗口不会停止服务**。命令行等价物：

```powershell
pythonw installer\manager.py
```

「下载 / 安装依赖」窗口顶部的「一键部署」会走完整流程：从 GitHub `main` 下载最新代码 → **按 `config/features.json` 安装启用的能力** → 检查环境 → 按依赖顺序启动全部服务 → 校验端口。弹窗选「否」则改用本机仓库（含未提交改动）。语音（CosyVoice2，约 3GB）和微信桥默认关闭，不需要就别装；改完点工具栏「部署配置」即可。命令行等价：

```powershell
python installer\manager.py --deploy           # GitHub main + 按配置安装
python installer\manager.py --deploy --local   # 本机仓库（含未提交改动）
python installer\manager.py --deploy --select 1,2,8   # 临时指定安装项
```

可选能力缺失时都是降级而不是报错：没有 voice 就用 AstrBot 的 edge-tts 兜底或直接跳过语音；没有 video 只能拿到链接、没有标题/字幕；没有 store 时记忆不落盘；没有桌宠时气泡跳过。面板里被关闭的能力会标「未启用（部署配置）」。

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

# 必做：安装 24 个插件
powershell -ExecutionPolicy Bypass -File .\installer\install-plugins.ps1 -AstrBotData "<你的AstrBot>\data"
```

`<你的AstrBot>` 换成你实际的 AstrBot 目录。装完在 WebUI → 插件管理 → 重新加载，插件列表里应出现 24 个 `astrbot_plugin_*`。先只启用 `human_sim`、`provider_router`、`basic_tools` 熟悉行为，其余按需开启，指令与配置见 [`docs/PLUGINS.md`](PLUGINS.md)。

## 6. NapCat：接 QQ

1. 打开 <https://github.com/NapNeko/NapCatQQ/releases>，下载 `NapCat.Shell.Windows.OneKey.zip`（`v4.18.42` 及以后均可）。
2. 解压到 `third_party\NapCat`，双击其中的 `NapCatInstaller.exe`，按提示选择你本机 QQ 的版本完成安装。
3. 启动：`installer\start-napcat-win.bat`。
4. 浏览器打开 <http://127.0.0.1:6099/webui>。首次要 token：它在 NapCat 启动窗口/日志里打印，也可看 `third_party\NapCat\config\webui.json` 的 `token` 字段。登录后用**手机 QQ 扫码**登录机器人账号——建议用小号，登录后该号在手机端会下线。成功标志：WebUI 顶部显示 QQ 号和「已登录」。
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

脚本会做三件事：从官方仓库 <https://github.com/QwenAudio/CosyVoice> 拉源码，建独立 venv `audio\cosyvoice_env`，从 ModelScope 下载 `iic/CosyVoice2-0.5B` 到 `audio\cosyvoice_models\CosyVoice2-0.5B`。服务启动时按「仓库根 → `~/dafeiyu` → 浅层搜索」自动找名为 `cosyvoice_models` 的目录，目录被挪走时可用 `COSYVOICE_MODEL_DIR` 指定。

- 只装不启动：`python tools\setup_cosyvoice.py --no-start`
- 手动启动：`installer\start-cosyvoice-tts.bat`（监听 8714）
- 验证：<http://127.0.0.1:8714/health>

没有 NVIDIA 显卡时不要装，语音会自动回退到 edge-tts。

### 7.1 权重与素材来源

| 东西 | 来源 | 是否入库 | 说明 |
|---|---|---|---|
| 基座权重 `CosyVoice2-0.5B` | ModelScope `iic/CosyVoice2-0.5B`（Apache-2.0） | 否，约 3GB | 由 `setup_cosyvoice.py` 自动下载；换机器重跑一次即可 |
| 推理代码 | `github.com/QwenAudio/CosyVoice` | 否 | 脚本 clone 或下 zip 到 `audio\CosyVoice` |
| 5 条声线 | `audio\cosyvoice_ref\profiles\{小忆,姬子,流萤,御姐,低音炮}` | **是**：`ref.wav` + `prompt.txt` | 每条约 3~10 秒的参考人声；`/声线` 切换靠的就是这些 wav |
| 微调权重 `ft_model_*.pth` | 本地训练产物 | 否，单个约 750MB | 有就音色更像，没有就退化为 zero-shot 克隆（仅用 `ref.wav`） |
| 参考音素材 | 作者本机自备的短音频样本 | 是 | 仅本地自用；二次分发或商用请自行确认版权 |

想换自己的声音：把一段 **3~10 秒、干净无背景音乐**的 wav 放到 `audio\cosyvoice_ref\ref.wav`，或启动前设 `COSYVOICE_PROMPT_WAV` 指向你的文件；也可以在 <http://127.0.0.1:23366/ui/train>（声线管理页）上传参考音 + 提示文本，直接生成一条新声线档案，`/声线` 里立刻能选。想省显存/换云端语音，可以完全不加载 CosyVoice：设 `TTS_PROVIDER=openai` + `TTS_BASE_URL` / `TTS_API_KEY` / `TTS_MODEL`，`/tts` 会直接转发给任意 OpenAI 兼容的语音服务。

### 7.2 翻唱（Seed-VC）的权重与素材

`/听歌 歌名` 的翻唱不是 CosyVoice 出的，链路是：原曲 → demucs 分轨 → Seed-VC 把干声换成机器人平时说话的嗓子 → 混回伴奏。它需要**第二套环境**，安装器不装，要手动补：

| 东西 | 放哪 | 来源 | 是否入库 |
|---|---|---|---|
| Seed-VC 源码 | `audio\seed-vc\seed-vc-main` | 上游 Plachtaa/seed-vc 的 zip 解压 | 否 |
| 推理环境 | `audio\seedvc_env` | 见 [DEPLOYMENT-LESSONS 2.3](DEPLOYMENT-LESSONS.md#23-两个-python-环境关键)（torch 2.7.1+cu128、demucs、accelerate 等） | 否 |
| Seed-VC 基座权重 | 首次运行自动下载 | 上游 HuggingFace，代码里已把 `HF_ENDPOINT` 指向 hf-mirror | 否 |
| demucs 分轨权重 `htdemucs` | 首次运行自动下载 | torch 生态 | 否 |
| 说话嗓子微调权重 `ft_model_*.pth` | `audio\seed-vc\runs\xiaoyi_ft\`，或声线档案目录 + `ckpt.txt` | 作者本地用说话参考音微调得到的 Seed-VC 产物 | 否，单个约 750MB |
| 音色参考 `ref.wav` | `audio\cosyvoice_ref\` | 与 TTS 共用同一份，Seed-VC 的 `--target` 直接指向它 | 是 |

没有 `seedvc_env` 或 Seed-VC 源码时翻唱会失败，但 `/听歌 原曲` 发原曲、`/听歌` 列曲库都不受影响。FFmpeg 必须在 PATH（demucs 分轨和 pydub 都要）。

## 8. 可选组件

**Ollama**：从 <https://ollama.com/download/windows> 安装后按需拉模型，三个模型分工不同：

| 模型 | 拉取命令 | 用来干什么 |
|---|---|---|
| `qwen2.5vl:3b` | `ollama pull qwen2.5vl:3b` | **识图**：表情包自动收录时的粗筛、视频抽帧画面描述（`sticker_pack` / `face_convert` / `subtitle`） |
| `qwen3.5:9b` | `ollama pull qwen3.5:9b` | 小模型档对话（`provider_router` 的群聊/简单消息）、桌宠旁白、识图备选 |
| `nomic-embed-text` | `ollama pull nomic-embed-text` | **语义向量**：长期记忆与知识库检索，不拉则向量召回不可用 |

- 一个都不装也能跑，所有模型调用走云端 provider；只是没有本地识图、本地小模型和向量记忆。
- **表情库要「完整」就绕不开识图模型**：没有本地视觉模型时，群友发来的新图不会丢，但只会进「待核验/待命名」区，需要主人在存储页手动看图命名后入库；关掉自动收录、改成手动管库的配置见 [DEGRADED.md 的表情包一节](DEGRADED.md#表情包ai-主动收录--自己管库)。


**微信桥**：需要一台**已经登录、窗口可见**的桌面微信（桥不负责登录，它用 UIA 附着在已登录的微信上）。

1. 手动打开 PC 微信 4.x，用手机扫码登录，主窗口保持可见——锁屏、最小化到托盘、隐藏窗口都会让 UIA 找不到控件。
2. 启动：`.\services\io-wechat\start-wechat.bat`，看到 `listening on 23392` 表示服务起来了；它每 5 秒自动重试附着，不用重启。
3. 验证：打开 <http://127.0.0.1:23392/> 看状态面板，显示「微信已附着」和机器人昵称即成功（等价于 `curl http://127.0.0.1:23392/health` 返回 `"ok": true`）；`/groups`、`/friends` 能返回列表说明已经读到通讯录。
4. 到 `astrbot_plugin_wechat_oc_bridge` 的 `config.json` 填 `allow_chats`（允许接入哪些会话）——这是安全边界，不填不会转发任何消息。

端口与字段见 `services\io-wechat\service.json`：主端口 23392，另开 23336 兼容旧接口。

> **微信桥只收发文字**：图片、语音、视频在桥里只会变成 `[图片]` / `[语音]` / `[视频]` 占位，不进入 AstrBot 消息链，也不做识别（图片会额外推给桌宠）。语音转写、视频抽帧理解是 QQ 侧 `face_convert`（faster-whisper + Ollama 视觉）和 `web_lookup` 字幕链路的能力，微信侧不生效。

### 8.1 桌宠：先分清三个东西

| 名字 | 是什么 | 要不要装 |
|---|---|---|
| `dsh-pet` | DeepSeek Harness（DSH）的桌宠插件，**本项目实际在跑的桌宠** | 要，见 8.2 |
| `clawd-on-desk` | 上游桌宠壳（`pet/clawd-on-desk` 里是源码，`relay/pet-relay.js` 改造自它） | 不要，仓库里已有源码，不需要再去装它的官方 exe |
| `pet/dafeiyu-web`、`pet/dafeiyu3d` | 前端实验版，没接主链路 | 不用管 |

> 很多人卡住，是因为先下载了 `Clawd-on-Desk-Setup-*.exe` 装成一只独立桌宠——它和 QQ 机器人没有任何连接，装完当然「没事发生」。本项目用的是 `dsh-pet`。

### 8.2 装 dsh-pet（需要本机已有 DSH）

**DSH 目前未公开发布**：没有官方下载页，仓库也不能分发。所以这一步的前提是 `%USERPROFILE%\.dsh\profiles\web` 已经存在（说明你本机装过并运行过 DSH）。没有这个目录，脚本会在第 2 步直接报错退出。

```powershell
# 1) 确认 DSH profile 存在
Test-Path "$env:USERPROFILE\.dsh\profiles\web"     # 应为 True

# 2) 跑安装脚本
powershell -ExecutionPolicy Bypass -File .\pet\dsh-pet\install.ps1
```

脚本做四件事，任何一步失败都会打印原因：

1. 取安装包 `pet\dsh-pet\dsh-pet-0.4.1.tgz`（62.5 MB，**随仓库提供**，clone 后就在）；文件缺失时才从 Release 下载到 `%TEMP%`；
2. `npm install` 到 `%USERPROFILE%\.dsh\profiles\web`；
3. 覆盖 `pet\dsh-pet\patches\` 里的增量补丁；
4. 执行 `ensure:electron` 下载 Electron 运行时，并把 `dsh-pet` 写进 profile 的 `dsh.profile.bundles`。

**验证装成功**

```powershell
Test-Path "$env:USERPROFILE\.dsh\profiles\web\node_modules\dsh-pet\package.json"   # True
Select-String -Path "$env:USERPROFILE\.dsh\profiles\web\package.json" -Pattern "dsh-pet"
```

然后**重启 DSH**，在插件列表里启用 `dsh-pet`，桌面上才会出现桌宠。个人配置在 `%USERPROFILE%\.dsh\dsh-pet`，不进仓库。

### 8.3 让桌宠接上 QQ 机器人（可选，但这一步不做就只是一只装饰）

桌宠本身只显示，不跟机器人通信。要让它说机器人那边的话，需要中继：

```powershell
cd relay
node pet-relay.js            # 监听 23334；也可以双击 launch-relay.vbs 后台跑
```

| 检查 | 期望 |
|---|---|
| <http://127.0.0.1:23334/health> | 返回 JSON |
| <http://127.0.0.1:23334/last> | 能看到最近一条旁白 |
| 桌宠宿主 | <http://127.0.0.1:3080/dsh-pet-7340/state> 能打开 |

`relay/config.json` 里 `pet` 指向 `127.0.0.1:3080`、`brain` 指向 `127.0.0.1:23402`——这两个服务没起，桌宠只会显示不会讲话。消息方向是：AstrBot 插件 `astrbot_plugin_dafeiyu_pet` → `POST 23334/state` → brain 生成旁白 → 桌宠 3080。

### 8.4 桌宠常见卡点

| 现象 | 原因 |
|---|---|
| 脚本报「找不到 DSH web profile」 | 本机没装 DSH，桌宠这条路暂时走不通，等 DSH 公开发布 |
| 安装包缺失或下载失败 | 从 Release 手动下载 `dsh-pet-0.4.1.tgz` 放进 `pet\dsh-pet\` 再跑 |
| 装完桌宠没出现 | 没重启 DSH，或插件列表里没启用 `dsh-pet` |
| 桌宠出现但从不说话 | `relay` 没起，或 brain/model 服务不在线 |
| 想调海草/背景泡泡 | `.\pet\dsh-pet\install.ps1 -Backdrop check\|on\|off` |

### 8.5 音乐库：扫码登录（酷狗 / QQ音乐 / 网易云）

QQ 里的 `/听歌` 只负责「播放/翻唱曲库里已有的歌」，**不联网搜歌**；歌要先入库：主人在 QQ 发 `/扒歌 歌名`，或在网页曲库里加歌。两条路各有自己的登录：

| 入口 | 怎么登录 | Cookie 存哪 | 不登录的后果 |
|---|---|---|---|
| QQ：`/扒歌 歌名`（仅主人） | 未登录酷狗时会自动发二维码，扫码即完成；咪咕不用登录 | `plugin_data\astrbot_plugin_basic_tools\kugou_cookie.txt` | 酷狗拿不到 VIP 直链，自动回退咪咕，再不行回退网易云接口 |
| 网页：<http://127.0.0.1:23366/ui/songs> | 「音源配置」里点酷狗 / QQ音乐 / 网易云的**扫码登录** | `services\audio\data\source_cookies.json` | 页面搜歌直接返回 `403 音源需要先扫码登录：xxx` |

- 插件**先读**网页那份 `source_cookies.json`，没有才读自己的 `kugou_cookie.txt`——所以在网页扫一次，QQ 侧也认。
- Cookie 会过期，失效后重扫；本地音频不用登录（目录可用 `SONGS_LOCAL_DIR` 指定）。
- 扫码登录用 Playwright 真浏览器，依赖见 [1.2](#12-python-包依赖)。
- 翻唱（`/听歌 歌名`）还要 Seed-VC 那套环境，见 [7.2](#72-翻唱seed-vc的权重与素材)；只要原曲就发 `/听歌 原曲 歌名`，不依赖 Seed-VC。

### 8.6 看视频 / 刷视频：扫码登录（B站 / 抖音 / 快手）

「看视频」有两条独立入口，**Cookie 不互通，各扫一次**：

| 入口 | 打开方式 | 登录位置 | Cookie 存哪 | 不登录的后果 |
|---|---|---|---|---|
| QQ 群/私聊 | `/看视频`、`/视频`、`/刷视频`（`web_lookup` 插件） | 未登录时插件会自动发二维码，扫码即回写 | 插件自己的 `config.json`：`bili_cookie`、`douyin_cookie`、`kuaishou_cookie`、`ixigua_cookie` | B站普通信息仍可用，拿不到 AI 字幕/评论；抖音、快手拿不到评论 |
| 控制台网页 | <http://127.0.0.1:23368/ui/parse>（看视频）<br><http://127.0.0.1:23368/ui/feed>（刷视频） | 页面下方「扫码登录」，B站/抖音/快手各一个按钮 | `services\video\data\parse.json`（B站刷片单在 `feed.json`） | B站单条信息仍可解析；抖音/快手**必须登录**才能刷片单 |

- B站也可以手填：`bili_cookie` 只填 `SESSDATA` 一段也认（代码检测到缺 `buvid3` 会自动补一个）。
- 抖音 / 快手 / 西瓜的二维码靠 Playwright 开真浏览器截屏，需要 `playwright` + `qrcode` 和 Chromium（见 [1.2](#12-python-包依赖)）；没装时页面会提示改用 Cookie 手填。
- 「看视频」的语音转写走 faster-whisper，画面理解走 Ollama 视觉模型（[1.2](#12-python-包依赖) / [1.4](#14-模型与外部程序)）；两者都不影响标题/作者/简介这类纯文本解析。

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
| AstrBot | <http://127.0.0.1:6185> 能登录，插件管理里 24 个插件 |
| NapCat | <http://127.0.0.1:6099/webui> 能打开，QQ 显示已登录 |
| QQ 链路 | AstrBot 日志出现 `aiocqhttp` 已连接；群里发消息机器人有反应 |
| 语音 | `8714/health` 正常；关闭 CosyVoice 后回复仍有声（回退 edge-tts） |
| 微信 | `io-wechat` 的 `/health` 显示已附着窗口；收发一条消息验证 |
| 桌宠 | DSH 插件列表里有 `dsh-pet`，启用后桌面出现桌宠 |

## 10. 核心四服务的启动与配置

```powershell
.\installer\start-core.bat
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
| `astrbot_plugins/function/astrbot_plugin_human_sim/config.json` | `recharge_script`、`recharge_python` | 默认空；只有用「动态出码」功能才填，相对路径按仓库根解析 |

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
| 远程 / 公网访问（有公网 IP 或自有域名） | 只开**反向代理端口**（示例 `8443`） | 用 Nginx / Caddy 反代到 `127.0.0.1:23408` 并加认证，见下文 |
| 远程访问（**没有公网 IP**，家宽 / 校园网） | 只穿透 `23408` | 用樱花 SakuraFrp 内网穿透，见 [12.2](#122-樱花内网穿透sakurafrp) |

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

### 12.2 樱花内网穿透（SakuraFrp）

家宽、校园网通常没有公网 IP，路由器也做不了端口映射。这时用樱花 SakuraFrp 把其余服务都留在内网，只把控制台 `23408` 穿出去。官方文档：<https://doc.natfrp.com/>。

**一、注册与实名**

1. 打开 <https://www.natfrp.com/> 注册账号并完成实名认证（内地用户必须实名，否则无法建立隧道）。
2. 登录后进「管理面板 → 隧道 → 新建隧道」。

**二、创建隧道**

| 字段 | 填什么 |
|---|---|
| 隧道类型 | `TCP`（推荐：不需要域名，也省一条隧道；`HTTP(S)` 类型**必须绑定自己的域名**，内地节点还要求域名已备案） |
| 节点 | **必须选非内地节点**。规定：用 `TCP` 隧道穿透 HTTP 协议时不能走内地节点，选了内地节点会连不上 |
| 本地 IP | `127.0.0.1` |
| 本地端口 | `23408`（hub 控制台） |
| 远程端口 | 留空随机分配，或自己指定一个 |

> 只建这一条隧道。**不要**给 `3000`、`6199`、`6099`、`6185` 建隧道——那等于把 QQ 登录态和 NapCat 控制权直接交到公网。

**三、装启动器并启动**

1. 从 <https://www.natfrp.com/> 或官方文档下载 **SakuraLauncher**（Windows），安装后登录。
2. 在「隧道」标签找到刚建的隧道，打开右上角开关；**不要频繁开关**，开关后等几秒。
3. 切到「日志」标签，看到连接成功即生效，日志里会打印访问地址，形如 `xxx.natfrp.cloud:远程端口`。

**四、访问与验证**

- 外网浏览器打开 `http://<节点域名>:<远程端口>`，应能看到 hub 控制台。
- 本机侧不用为 `23408` 开 Windows 防火墙入站规则——frpc 是**出站**连接，公网方向不经过本机防火墙。
- 打通但打不开时先验本地：`Test-NetConnection 127.0.0.1 -Port 23408`。本地都不通，隧道再对也没用。

**五、安全**

- 隧道用完就关，不要 7×24 挂着。
- 节点支持「访问认证」就打开（部分节点提供），或按 12.1 在 hub 前面加一层 Basic Auth。
- 分享地址时不要发到公开群；`3000`/`6199` 的 token 更不能跟着一起发。
- 手机端只是临时看一眼时，也可以不改链路，直接用 `ssh -L 23408:127.0.0.1:23408` 之类的隧道，更轻。

**六、常见错误**

| 现象 | 原因 |
|---|---|
| 隧道在线但页面打不开 | 本机 `23408` 没起，或 hub 监听的地址不是 `127.0.0.1` |
| 建隧道时没有 `HTTP(S)` 选项 | 该节点不支持建站，换节点；或直接用 `TCP` |
| 选了内地节点 + TCP + HTTP 应用，连不上 | 换成非内地节点 |
| 网页能开但接口全 404 | 反代/穿透只映射了首页路径，改用整端口 TCP 穿透而非路径级反代 |

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
