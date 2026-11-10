# 大肥鱼 QQ 机器人（Windows 原生部署）

> **这是作者本机的部署记录，不是通用安装指南。** 文中的目录名（如 `astrbot-win/AstrBot-4.29.3`）、账号密码、端口与硬件（RTX 5070）都是本机示例；AstrBot 主程序并不在仓库里，需要按官方渠道自行安装。
>
> 干净机器请从 [`docs/GETTING-STARTED.md`](GETTING-STARTED.md) 开始：上游组件的官方下载地址、版本、安装位置与验收方式都写在那里。本文只保留本机排障与细节。
>
> 真实的迁移事故、开源组件的坑与排障速查表见 [`docs/DEPLOYMENT-LESSONS.md`](DEPLOYMENT-LESSONS.md)。

数据、配置、插件全部继承自 Docker 版（`stack/`），本机原生运行，不再依赖 Docker Desktop。

> 新手推荐直接双击仓库根目录的 `install.bat`：菜单式安装会自动完成下文大部分步骤（建 venv、下载 AstrBot 与 NapCat、装插件、装桌宠）。

## 架构

```
QQ ⇄ NapCat(napcat-win, Shell 版) ⇄ 反向 WS 6199 / HTTP 3000(token) ⇄ AstrBot(astrbot-win) ⇄ DeepSeek API
```

## 目录

- `astrbot-win/AstrBot-4.29.3/`：本机的 AstrBot 目录（源码运行）
  - 仓库里**只带 `data/` 示例数据**（配置模板、表情包、文生图模板）；主程序用 `install.bat` 第 3 项或官方 Launcher 安装
  - `cmd_config.json` 已把 3 处 `host.docker.internal` 改为 `127.0.0.1`（Ollama embedding 等）
- `astrbot-win/venv/`：Python 3.13 虚拟环境（requirements.txt + edge-tts + imageio-ffmpeg）
- `napcat-win/`：NapCat Shell Windows 版（自带 QQ 壳，9.9.31-49738）
  - `napcat/config/onebot11_1000000000.json`：反向 WS → `ws://127.0.0.1:6199/ws`，token `change-me-6199-token`
  - 同一个文件里还要有 **HTTP 服务器**：`host 127.0.0.1`、`port 3000`、token 同上。只配反向 WS 时机器人只能被动回复，hub 主动发消息会提示「napcat 不在线」
  - `napcat/config/webui.json`：WebUI 6099 端口，token `change-me-webui-token`

## 仓库内随附的素材与配置

克隆后下列文件已就位，不需要额外下载；**与代码里的相对路径完全对应**，按原路径覆盖即可：

| 路径 | 内容 | 说明 |
|---|---|---|
| `astrbot-win/AstrBot-4.29.3/data/stickers/` | 472 个表情包 + `map.txt`（412 行索引）+ `fav/` 收藏库 | 表情包插件开箱可用 |
| `astrbot-win/AstrBot-4.29.3/data/config/*.json` | pet / humanizer / livingmemory / web_lookup 四个插件配置 | **token、cookie 等密钥字段已清空**，按需填写 |
| `astrbot-win/AstrBot-4.29.3/data/local_tools/tool_names.json` | 本地工具名册 | 工具调用路由用 |
| `astrbot-win/AstrBot-4.29.3/data/t2i_templates/*.html` | 文生图 HTML 模板 | 回复转图片用 |
| `audio/cosyvoice_ref/profiles/<声线>/` | `ref.wav` + `prompt.txt`（5 组） | 克隆声线参考音；`.pth` 微调权重不入库 |

仍需自行准备（体积过大或含账号信息，不入库）：

- `audio/CosyVoice/` 与模型权重、`audio/cosyvoice_models/` 下的 `.pth`
- Ollama 模型（识图用 `qwen2.5vl:3b`，约 3.2GB）
- AstrBot 主程序的 `cmd_config.json`（JWT 密钥、provider 密钥都在这里，首次启动自动生成后按需填）
- 运行期数据：`data/plugin_data/`（语音缓存、记忆库）、`data/logs/`、`data/temp/`

## 启动

在仓库根目录执行（脚本会按自身位置定位仓库，不需要改路径）：

```bat
installer\start-astrbot-win.bat   :: 先启动 AstrBot（面板 6185，反向 WS 6199）
installer\start-napcat-win.bat    :: 再启动 NapCat
installer\start-cosyvoice-tts.bat :: （可选）本地语音服务 127.0.0.1:8714
```

首次启动 NapCat 需要登录 QQ 1000000000：

