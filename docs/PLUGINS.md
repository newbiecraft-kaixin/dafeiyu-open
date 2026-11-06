# 插件使用说明

`astrbot_plugins/` 下共 18 个插件，全部基于 AstrBot 4.x 插件接口开发。

## 通用安装方式

1. 把插件目录拷进 AstrBot：

   ```powershell
   # 方式一：直接拷贝（推荐）
   Copy-Item -Recurse .\astrbot_plugins\astrbot_plugin_human_sim <AstrBot目录>\data\plugins\

   # 方式二：用仓库自带脚本，一次装全部
   powershell -ExecutionPolicy Bypass -File .\tools\windows\install-plugins.ps1 -AstrBotData <AstrBot目录>\data
   ```

2. 在 AstrBot WebUI（默认 `http://127.0.0.1:6185`）→ 插件管理 → 重新加载。
3. 每个插件目录里的 `config.json` 就是配置文件，改完重载生效。**所有键都会生效**（早期版本会丢弃不在默认表里的键，已修复）。
4. 插件之间有依赖关系的在下面单独标了；没有标注的都可以单独装。

## 指令速查

| 指令 | 说明 | 权限 |
|---|---|---|
| `/听歌 <歌名>` | 点歌并播放（别名 `/歌曲`） | 所有人 |
| `/扒歌` | 抓取当前播放的歌 | 管理员 |
| `/声线 [名字]` | 不带参数列出声线；带名字切换（如 `/声线 流萤`） | 管理员 |
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
| `/看空间 [QQ号] [条数]` | 看 QQ 空间说说 | 所有人 |
| `/视频 [平台] [关键词] [条数]` | 按标签刷 B 站视频池（别名 `/刷视频`） | 所有人 |
| `/看视频 <链接>` | 解析视频出字幕/内容摘要 | 所有人 |
| `/重置` | 重置个人档案 | 所有人 |

## 各插件说明

### 聊天拟人（核心特色）

**astrbot_plugin_human_sim** — 群内拟人行为的调度中枢
- 潜水插话、冷场开话头、冒泡发 QQ 表情、发言带表情包
- 拍一拍全链路：被拍必回、连拍回戳、回完人顺手拍回去
- 所有限频带概率 + 冷却 + 每分钟限额 + 活动时段频控，夜间降频
- 关键配置：`watch_groups`（要监视的群号列表，**必填**）、`interject_probability`（插话概率）、`group_idle_minutes`（多久算冷场）
- 依赖：表情包功能需要 `astrbot_plugin_sticker_pack` 的图库

**astrbot_plugin_humanizer** — 人味处理
- 清洗 Markdown 结构、AI 腔样板句、长句排比，防复读
- 无配置、无指令，装上即生效

**astrbot_plugin_group_window** — 群聊动态窗口
- 被动记录每个群的聊天（不唤醒不回复），机器人发言时注入上下文
- 关键配置：`group_ids`（要记录的群）、`max_messages`（窗口大小）

**astrbot_plugin_speaker_tag** — 发言人标签
- 群消息补 `[昵称/时间]:` 标签，每人档案按会话隔离存储并在回复时注入
- 指令：`/重置` 清个人档案

### 声音与多媒体

**astrbot_plugin_basic_tools** — 基础工具集 + 点歌 + 声线
- 指令见速查表；`/声线` 的名字来自 `cosyvoice_ref/profiles/` 下的目录
- 依赖：本地语音需要 CosyVoice2 TTS 服务（见 README「快速开始」）；无 GPU 时自动回退 edge-tts

**astrbot_plugin_sticker_pack** — 表情包引擎
- 回复里的 `:情绪词:` 按语义相似度从全库挑图，单独成条发送
- 支持收藏（fav）与查重：图片指纹只在收藏入库时使用
- 关键配置：`unlimited_groups`（不限流群）、`follow_up_probability`（补图概率）
- 图库结构：`map.txt` 索引 + `fav/` 收藏 + `pool/` 候选（参考仓库 `astrbot-win/.../stickers/`）

