# 插件使用说明

`astrbot_plugins/` 下共 24 个插件，按职责分三个子包，全部基于 AstrBot 4.x 插件接口开发：

- `input/` — 输入层：平台消息/事件接入（group_window、speaker_tag、qzone_channel、wechat_oc_bridge）
- `output/` — 输出层：回复加工与主动推送（humanizer、face_convert、sticker_pack、daily_qzone、dafeiyu_pet）
- `function/` — 功能层：工具、路由与行为（basic_tools、local_tools、web_lookup、qq_mcp、provider_router、tool_gate、human_sim、self_growth、livingmemory、command_guard、prompt_cache、prompt_audit、zz_prompt_dump、dsh_bridge、subtitle_stt）

> 每个插件要额外装什么、哪些可以不装，见 [GETTING-STARTED 3.4 的插件清单表](GETTING-STARTED.md#34-插件是怎么被加载的)。

> AstrBot 只认 `data/plugins/<插件名>` 平铺一级目录，子包仅用于仓库组织；拷贝/安装时自动打散。

## 通用安装方式

1. 把插件目录拷进 AstrBot：

   ```powershell
   # 方式一：直接拷贝（推荐，注意子包层级）
   Copy-Item -Recurse .\astrbot_plugins\function\astrbot_plugin_human_sim <AstrBot目录>\data\plugins\

   # 方式二：用仓库自带脚本，一次装全部
   powershell -ExecutionPolicy Bypass -File .\installer\install-plugins.ps1 -AstrBotData <AstrBot目录>\data
   ```

2. 在 AstrBot WebUI（默认 `http://127.0.0.1:6185`）→ 插件管理 → 重新加载。
3. 每个插件目录里的 `config.json` 就是配置文件，改完重载生效。**所有键都会生效**（早期版本会丢弃不在默认表里的键，已修复）。
4. 插件之间有依赖关系的在下面单独标了；没有标注的都可以单独装。

## 指令速查

| 指令 | 说明 | 权限 |
|---|---|---|
| `/听歌 <歌名>` | 点歌并播放（别名 `/歌曲`） | 所有人 |
| `/听歌 完整 <歌名>` | 直接发曲库里的整首原曲文件 | 所有人 |
| `/扒歌` | 抓取当前播放的歌 | 管理员 |
| `/声线 [名字]` | 不带参数列出声线；带名字切换（如 `/声线 流萤`、`/声线 云希`） | 管理员 |
| `/定时任务` | 查看 / 管理定时任务 | 管理员 |
| `/停` | 暂停插话/语音等行为 | 管理员 |
| `/拉黑` | 拉黑用户或群 | 管理员 |
| `/普通` `/管理` | 查看普通/管理员可用命令 | 所有人/管理员 |
| `/赞我` | 给自己的 QQ 名片点赞 | 所有人 |
| `/禁言 [分钟]` | 让机器人闭嘴，默认 2 分钟 | 所有人 |
| `/解禁` | 解除本群禁言 | 所有人 |
| `/入群 设置 消息｜移除｜测试` | 进群提醒（按群生效） | 管理员 |
| `/退群 设置 消息｜移除｜测试` | 退群提醒（按群生效） | 管理员 |
| `/余额` | 查 DeepSeek 余额（仅官方 key 会话） | 所有人 |
| `/模型 [页码]` | 查看当前会话用的模型 | 所有人 |
| `/转换` | 切换当前会话模型 | 所有人 |
| `/看空间 [QQ号] [条数]` | 看 QQ 空间说说 | 所有人（主人/好友空间不扫） |
| `/视频 [平台] [关键词] [条数]` | 按标签刷 B 站视频池（别名 `/刷视频`） | 所有人 |
| `/看视频 <链接>` | 解析视频出字幕/内容摘要 | 所有人 |
| `/重置` | 重置个人档案 | 所有人 |

## 各插件说明

### 聊天拟人（核心特色）

**astrbot_plugin_human_sim** — 群内拟人行为的调度中枢
- 潜水插话、冷场开话头、冒泡发 QQ 表情、发言带表情包
- 拍一拍全链路：被拍必回、连拍回戳、回完人顺手拍回去
- 被 @ / 叫名字必回（`at_reply_probability` 默认 1.0）；定向判定走云端模型，普通闲聊才用本地 9B，避免小模型把「在叫我」判成沉默
- 回复默认等 1–5 秒（`reply_delay_seconds`），单条 80 字（`max_chars`）；讲故事、展开说等长文请求仍放宽到 `long_max_chars`
- 表情包点单本地直发：`sticker_request_local=true` 时「发个表情/来张图」不进 AI，命中后 1 秒左右出图；同一人连点冷却 4 秒
- 主动发图节奏：`lurk_check_interval_minutes`（巡查间隔，默认 8）、`sticker_probability`（回复带图 0.45）、`sticker_back_cooldown_seconds`（斗图回图冷却 60 秒）、`react_cooldown_seconds`（贴表情冷却 45 秒）
- 语音请求（「发条语音/说给我听」）会交给正式流水线调 `send_voice`，本地决策链不发语音；合成失败自动把内容改成文字发出去
- 所有限频带概率 + 冷却 + 每分钟限额 + 活动时段频控，夜间降频
- 关键配置：`watch_groups`（要监视的群号列表，**必填**）、`interject_probability`（插话概率）、`group_idle_minutes`（多久算冷场）
- 依赖：表情包功能需要 `astrbot_plugin_sticker_pack` 的图库

**astrbot_plugin_humanizer** — 人味处理
- 清洗 Markdown 结构、AI 腔样板句、长句排比，防复读
- 默认一次回复只发一条消息（`multi_bubble_probability=0`），不把回复拆成多条气泡

**astrbot_plugin_group_window** — 群聊动态窗口
- 被动记录每个群的聊天（不唤醒不回复），机器人发言时注入上下文
- 关键配置：`group_ids`（要记录的群）、`max_messages`（窗口大小）

**astrbot_plugin_speaker_tag** — 发言人标签
- 群消息补 `[昵称/时间]:` 标签，每人档案按会话隔离存储并在回复时注入
- 指令：`/重置` 清个人档案

### 声音与多媒体

**astrbot_plugin_basic_tools** — 基础工具集 + 点歌 + 声线
- 指令见速查表；`/声线` 的可选声线 = 克隆档案（`audio/cosyvoice_ref/profiles/` 下含 `ref.wav` 的目录，如 姬子、小忆、流萤、御姐）+ 10 个热门 Edge 音色（晓晓、晓伊、云希等，免素材）
- 依赖：本地语音需要 CosyVoice2 TTS 服务（见 README「快速开始」）；无 GPU 时自动回退 edge-tts
- `/扒歌 歌名`（仅主人）联网扒歌入库：未登录酷狗时自动发二维码，Cookie 存 `plugin_data/astrbot_plugin_basic_tools/kugou_cookie.txt`，并优先复用网页曲库的 `services/audio/data/source_cookies.json`；咪咕免登录，网易云兜底
- `/听歌` 只播放/翻唱曲库已有文件，不联网搜索；翻唱依赖 Seed-VC 独立环境（`audio/seedvc_env` + `audio/seed-vc`），没有时只能 `/听歌 原曲` 发原曲
- 隐私边界：`check_master_activity` 只在私聊查主人自己的记录，群里一律拒绝；转发聊天记录进提示词前会剔除 `/`、`#`、`！` 开头的指令行
- 语音合成（CosyVoice）单段失败不影响其它段；整条都失败时把内容改成文字发出去，不静默丢消息

**astrbot_plugin_sticker_pack** — 表情包引擎
- 回复里的 `:情绪词:` 按语义相似度从全库挑图，单独成条发送
- 文本语义匹配（纯本地算法，不调用大模型）：
  - 中文用 `jieba` 分词后按「词」比对，单字不参与检索——避免「一切」命中「切」、「积累」命中「累」这类误配
  - 分层给分：名字/原句命中 1.0~1.3，同簇标准词封顶 `text_match_core_score`（默认 0.85），近义词 0.62，只命中情绪字时 ≤0.46 作为长尾；近义层够不到核心线，只能补池
  - 否定式单独成簇（不开心→委屈、不好笑→无语），查询和图库两侧都做否定判定，「不开心」里的「开心」不算命中
  - `jieba` 是可选依赖：装了就按词匹配；没装自动退回二元组分词，功能可用、精度略降
  - 相关配置：`text_match_core_score`、`text_match_min_score`、`text_match_topk`
- 自动配 QQ 黄脸：正文命中情绪规则后补一张脸，走独立配额，不跟贴图共用 120s/5 条那套闸
  - 相关配置：`auto_face_probability`（默认 0.6）、`auto_face_min_chars`、`face_min_gap_seconds`（默认 45）、`face_text_per_face`（默认 2）
- 支持收藏（fav）与查重：图片指纹只在收藏入库时使用
- 自动收录需要本地识图模型（默认探测 Ollama `qwen2.5vl:3b`，可在 `vision_model` 指定）；没有识图模型时新图进「待核验/待命名」区，要人工命名才入库；`fav_vision_check` 控制是否为每张图先做一次视觉粗筛
- 关键配置：`unlimited_groups`（不限流群）、`follow_up_probability`（补图概率）、`vision_model`
- 图库结构：`map.txt` 索引 + `fav/` 收藏 + `pool/` 候选（参考仓库 `astrbot-win/.../stickers/`）

**astrbot_plugin_face_convert** — 文字表情转 QQ 表情
- 把 `[流泪]` `[doge]` 等转成真实 QQ 表情段，无配置
- 同一插件还承担 QQ 侧的语音/视频识别：语音消息用 `faster-whisper` 转写，视频抽帧后的画面描述统一走中枢「识图模型」（见下方「识图模型」）；该模型没选时不识图、也不加载本地模型，消息里只留 `[识图失败，请在 中枢→模型→识图模型 设置识图模型]` 占位，不影响收发
- 微信桥不转发语音/视频，所以这套识别只对 QQ 侧生效

**astrbot_plugin_web_lookup** — 联网查证 + 视频
- 搜索候选 → 校验 → 抓正文 → 相似度挑最相关页面
- 也支持直接给链接抓正文、B 站视频字幕、按标签刷视频（片单池）
- 关键配置：`bili_tags`（B 站标签）、`bili_cookie` / `douyin_cookie` / `kuaishou_cookie` / `ixigua_cookie`（扫码登录后自动写入；B 站只填 `SESSDATA` 也认）
- 扫码登录：未登录时插件自动发二维码，B 站走官方接口，抖音/快手/西瓜用 Playwright 开真浏览器截屏（需 `playwright` + `qrcode` + Chromium）
- 与 `services/video` 的 Cookie 不互通：QQ 里的 `/看视频` 用插件 `config.json`，网页看视频/刷视频用 `services/video/data/parse.json`，各扫一次

### QQ 空间

**astrbot_plugin_qzone_channel** — 空间独立信道
- 轮询自己说说下的新评论，用独立上下文回复，不混入群聊/私聊记忆
- 关键配置：`manual_cookie`（内置浏览器登录后复制进来，含 p_skey/uin 才有效）、`poll_interval_minutes`
- 回完一条评论后 60 秒补查一次，接住对方马上追问；没有新评论才回到正常轮询间隔
- 指令：`/看空间 [QQ号] [条数]`，不带参数看自己；只扒非好友的公开空间，主人和好友的空间一律不扫（不分管理员级别，避免把别人之间的互动带出来）；群聊素材不受影响

**astrbot_plugin_daily_qzone** — 每日自动发说说
- 内容优先取当日群聊总结，概率配表情包图
- 关键配置：`post_hours`（几点发）、`enabled`

### 模型与权限

**astrbot_plugin_provider_router** — 模型路由
- 群聊、好友私聊、微信各走各的模型；陌生人私聊一律本地，不烧云端 token。配置丢失时群聊/私聊兜底 DeepSeek，不会静默降级到本地小模型
- 关键配置：`group_provider_id` / `private_provider_id` / `stranger_private_provider_id` / `wechat_provider_id`；`deepseek_group_ids` + `smart_route_enabled` 控制哪些群允许简单问题切小模型
- 健康探针会定期检查各 provider，挂了按档位冷却后重试

**astrbot_plugin_tool_gate** — 按群接入门控
- 按群决定模型/人设/工具集，支持一键预设与私聊接入（可视化页维护）
- 关键配置：`owner_ids`（管理员 QQ 号）、`owner_full_access`

**astrbot_plugin_local_tools** — 本机工具
- 给 AI 本机文件读写与命令执行权限，默认仅主人可用
- 非主人开启时退化为 workspace 沙箱且无命令执行

**astrbot_plugin_livingmemory** — 长期记忆
- 提取、召回、遗忘三段生命周期；召回按相关度挑，不整库塞进 prompt
- 依赖：`networkx jieba pytz aiofiles aiosqlite`（插件目录自带 `requirements.txt`）

**astrbot_plugin_command_guard** — 命令短路
- 命令类 handler 已经回过话时跳过默认 LLM，省一次模型调用
- 无配置，装上即生效

**astrbot_plugin_prompt_cache** — 前缀缓存优化
- 裁掉用不上的技能清单，把 system prompt 的动态段压到末尾，提高 DeepSeek 前缀缓存命中率
- 无配置；换模型/改人设后第一次请求本来就会 miss

**astrbot_plugin_prompt_audit** — 缓存诊断
- 只读记录每次 LLM 请求各段字符数与前缀哈希，用来确认缓存有没有命中，不影响对话

**astrbot_plugin_zz_prompt_dump** — 调试 dump
- 把 LLM 请求/回复全链路写盘（总流 + 按群独立 JSON），供 llm-monitor 可视化
- `zz_` 前缀保证最后加载；平时别开，会把聊天内容落盘

**astrbot_plugin_self_growth** — 自主成长
- AI 记经验教训到成长笔记，自动注入系统提示，支持按关键词遗忘
- 无配置，装上即生效

### 识图模型（全局开关）

「中枢 → 模型 → 识图模型」是唯一的识图入口，配置落在
`astrbot-win/AstrBot-4.29.3/data/local_tools/vision.json` 的 `provider_id`：

- **选了模型**：表情包识图、头像识别、视频抽帧描述、图片画面描述全部走这一个模型
- **选「不识别（关闭）」或留空**：以上功能一律跳过——不调字幕服务、不连本地 Ollama、不加载任何视觉模型；
  群里收到图片只会留一条占位提示，等配置好再看
- 为什么默认关：本地 9B 视觉模型一加载就是 6.3 GB，会把同卡的 CosyVoice 语音合成挤到十几秒一句；
  只做聊天时没有任何识图需求，让它常驻纯属浪费
- 关掉后想恢复：在中枢页面选中任意带图片能力的模型即可，改完立即生效，不用重启

### 平台桥接

**astrbot_plugin_dafeiyu_pet** — 桌宠状态桥
- 把 QQ/微信消息与回复状态发给桌宠中转服务（默认 `127.0.0.1:23334`）
- 推送内容按原台词切段（默认 60 字一句）依次投递，不经过任何模型改写/旁白；
  `/`、`／` 开头的消息按指令处理，不气泡、不发声
- 克隆音色合成失败时只保留字幕，不回落在线 Edge（避免外网 TTS 卡 60~90 秒）
- 桌宠只做状态面板，不共享机器人人格与记忆
- 依赖：`relay/pet-relay.js` 在跑

**astrbot_plugin_qq_mcp** — QQ MCP 工具
- 发消息（拆条+打字节奏）/查历史/戳一戳封装成 MCP streamable-http，供 DSH（桌宠宿主）直接调用

**astrbot_plugin_wechat_oc_bridge** — 微信接入
- 把本机 io-wechat（PC 微信小号 RPA）的消息接入 AstrBot，回复走同一个小号
- 依赖：`services/io-wechat` 在跑；关键配置 `allow_chats`（允许哪些会话，**安全边界**）

**astrbot_plugin_dsh_bridge** — DSH 外部 agent
- 在插件里注册 `dsh_bridge` provider，把 DSH 当模型用，AstrBot 升级时不用再改 core
- 依赖：本机 DSH（`%USERPROFILE%\.dsh\profiles\web`）+ 桥接服务 `127.0.0.1:18081`

**astrbot_plugin_subtitle_stt** — 语音转字幕
- 注册 `subtitle_stt` provider，把语音消息转成文字再交给模型
- 依赖：`faster-whisper`；首次使用会下载模型

## 生效清单：改了东西还要做什么

光改仓库文件通常不会自动生效，按下表补齐动作：

| 改了什么 | 必须做的动作 | 生效范围 / 注意 |
|---|---|---|
| **身份（persona）**：`services/hub/seeds/personas.json` 新增 / 修改 | 1）导入人格库：中枢管理页「人格库 → 导入预置」，或 AstrBot WebUI 人格管理里导入，或直接写 `<AstrBotHome>\data\data_v4.db` 的 `personas` 表（`persona_id` + `system_prompt`）；2）到 `astrbot_plugin_tool_gate` 可视化页，或 `data\local_tools\access.json` 的 `groups[群号/QQ]` 里给指定群 / 人填 `persona_id` | 只改 `personas.json` 不会出现在聊天里；没绑定 `persona_id` 的会话继续用默认人格。中枢导入新增人格后会重启 AstrBot（`services/hub/hubsrv/botctl.py` 的 `import_personas()`） |
| **脑人设**：`stack/persona-*.md`、`roles.json` 的 `persona_files` | 重启 brain（23402）；该角色下一次 `/chat` 或 `/event` 时重新生成 `~/.dsh/profiles/brain-*/cordis.patch.yml` | `cordis.patch.yml` 是生成物，直接改它会在下次生成时被覆盖；旧会话的 system prompt 已写进会话事件，必要时重建会话 |
| **桌宠**：`pet/dsh-pet/` 的补丁 / 前端 | 运行 `pet\dsh-pet\install.ps1`（或重新安装 / 重启桌宠） | 只把文件复制过去不生效；install 脚本才会把补丁装到运行副本 |
| **声线**：`audio/cosyvoice_ref/profiles/`、`basic_tools` 的声线配置 | 在 AstrBot WebUI 重新加载 `astrbot_plugin_basic_tools`；确认 CosyVoice2 TTS 服务在跑 | 下一条语音才用新声线；无 GPU 时自动回退 edge-tts |
| **插件**：插件代码或 `config.json` | 拷到 `<AstrBotHome>\data\plugins\` 后，在 WebUI 插件管理里「重新加载」；加载期逻辑改动要重启 AstrBot | 只改文件不重载不生效；旧版本还会丢弃白名单外的配置键 |
| **brain 管理员**：增删主人 | 改 AstrBot `<AstrBotHome>\data\cmd_config.json` 的 `admins_id` | `roles.json` 的 `admins` 只是额外补充，真实管理员以 `admins_id` 为准；按 mtime 缓存，改完**不需要重启 brain**（`services/brain/roles/config.py` 的 `external_admins()`） |

> 安装、配置项的详细说明见上文；这张表只列「改完还必须做什么」。

## 常见问题

- **改了 config.json 没生效？** 在 AstrBot WebUI 里重新加载插件。旧版本会把不在 DEFAULTS 白名单里的键静默丢弃，请先更新到仓库最新代码。
- **`/声线` 切了没声音？** 确认 CosyVoice TTS 服务在跑（`installer/start-cosyvoice-tts.bat`），下一条语音才生效。
- **表情包发不出来？** 图库路径没配对；`map.txt` 里的相对路径要能从插件工作目录解析到。
- **看空间报错？** `manual_cookie` 失效了，用内置浏览器重新登录 QQ 空间后把新 cookie 填回去。
