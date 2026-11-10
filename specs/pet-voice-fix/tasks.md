# 桌宠推送消息不发声 · 任务清单

依赖顺序自上而下，前一项完成并通过自检再进入下一项。

## T1 relay 分段朗读（高）
- [x] `relay/pet-relay.js` 新增 `splitSpeech(raw, max = 60)`，按句切分、超长硬切。
- [x] `deliverPet()` 聊天消息改为逐段 `injectToPet`，段间 `sleep(600)`。
- [x] `lastStatus` 增加 `segments` 字段。
- 验收：`node -e` 单测 `splitSpeech` 对 150 字文本返回 ≥3 段且每段 ≤60 字。

## T2 推送不再走 9B 旁白（高）
- [x] `deliverPet()` 旁白条件加 `!isChatPayload(payload)`。
- [x] `relay/config.json` 改 `"narrate": false`。
- 验收：注入 QQ 消息后 `manage-pet.log` 无 narrate 调用、无 ollama 请求。

## T3 去掉在线 Edge 兜底（高）
- [x] 改 `pet/dsh-pet/patches/runtime/electron-helper/shared-core.js` 的 `fetchSpeech()`：3 次重试、单次 120s、全失败返回 `null`。
- [x] `speakAuto()` / `playManual()` 处理 `null` → 仅字幕。
- [x] 同文件同步到 `%USERPROFILE%\.dsh\profiles\web\node_modules\dsh-pet\runtime\electron-helper\shared-core.js`。
- 验收：`grep -n "FALLBACK_EDGE_VOICE"` 两份文件都不再被兜底分支引用。

## T4 释放显存（中）
- [x] `ollama stop qwen3.5:9b`。
- [x] `nvidia-smi` 确认已用 ≤ 4.5G（实测 2922 / 8151 MiB）。
- 验收：直连 `POST http://127.0.0.1:8714/tts` 新文本 RTF 从 12~40 降到 0.68~0.72。

## T4b 修正 surface 误判（高，排查中新增）
- [x] relay 不再把 `display: "both"` 原样当 `surface` 发送，保留配置里的 `desktop`。
- 原因：宿主只认 `desktop` / `web`，收到 `both` 时桌面端不算语音目标，只出气泡不出声。

## T5 端到端验证（高）
- [x] 重启 relay 与桌宠 helper。
- [x] 注入 150 字 QQ 消息：`/last` 返回 `segments: 3`，`tts_cache` 新增 3 个 wav（19:26:06 / 19:26:25 / 19:26:41）。
- [x] 查 `~/.dsh/dsh-pet/logs.txt`：重启后无新的 `voice=edge`、无 `ms=9xxxx`。
- [x] 显存回收后 CosyVoice 连续合成正常（rtf 0.68~0.72）。
