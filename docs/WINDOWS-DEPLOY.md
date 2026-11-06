# 大肥鱼 QQ 机器人（Windows 原生部署）

数据、配置、插件全部继承自 Docker 版（`stack/`），本机原生运行，不再依赖 Docker Desktop。

## 架构

```
QQ ⇄ NapCat(napcat-win, Shell 版) ⇄ WebSocket 反向(6199, token) ⇄ AstrBot(astrbot-win) ⇄ DeepSeek API
```

## 目录

- `astrbot-win/AstrBot-4.29.3/`：AstrBot 主程序（源码运行）
  - `data/`：从 `stack/data/` 复制的完整数据（配置、插件、人格、表情包、数据库）
  - `cmd_config.json` 已把 3 处 `host.docker.internal` 改为 `127.0.0.1`（Ollama embedding 等）
- `astrbot-win/venv/`：Python 3.13 虚拟环境（requirements.txt + edge-tts + imageio-ffmpeg）
- `napcat-win/`：NapCat Shell Windows 版（自带 QQ 壳，9.9.31-49738）
  - `napcat/config/onebot11_1000000000.json`：反向 WS → `ws://127.0.0.1:6199/ws`，token `change-me-6199-token`
  - `napcat/config/webui.json`：WebUI 6099 端口，token `change-me-webui-token`

## 启动

双击（或命令行运行）：

```bat
C:\path\to\dafeiyu\start-astrbot-win.bat   # 先启动 AstrBot（面板 6185，反向 WS 6199）
C:\path\to\dafeiyu\start-napcat-win.bat    # 再启动 NapCat
C:\path\to\dafeiyu\start-cosyvoice-tts.bat # （可选）本地语音服务 127.0.0.1:8714
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
- 一键安装（本机 Windows 跑一次，需联网 + N 卡）：
  ```bat
  py setup_cosyvoice.py        # 建 venv + 装 torch CUDA + CosyVoice2 + 拉模型 + 启动
  ```
  - 本机是 RTX 5070（Blackwell），脚本已固定 **torch 2.7.1 + cu128**（老 cu121 轮子在 50 系上直接报 no kernel image）。
  - 机器没有 py 启动器 / git 也能装：源码走 codeload zip（Matcha-TTS 子模块单独下到 `third_party/`）；
    文本规整用 `wetext` 代替 Windows 上编不过的 WeTextProcessing。
  - 日常启动用 `start-cosyvoice-tts.bat`（会自动设 `COSYVOICE_REPO_DIR` 指向 `dafeiyu\CosyVoice`）。
- 想要更好听的声音：把一段 3~10 秒干净人声 wav 放到
  `dafeiyu/cosyvoice_models/zero_shot_prompt.wav`（或设 `COSYVOICE_PROMPT_WAV`），克隆出来就是那个声线。
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