**astrbot_plugin_face_convert** — 文字表情转 QQ 表情
- 把 `[流泪]` `[doge]` 等转成真实 QQ 表情段，无配置

**astrbot_plugin_web_lookup** — 联网查证 + 视频
- 搜索候选 → 校验 → 抓正文 → 相似度挑最相关页面
- 也支持直接给链接抓正文、B 站视频字幕、按标签刷视频（片单池）
- 关键配置：`bili_tags`（B 站标签）、`bili_cookie`（扫码登录后自动存）

### QQ 空间

**astrbot_plugin_qzone_channel** — 空间独立信道
- 轮询自己说说下的新评论，用独立上下文回复，不混入群聊/私聊记忆
- 关键配置：`manual_cookie`（内置浏览器登录后复制进来，含 p_skey/uin 才有效）、`poll_interval_minutes`
- 指令：`/看空间 [QQ号] [条数]`，不带参数看自己

**astrbot_plugin_daily_qzone** — 每日自动发说说
- 内容优先取当日群聊总结，概率配表情包图
- 关键配置：`post_hours`（几点发）、`enabled`

### 模型与权限

**astrbot_plugin_provider_router** — 模型路由
- 群聊自动走本地 Ollama，好友私聊走云端，陌生人走本地；可按用户/群固定模型
- 关键配置：`group_provider_chain`（群聊模型链，前一个挂了切下一个）、`smart_route_enabled`（简单问题走小模型）

**astrbot_plugin_tool_gate** — 按群接入门控
- 按群决定模型/人设/工具集，支持一键预设与私聊接入（可视化页维护）
- 关键配置：`owner_ids`（管理员 QQ 号）、`owner_full_access`

**astrbot_plugin_local_tools** — 本机工具
- 给 AI 本机文件读写与命令执行权限，默认仅主人可用
- 非主人开启时退化为 workspace 沙箱且无命令执行

**astrbot_plugin_care_ping** — 主动私聊
- 冷场时主动找主人：≥180 分钟空闲、09:00–23:00、每天 ≤4 条，可带表情包
- 关键配置：`master_umo`（主人的 unified_msg_origin，**必填**）、`provider_id`

**astrbot_plugin_self_growth** — 自主成长
- AI 记经验教训到成长笔记，自动注入系统提示，支持按关键词遗忘
- 无配置，装上即生效

### 平台桥接

**astrbot_plugin_dafeiyu_pet** — 桌宠状态桥
- 把 QQ/微信消息与回复状态发给桌宠中转服务（默认 `127.0.0.1:23334`）
- 桌宠只做状态面板，不共享机器人人格与记忆
- 依赖：`relay/pet-relay.js` 在跑

**astrbot_plugin_qq_mcp** — QQ MCP 工具
- 发消息（拆条+打字节奏）/查历史/戳一戳封装成 MCP streamable-http，供 DSH（桌宠宿主）直接调用

**astrbot_plugin_wechat_oc_bridge** — 微信接入
- 把本机 io-wechat（PC 微信小号 RPA）的消息接入 AstrBot，回复走同一个小号
- 依赖：`services/io-wechat` 在跑；关键配置 `allow_chats`（允许哪些会话，**安全边界**）

## 常见问题

- **改了 config.json 没生效？** 在 AstrBot WebUI 里重新加载插件。旧版本会把不在 DEFAULTS 白名单里的键静默丢弃，请先更新到仓库最新代码。
- **`/声线` 切了没声音？** 确认 CosyVoice TTS 服务在跑（`tools/windows/start-cosyvoice-tts.bat`），下一条语音才生效。
- **表情包发不出来？** 图库路径没配对；`map.txt` 里的相对路径要能从插件工作目录解析到。
- **看空间报错？** `manual_cookie` 失效了，用内置浏览器重新登录 QQ 空间后把新 cookie 填回去。
