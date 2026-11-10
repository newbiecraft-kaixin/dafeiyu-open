# 微服务多机部署指南

> 适用场景：一台机器跑不下全套（比如语音要 GPU、微信/QQ 要桌面），把服务拆到多台 Windows 机器上分摊。
>
> **先读这段**：本项目按「单机回环」设计——所有自研服务默认只监听 `127.0.0.1`，服务间调用大量硬编码回环地址（逐项清单见[第 7 节](#7-当前版本的局限硬编码清单)）。跨机部署**可行，但有明确代价**：要么用 Windows 自带的 `netsh portproxy` 做端口转发（不改代码，见 3.3），要么改代码把回环地址参数化（见第 7 节）。第 3 节的三机方案以「不改代码」为前提。
>
> 单机部署看 [GETTING-STARTED.md](GETTING-STARTED.md)；本文不重复安装器用法，只讲拆机。

## 1. 架构总览

服务清单以 `services/*/service.json` 为准（共 14 个），hub 控制台 <http://127.0.0.1:23408> 按下面五层展示：

| 层 | 服务 | 一句话职责 |
|---|---|---|
| 中枢层 | hub (23408) | 能力注册中心 + MCP 风格工具调用；扫描 `services/*/service.json` 聚合健康状态，控制台启停本机服务 |
| 中枢层 | model (23406) | 模型路由：身份 → provider/model/key，OpenAI 兼容 / Ollama / 本地 harness 统一入口 |
| 中枢层 | store (23407) | 统一记忆层（SQLite）：会话、消息、长期事实 |
| 中枢层 | brain (23402) | 角色路由 + 人设 + 会话 + 记忆编排；模型调用交给 model 层 |
| 输入输出层 | astrbot (6185 / WS 6199) | QQ 机器人宿主，24 个自研插件跑在它里面 |
| 软件层 | napcat (3000 / WebUI 6099) | QQ 协议端：OneBot HTTP 3000 + 反向 WS 6199，与 QQ 客户端同机 |
| 软件层 | io-wechat (23392 / 旧 23336) | 微信桌面桥：Windows UIA 驱动桌面微信收发，必须与桌面微信同机 |
| 能力层 | voice (8714) | CosyVoice2 本地情感 TTS，独立 venv + NVIDIA GPU |
| 能力层 | audio (23366，副端口 23405) | 音色训练（seed-vc）、歌曲库、翻唱管理；**subtitle 已并入本进程**（whisper 转写 + Ollama 画面描述） |
| 能力层 | subtitle (23405) | 已并入 audio，无独立进程；旧调用方继续打 23405 |
| 能力层 | video (23368) | B站/抖音/快手/西瓜链接解析、字幕、评论，刷 B站片单 |
| 能力层 | stickers (8712) | 表情/图片存储与对外分享（`_share` 链接）、tool_gate 接入管理页；**清单 entry 脚本不在仓库中，当前无法启动**（见 3.5） |
| 监控层 | pet (23334) | pet-relay：桌宠旁路，`/state` → brain → 桌宠窗口气泡旁白 |
| 监控层 | dsh (3080) | DeepSeek Harness 桌宠宿主：桌宠窗口、OCR 点评（未公开组件，仅限已装 DSH 的机器） |

分层归属定义在 `services/hub/hubsrv/config.py` 的 `LAYER_BY_NAME`。

## 2. 单机标准部署（简述）

单机是全部的前提，先保证一台机器能跑通：

1. 按 [GETTING-STARTED 第 4 节](GETTING-STARTED.md#4-方式一一键安装推荐)执行 `.\install.bat`，选装组件；
2. 核心：`installer\start-core.bat`（hub/model/store/brain 四服务，脚本按自身位置定位仓库根，端口已监听则跳过）；
3. 验收：[GETTING-STARTED 第 9 节](GETTING-STARTED.md#9-逐项验收)逐个打 `/health`；
4. 端口速查与防火墙：[GETTING-STARTED 第 12 节](GETTING-STARTED.md#12-端口速查)。

多机部署的每台机器都从「单机装好自己那部分」开始，再打通跨机寻址。

## 3. 多机分工方案（三机示例）

### 3.1 分工与硬约束

以下分工**根据代码里的真实耦合做了修正**：brain 与 store 不能分机（见下），所以 store 跟中枢放 A 机，而不是和媒体服务一起。

| 机器 | 定位 | 部署的服务 | 硬约束（为什么是这些） |
|---|---|---|---|
| **A 机（接入与中枢）** | 与 QQ/微信客户端同机的常开机器 | hub 23408、astrbot 6185/6199、napcat 3000/6099、io-wechat 23392、**brain 23402、store 23407** | NapCat 要本机 QQ 登录；io-wechat 用 Windows UIA 附着本机桌面微信；**brain 进程内直接 `import store` 的 MemoryStore 打开本地 SQLite 文件（`services/brain/app/state.py` + `services/brain/service.py` 把 `services/store` 加入 `sys.path`），store 必须与 brain 同机** |
| **B 机（语音与模型，建议带 NVIDIA GPU）** | 2~4GB 显存跑 CosyVoice + whisper | voice 8714、audio 23366（含 subtitle 23405 副端口）、model 23406 | audio 与 subtitle 是同一进程；CosyVoice 独立 venv + GPU；model 与 brain 分离靠 `MODEL_URL` 环境变量（`services/brain/harness/client.py` 原生支持，是全项目唯一现成的跨机寻址开关） |
| **C 机（媒体与桌宠）** | 平时用的桌面机 | video 23368、pet-relay 23334、dsh 3080；stickers 8712（见 3.5 的启动限制） | 桌宠窗口（23335）显示在人面前的显示器上；video/stickers 无状态、可自由摆放 |

> brain 和 model 分机是安全的：brain 调 model 只走 HTTP 且支持 `MODEL_URL`。但 **hub 的 MCP 工具**（`voice.say`、`video.look`、`memory.remember`、`pet.speak`）仍硬编码回环地址——即 A 机的 hub 调 B 机 voice、C 机 video 会失败，必须按 3.3 的 portproxy 映射，或按第 7 节改代码。

### 3.2 每台机器的公共步骤

```powershell
# 1. Python 3.12+（AstrBot 硬性要求；核心四服务 3.11+ 也可，全用标准库零第三方依赖）
python -V   # 必须 >= 3.12，否则 winget install --id Python.Python.3.12 -e

# 2. Git 克隆完整仓库（每台机器都克隆完整仓库，service.json 清单和 shared/infra 是共享代码）
git clone https://github.com/newbiecraft-kaixin/dafeiyu-open.git
cd dafeiyu-open

# 3. 按角色装依赖（install.bat 对应项）
#    A 机：-Select 1,2,3,4,5,6   （环境/核心/AstrBot/插件/NapCat/微信桥）
#    B 机：-Select 1,2,7         （环境/核心/CosyVoice，需 NVIDIA 显卡）
#    C 机：-Select 1,2           （环境/核心即可；桌宠另见 pet/dsh-pet/README.md）
.\install.bat -Select 1,2 -Yes
```

依赖规则（详见 [GETTING-STARTED 1.2](GETTING-STARTED.md#12-python-包依赖)）：

- 核心四服务（hub/model/store/brain）只用 Python 标准库，**不需要 pip 装任何东西**；
- AstrBot 插件附加依赖（edge-tts、imageio-ffmpeg、faster-whisper、numpy、requests、jieba）装进 `astrbot-win\venv`；
- 需要扫码登录/刷视频时额外装 `playwright` + `qrcode`（插件所在的 `astrbot-win\venv` 与运行 `services/video` 的解释器各一份，见 [GETTING-STARTED 1.2](GETTING-STARTED.md#12-python-包依赖)）；
- CosyVoice 用 `tools\setup_cosyvoice.py` 建独立 venv（`audio\cosyvoice_env`）+ 下载模型到 `audio\cosyvoice_models`（约 3GB），不要装进 AstrBot 的 venv；
- 微信桥依赖 `uiautomation` + `wechatauto`，装在 `astrbot-win\venv`（仅 A 机需要）。

### 3.3 跨机寻址：真实机制与要改的配置

**先说结论——当前版本的寻址机制是：**

1. 所有自研服务监听 `127.0.0.1`：`shared/infra/config.py` 第 34 行 `HOST = "127.0.0.1"` 是写死的模块常量（不读环境变量），`shared/infra/web/server.py` 的 `serve()/run()` 默认用它绑定端口；
2. 端口号可覆盖：`shared/infra/config.py::port_of()` 支持 `<服务名大写>_PORT` 环境变量（如 `HUB_PORT`、`MODEL_PORT`、`AUDIO_PORT`、`IO_WECHAT_PORT`、`SUBTITLE_PORT`、`VIDEO_PORT`、`STORE_PORT`、`BRAIN_PORT`、`WX_SVC_PORT`），但**只改端口、不改主机**；
3. 跨机唯一现成的开关是 brain → model 的 `MODEL_URL` 环境变量（`services/brain/harness/client.py:52`）；
4. hub 的探活（`hubsrv/probe.py` 用 `socket.connect_ex(("127.0.0.1", port))`）和启停（`hubsrv/manage.py` 用 `subprocess.Popen` 本机起进程）都只针对本机。

因此跨机寻址有两条路：

**路线甲：netsh portproxy（不改代码，推荐先用这个验证）**

原理：在「调用方机器」上把远端服务映射成本机回环端口，硬编码 `127.0.0.1:x` 的代码照常工作；在「服务方机器」上用 portproxy 把服务暴露给局域网（绕过只听回环的限制）。两步都需要管理员 PowerShell。

服务方（B 机，把本机 8714 暴露出去；每个要暴露的端口一条）：

```powershell
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8714 connectaddress=127.0.0.1 connectport=8714
```

调用方（A 机，把 B 机的 8714 映射进本机回环；A 机代码里调 `127.0.0.1:8714` 就会真实到达 B 机）：

```powershell
netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=8714 connectaddress=<B机IP> connectport=8714
```

三机方案需要的映射全景（按部署位置倒推）：

| 映射在哪台 | 监听端口 | 转发到 | 让谁的硬编码生效 |
|---|---|---|---|
| B 机暴露 | 0.0.0.0:8714 / 23366 / 23405 / 23406 | 127.0.0.1 同端口 | voice / audio(含 subtitle) / model 可被局域网访问 |
| C 机暴露 | 0.0.0.0:23368 / 8712 / 23334 | 127.0.0.1 同端口 | video / stickers / pet-relay 可被局域网访问 |
| A 机映射 | 127.0.0.1:8714、23405 | B 机 | hub 的 `voice.say`（tools.py）、botctl 的 TTS 预热、插件 `COSYVOICE_URL`/`SUBTITLE_URL`（若仍填回环） |
| A 机映射 | 127.0.0.1:23406 | B 机 | brain 不设 `MODEL_URL` 时的默认地址（设了就不用映射） |
| A 机映射 | 127.0.0.1:23368、8712、23334 | C 机 | hub 的 `video.look`/`video.feed`、web_lookup 与 music 的 `_share` 链接、hub petctl 与 io-wechat 的 pet_push |

查看与清除：`netsh interface portproxy show all` / `netsh interface portproxy delete v4tov4 listenaddress=... listenport=...`。

**路线乙：改代码参数化（一次改完，长期干净）**

见[第 7 节](#7-当前版本的局限硬编码清单)，核心是给 `shared/infra/config.py` 加 `HOST` 环境变量支持 + 给 hub 的探活/工具调用加「服务 → 主机」映射表。

**每台机器上真正要改的配置（按当前事实汇总）**

| 机器 | 配置点 | 值 | 出处 |
|---|---|---|---|
| A 机 | brain 进程环境变量 `MODEL_URL` | `http://<B机IP>:23406` | `services/brain/harness/client.py`（不设则回退 `127.0.0.1:${MODEL_PORT:-23406}`） |
| A 机 | AstrBot 进程环境变量 `COSYVOICE_URL` | `http://<B机IP>:8714` | `astrbot_plugins/function/astrbot_plugin_basic_tools/voice_engine.py`、`sing_render.py`（环境变量在 AstrBot 启动前设置，插件随宿主进程继承） |
| A 机 | AstrBot 进程环境变量 `SUBTITLE_URL` | `http://<B机IP>:23405` | `astrbot_plugins/output/astrbot_plugin_face_convert/settings.py` |
| C 机 | `relay/config.json` | `brain` 改 `http://<A机IP>:23402`、`ollama` 改 `http://<B机IP>:11434`（pet-relay 无环境变量，只认这个文件） | `relay/pet-relay.js` 启动时读取 |
| B 机 | （可选）subtitle 的 `OLLAMA_BASE` | Ollama 不在 B 机时指向实际地址 | `services/subtitle/subsvc/config.py` |

插件里**配置文件型**的地址无需动代码：`astrbot_plugin_dafeiyu_pet` 的 `relay_url`、`astrbot_plugin_wechat_oc_bridge` 的 `io_base` 都在各自 `config.json`；`face_convert` 的 Ollama 地址走 AstrBot WebUI 的 Provider 配置（`ollama-chat` 前缀 provider 的 `api_base`）。

### 3.4 防火墙

服务方机器（B/C 机）对每个暴露端口加入站规则（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName "dafeiyu voice 8714" -Direction Inbound -Protocol TCP -LocalPort 8714 -Action Allow
```

| 机器 | 需放行 |
|---|---|
| A 机 | 23408（控制台，按需）；QQ/微信链路的 3000/6199/6099/6185 **保持本机闭环，不开入站**（见 [GETTING-STARTED 12.1](GETTING-STARTED.md#121-端口放行与反向代理)） |
| B 机 | 8714、23366、23405、23406 |
| C 机 | 23368、8712、23334 |

局域网内网互信，公网/家宽场景一律走反向代理或内网穿透，不要把上述端口直接映射公网。

### 3.5 各机启动

hub 的「控制台启停」（`hubsrv/manage.py::run(name, 'start/restart/stop')`，即控制台上每个服务卡片的启动/停止按钮）**只能管理 hub 所在机器（A 机）的进程**。B/C 机的服务在各自机器上用命令行/脚本启动：

```powershell
# A 机：核心四服务一键（brain/store 就在其中）
.\installer\start-core.bat
# A 机：QQ / 微信接入
.\installer\start-astrbot-win.bat
.\installer\start-napcat-win.bat
.\services\io-wechat\start-wechat.bat

# B 机：语音与模型（core 脚本会同时拉起 hub/brain/store 的本机副本，不想跑可单独起）
.\installer\start-cosyvoice-tts.bat     # voice 8714
.\services\model\start-model.bat            # model 23406
# audio（23366，含 subtitle 23405 副端口）没有独立 bat，直接：
cd services\audio; python service.py

# C 机：媒体与桌宠
cd services\video; python service.py        # 23368
node relay\pet-relay.js                     # 23334（桌宠中继，只用 Node 内置模块；也可双击 relay\launch-relay.vbs 后台跑）
```

> **stickers 服务当前无法按清单启动**：`services/stickers/service.json` 的 entry 指向 `astrbot-win/AstrBot-4.29.3/data/stickers/tools_source_server.py`，但仓库的示例 data 目录里只有表情包图库，**没有这个脚本**（它只在作者运行目录存在）。`hubsrv/manage.py` 会因 entry 文件缺失拒绝启动它。三机方案建议：stickers 暂不部署，或把该脚本补进仓库后与 AstrBot 同机（A 机）运行。

> B/C 机如果误跑了 `start-core.bat`，会在本机再起一套 hub/brain/store（端口不冲突，但不联网不互通，属于资源浪费）——按需单独启动即可。

## 4. 端口清单表

| 服务 | 端口 | 协议 | 所在机（三机方案） | 覆盖环境变量 |
|---|---|---|---|---|
| hub | 23408 | HTTP | A | `HUB_PORT` |
| model | 23406 | HTTP | B | `MODEL_PORT` |
| store | 23407 | HTTP | A | `STORE_PORT` |
| brain | 23402 | HTTP | A | `BRAIN_PORT` |
| astrbot WebUI | 6185 | HTTP | A | — |
| astrbot aiocqhttp | 6199 | WebSocket | A（NapCat 反向 WS 连入） | — |
| napcat OneBot HTTP | 3000 | HTTP | A | — |
| napcat WebUI | 6099 | HTTP | A | — |
| io-wechat | 23392（旧 23336） | HTTP | A | `IO_WECHAT_PORT`（旧 `WX_SVC_PORT`） |
| voice（CosyVoice） | 8714 | HTTP | B | 清单 `port` |
| audio | 23366 | HTTP | B | `AUDIO_PORT` |
| subtitle（audio 副端口） | 23405 | HTTP | B | `SUBTITLE_PORT` |
| video | 23368 | HTTP | C | `VIDEO_PORT` |
| stickers | 8712 | HTTP | C | 清单 `port` |
| pet（pet-relay） | 23334 | HTTP | C | `relay/config.json` 的 `port` |
| pet 窗口 | 23335 | HTTP | C（桌宠所在桌面） | — |
| dsh | 3080 | HTTP | C | DSH 自身配置 |
| Ollama（可选） | 11434 | HTTP | 建议 B（audio 视觉依赖它） | `OLLAMA_BASE`（subtitle） |

## 5. 服务依赖关系：谁挂了影响什么

hub 控制台 `/health` 与服务卡片会显示在线状态；`hubsrv/manage.py` 启动服务时也会提示「依赖未启动」。

| 挂掉的 服务 | 直接影响 | 链路依据 |
|---|---|---|
| hub | 控制台不可用；`human_sim` 的窗口注入（插件调 `127.0.0.1:23408/windows`）与 `basic_tools` 的主动私聊开窗失效；MCP 工具全部不可用 | 插件反向依赖 hub |
| model | brain 的 `/chat` 失败（角色生成无模型）；AstrBot 若 provider 走本地 model 同样降级，云端 provider 不受影响 | brain → model（HTTP） |
| store | store 服务本身挂了，hub 的 `memory.remember` 工具失效；**但 brain 的记忆不受影响**——brain 进程内直连同一个 SQLite 文件，不走 store 的 HTTP | brain 进程内 import store |
| brain | 微信侧人设/记忆编排失效（io-wechat 相关能力降级）；pet-relay 的旁白生成失败（C 机 relay 调 A 机 brain） | relay/config.json → brain |
| astrbot | QQ 侧完全无反应（收发全断）；微信侧 io-wechat 独立运行但失去 AstrBot 插件链 | NapCat → astrbot 反向 WS |
| napcat | QQ 完全断线；hub「发消息」工具报「napcat 不在线」 | astrbot、hub tools.py → 3000 |
| io-wechat | 微信收发断；hub 群列表微信侧报「微信接入无响应」 | hub botctl → 23392 |
| voice | 本地情感语音停；`basic_tools` 语音自动回退 edge-tts（在线免费 TTS），功能不断 | voice_engine 探测 8714 /health |
| audio（含 subtitle） | 音色训练/歌曲库/翻唱停；whisper 转写与画面描述停；**video 的字幕解析失败**（parse_api 硬编码调 23405） | video → 23405 |
| video | 看视频/刷B站工具停，其它不影响 | hub tools.py → 23368 |
| stickers | 表情包挑选**不受影响**（sticker_pack 直接读 AstrBot `data/stickers` 目录）；受影响的是 web_lookup/music 的 `_share` 外链和 tool_gate 接入管理页 | 8712 只做存储与分享 |
| pet-relay | 桌宠气泡/旁白停，聊天主链路不受影响 | dafeiyu_pet 插件 → 23334 |

总原则：**接入层挂 = 断联；中枢层挂 = 降级但能聊；能力层挂 = 单项功能消失**。机器人「还能不能说话」取决于 astrbot/napcat/model，「说话质量」取决于 brain/store，「有没有声音图视频」取决于能力层。

## 6. 常见问题

**跨机调用返回 401 / 无响应（token 问题）**
hub 调 NapCat 主动发消息前，从**仓库根固定路径** `napcat-win\napcat\config\onebot11.json` 读 OneBot HTTP 的 token（`hubsrv/botctl.py:30`）。如果 A 机仓库下没有这份文件（NapCat 没装到这个位置或没配过 3000 端口），token 读出来是空串，NapCat 开了 token 校验就会 401。处理：把 NapCat 安装/软链到 A 机仓库的 `napcat-win\` 位置并配好 3000 端口的 token；不要把带 token 的配置提交进仓库。

**AstrBot 与 NapCat 必须同机吗？**
QQ 客户端必须与 NapCat 同机（协议端驱动本机 QQ）。AstrBot 与 NapCat **协议上可以分机**（反向 WS 改成对端内网 IP、NapCat 机器放行 3000/6199，见 [GETTING-STARTED 12.1](GETTING-STARTED.md#121-端口放行与反向代理)），但 hub 的 QQ 管控（群列表、主动发消息）硬编码 `127.0.0.1:3000` 且 token 从 A 机本地文件读，**跨机后 hub 侧 QQ 管控失效**。所以本文的三机方案把 astrbot + napcat 一起放在 A 机。

**有没有 watchdog / 进程守护？**
没有。仓库里没有 watchdog 组件。现有的保活机制是：① `installer\start-core.bat` 的 `:ensure_port`——端口已监听就跳过，可重复执行；② hub 控制台每个服务卡片的启动/停止/重启（`hubsrv/manage.py::run(name, action)`），日志写在 `services/hub/data/manage-<服务名>.log`；③ 服务进程挂了不会自动拉起，需要再次执行启动脚本或在控制台点重启。

**日志在哪里？**
- hub manage 启动的进程：`services\hub\data\manage-*.log`；
- `start-core.bat` 启动的服务：`logs\<服务名>-core.log`、`logs\core-run.log`；
- AstrBot：`<AstrBot 目录>\data\logs\`；
- NapCat：WebUI（6099）内查看；
- DSH 网页 token 日志（hub 引用它拼 3080 的带 token 链接）：`%USERPROFILE%\.dsh\web-server.out.log`。

**B 机的服务起来了，A 机控制台显示离线？**
正常现象：hub 探活只连 `127.0.0.1`（`hubsrv/probe.py`），看不到远端机器的端口。B/C 机服务是否存活要到那台机器上 `Test-NetConnection 127.0.0.1 -Port <端口>` 自查。想让 hub 正确显示远端状态需要第 7 节的代码改造。

**跨机后表情包/图片链接发到 QQ 打不开？**
`astrbot_plugin_web_lookup` 和 `basic_tools` 的音乐分享把图片落到 stickers（8712）并生成 `http://127.0.0.1:8712/_share/...` 硬编码链接发给好友——这个回环地址对方永远打不开。`web_lookup` 有 `video_page_url` 配置可指定对外地址（留空用本机 8712），`_share` 链接本身则是写死的。最省事的做法：stickers 留在与 AstrBot 同机（A 机），C 机就不放它。

## 7. 当前版本的局限：硬编码清单

诚实结论：**除 brain→model（`MODEL_URL`）外，跨机部署没有任何现成配置开关**。以下按「跨机时会不会断」列出全部事实依据（文件:行为），改法附后。

**监听侧（影响所有自研服务）**

| 位置 | 现状 | 改法 |
|---|---|---|
| `shared/infra/config.py:34` | `HOST = "127.0.0.1"` 常量，`base_url()` 也用它拼地址 | 改成 `HOST = os.environ.get("DAFEIYU_HOST", "127.0.0.1")`，各服务启动前设置环境变量即可对外监听 |
| `shared/infra/web/server.py:271,308,325` | `serve()/serve_in_thread()/run()` 默认 `host=HOST` | 随上条自动生效 |

**hub 侧（探活/启停/工具全部只认回环）**

| 位置 | 现状 | 改法 |
|---|---|---|
| `hubsrv/probe.py:20` | 探活 `connect_ex(("127.0.0.1", port))` | 增加「服务名 → 主机」映射（环境变量如 `VOICE_HOST=<B机IP>`），按清单 `name` 查主机 |
| `hubsrv/registry.py:95,261` | health 探测与 DSH token 校验硬编码回环 | 同上 |
| `hubsrv/manage.py:170,212` | `/health` 就绪探测回环；`subprocess.Popen` 只能本机起进程 | 远端服务不做本地启停（保留状态展示），或接入远程 agent |
| `hubsrv/tools.py:23,90,145,160,180,193,213` | MCP 工具目标写死：pet 23334、voice 8714、napcat 3000、io-wechat `manifest.port`、store 23407、video 23368（均 `127.0.0.1`） | 统一改走 `base_url(name)` 并让 `base_url` 支持 `DAFEIYU_<NAME>_HOST` |
| `hubsrv/botctl.py:521,550,605,1772` | `WECHAT_API=127.0.0.1:23392`、napcat 3000、voice 8714 写死 | 同上 |
| `hubsrv/petctl.py:137,163,166,168` | relay 23334、pet 窗口 23335、ollama 11434、brain 23402 写死 | 同上 |
| `hubsrv/views.py:14,23` | `/view` 代理只允许 loopback 目标（安全设计） | 如需代理远端页面需把远端主机加入白名单集合 |

**服务间调用**

| 位置 | 现状 | 改法 |
|---|---|---|
| `services/video/parse_api.py:62,1121,1130` | video → subtitle(23405) 硬编码回环 | 改 `os.environ.get("SUBTITLE_BASE", "http://127.0.0.1:23405")` |
| `services/io-wechat/wxsvc/pet_push.py:18` | io-wechat → pet-relay 23334 写死 | 同上改为环境变量 |
| `services/brain/app/state.py:19` + `services/brain/service.py:28` | brain 进程内直连 store 的 SQLite（**必须同机的根因**） | 若要拆开，需把 MemoryStore 调用改为 HTTP 调 store 服务（工作量：brain/app/memory.py、memory_api.py、status.py 全部改走 HTTP） |

**AstrBot 插件侧**

| 位置 | 现状 | 改法 |
|---|---|---|
| `astrbot_plugin_human_sim/human_sim_core/pipeline.py:126`、`astrbot_plugin_basic_tools/basic_core/messaging.py:349` | 调 hub `127.0.0.1:23408/windows` 写死 | 改环境变量（如 `HUB_BASE`） |
| `astrbot_plugin_web_lookup/main.py:80`、`astrbot_plugin_basic_tools/basic_core/music.py:242` | `_share` 链接写死 `127.0.0.1:8712` | 改为可配置对外地址（注意这是发给 QQ 好友的链接，应是可达地址） |
| `astrbot_plugin_sticker_pack/collect.py:198-269`、`astrbot_plugin_basic_tools/cover_render.py:202,213` | Ollama `127.0.0.1:11434` 写死 | 改环境变量或读 AstrBot provider 配置（face_convert 已是这种做法） |

**已支持跨机的点（不用改）**：`MODEL_URL`（brain→model）、`COSYVOICE_URL`（basic_tools 语音/唱歌 → voice）、`SUBTITLE_URL`（face_convert → subtitle/audio）、`OLLAMA_BASE`（subtitle 视觉）、`relay_url`（dafeiyu_pet，config.json）、`io_base`（wechat_oc_bridge，config.json）、`relay/config.json`（pet-relay → brain/ollama/pet）。

**清单小结**：三机方案里真正被硬编码卡住的链路是——hub 的 MCP 工具（voice/video/memory/pet/napcat）、video 的转写回调、插件到 hub 的窗口注入、`_share` 外链、以及 Ollama 相关插件。用 portproxy 路线可全部绕过；用改码路线优先改 `shared/infra/config.py` 的 `HOST` 和 hub 的探活/工具两处，收益最大。

---

单机安装与验收见 [GETTING-STARTED.md](GETTING-STARTED.md)，架构说明见 [ARCHITECTURE.md](ARCHITECTURE.md)，降级形态见 [DEGRADED.md](DEGRADED.md)。
