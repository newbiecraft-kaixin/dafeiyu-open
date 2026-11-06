# 架构说明

## 消息链路

### QQ

```
QQ ↔ NapCat（OneBot11 反向 WS）↔ AstrBot ↔ 插件层
                                      ↕
                                   brain / model / voice / video
```

- `human_sim`：拟人化核心。决定要不要回、回什么语气、延迟多久；负责群聊冷却、引用解析、表情回应。
- `group_window`：按群维护近期上下文窗口。
- `web_lookup`：`/视频`、`/看视频`、搜索聚合；B 站刷片，抖音/快手/西瓜按平台搜索。
- `provider_router`：按用户/群指定模型与人设。
- 其余插件：点赞、禁言、表情包、贴纸、群公告、空间动态等。

### 微信

```
微信桌面版 ↔ services/io-wechat ↔ AstrBot（wechat 桥插件）↔ 插件层
```

`io-wechat` 以 HTTP 暴露 `/health` 与 `/send`，内部用 `wechatauto` 的 UIA 驱动操作桌面微信：

- **窗口让路**：发送前检查前台进程。若用户正在操作模拟器/全屏应用（`WX_DND_PROCS`），最多等待 `WX_GUARD_MAX_WAIT`（默认 90 秒）让其让出前台，超时则本次发送放弃，绝不把微信顶到最前。
- **空闲等待**：用户停止键鼠输入 `WX_GUARD_IDLE`（默认 3 秒）后才发送；微信已经在前台则跳过等待与激活。
- **会话复用**：发送目标使用联系人显示名，与聊天窗口标题比对；目标会话已经打开时直接粘贴+回车，不再搜索、点击会话。
- **发送后回位**：取消置顶、恢复原 Z 序，并尽量把焦点还给原前台窗口。
- **窗口自愈**：微信重启导致句柄失效时重新绑定主窗口与渲染区域（`heal_binding`）。

## 服务端口

端口定义在各目录 `service.json`：

| 服务 | 端口 | 作用 |
|---|---|---|
| io-wechat | 23392 | 微信桥 HTTP |
| brain | 23402 | 大脑/编排 |
| model | 23406 | 模型路由 |
| hub | 23408 | 控制台 |
| audio | 23366 | 音频处理 |
| subtitle | 23405 | 字幕 |
| store | 23407 | 存储 |
| video | 23368 | 视频解析/信息流 |
| stickers | 8712 | 表情包 |
| voice | 8714 | CosyVoice TTS |
| dsh | 3080 | 桌宠渲染面 |
| pet | 23334 | 桌宠继电 |
| astrbot | 6185 | AstrBot 面板 |
| napcat | 3000 | NapCat HTTP |

## 关键设计

- **配置即服务**：每个服务目录一个 `service.json`（端口、启动命令、依赖），`hub` 据此统一拉起与巡检。
- **模型解耦**：`services/model/providers.json` 用 `type` 区分 openai / ollama / harness，密钥只从环境变量读取。
- **语音降级**：CosyVoice 不可用时回退 edge-tts，保证不因 TTS 故障阻塞回复。
- **记忆分层**：短期用 `group_window`，长期记忆交给第三方 `livingmemory`（不随仓库分发），二者都不进 Git。
- **桌宠解耦**：桌宠通过 `relay` 与 `brain` 通信，渲染面与业务逻辑分离，可单独替换 Electron/Three.js 前端。

## 开源边界

随仓库分发：自研插件、微信桥、模型/语音/视频/控制台服务、桌宠前端、部署脚本与文档。

不随仓库分发（需自行安装）：AstrBot 主程序、NapCat、livingmemory、clawd-on-desk 桌宠壳、CosyVoice 代码与权重、Ollama 模型、各类运行数据与日志。