1. 浏览器打开 `http://127.0.0.1:6099/webui`，token 填 `change-me-webui-token`
2. 用大肥鱼 QQ 扫码 / 快速登录
3. 登录成功后 NapCat 自动连上 AstrBot（AstrBot 日志出现 aiocqhttp 连接即成功）

## 入口

| 用途 | 地址 | 凭据 |
|---|---|---|
| AstrBot 面板 | http://localhost:6185 | `astrbot` / `change-me-panel-password` |
| NapCat WebUI | http://127.0.0.1:6099/webui | token `change-me-webui-token` |

## 语音引擎（可选，CosyVoice2-0.5B）

机器人发语音用的是本地情感 TTS：**CosyVoice2-0.5B**（阿里通义，Apache-2.0，2~4GB 显存）。
为什么换它：旧方案 edge-tts 只能整体升降调，做不出情感音色；CosyVoice 用自然语言指令
（`用开心的语气说`）做**音调/情绪**控制，并能**克隆**一段参考声线 = 声音更好听。

- 服务独立进程 `cosyvoice_tts_server.py`（127.0.0.1:8714），模型常驻显存，不拖 AstrBot。
- 一键安装（本机 Windows 跑一次，需联网 + N 卡，仓库根目录执行）：
  ```bat
  python tools\setup_cosyvoice.py   :: 建 venv + 装 torch CUDA + 拉 CosyVoice2 源码与模型 + 启动
  ```
  - 本机是 RTX 5070（Blackwell），脚本已固定 **torch 2.7.1 + cu128**（老 cu121 轮子在 50 系上直接报 no kernel image）。
  - 机器没有 py 启动器 / git 也能装：源码走 codeload zip（Matcha-TTS 子模块单独下到 `tools\` 下）；
    文本规整用 `wetext` 代替 Windows 上编不过的 WeTextProcessing。
  - 日常启动用 `installer\start-cosyvoice-tts.bat`（会自动设 `COSYVOICE_REPO_DIR` 指向 `audio\CosyVoice`）。
- 想要更好听的声音：把一段 3~10 秒干净人声 wav 放到
  `audio\cosyvoice_models\zero_shot_prompt.wav`（或设 `COSYVOICE_PROMPT_WAV`），克隆出来就是那个声线。
- `voice_engine.py` 默认优先调本地 CosyVoice；服务没起/没就绪时自动回退 edge-tts（不报错）。
- 状态：`http://127.0.0.1:8714/health`。

## 点赞（本地方言全覆盖）

「赞 / 赞我 / 点赞 / 给我点个赞」→ 本地点赞 + 回一句话，不进 AI。
**带目标**的也认了（此前「给万川姐姐点赞」会漏判，只能靠 AI 多花一轮）：

- `给 @某人 点赞`（At 段直接拿 uid）
- `麻烦给 1000000000 点赞`（原文里的裸 QQ 号）
- `给万川姐姐点赞`（群名片/昵称：精确命中 > 剥称呼后缀「姐姐/大佬/哥…」> 互相包含 > 相似度；
  两字名相似度门槛 0.75，解析不出就交 AI 带名册兜底，**绝不瞎点**）
- 点完 @ 托付的人交代一声；同一人 30 秒冷却，每人每天 10 个赞（QQ 侧限制）由本地记账挡住。

## 表情回应接收

用户在你发出的消息/表情下贴表情回应（QQ 消息右下角小黄脸）时，机器人现在能**收到并感知**：
`human_sim` 插件的 `_on_group_notice` 新增 `group_msg_emoji_like` 分支，靠「自己发过的 message_id
记录 + `get_msg` 反查发送者」判定是不是 AI 的消息，记录「谁对哪条消息贴了什么」，并默认**回贴一个**
形成对称互动（群聊冷却内不重复贴）；对方用了机器人反应池里的表情时，小概率额外回一句文字。
诊断：`/表情回应` 看最近收到的回应。开关：`react_back_on_reaction` / `react_back_text_probability`（config.json）。

## 停止

两个控制台窗口直接关掉即可。开机自启可把两个 bat 的快捷方式放进 `shell:startup`。

## 与 Docker 版的关系

- Docker 栈已 `docker compose down`，镜像和 `stack/` 目录保留，随时可用 `docker compose up -d` 切回
- 两套共用 6185/6199/3000/6099 端口，**不要同时启动**
- 若之后在 Docker 版里改了配置/插件，把 `stack/data` 重新拷到 `astrbot-win/AstrBot-4.29.3/data` 覆盖即可
