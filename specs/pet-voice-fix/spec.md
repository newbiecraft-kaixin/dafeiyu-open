# 桌宠推送消息不发声 · 修复规格（Spec）

- 状态：待审批
- 日期：2026-11-10
- 现象：QQ/推送消息在桌宠上**有气泡字幕、没有声音**；碎碎念同样无声。用户原话：「桌宠为什么对推送的消息不发声了 之前不是可以」。

## 1. 结论（有证据的根因）

发声链路只有两条：

| 优先级 | 路径 | 依赖 | 现状 |
|---|---|---|---|
| 首选 | renderer → `POST http://127.0.0.1:23408/bot/tts` → `services/hub/hubsrv/_tts.py` → `8714` CosyVoice | 本机 GPU | 合成耗时在 4s ~ 103s 之间剧烈抖动 |
| 兜底 | renderer → `POST http://127.0.0.1:3080/dsh-pet-7340/voice/speak` → `dsh-pet/src/host/voice/speak.ts` 的 builtin **Edge** 合成 | 外网微软 Edge TTS | 6 个字要 57.7s，另一次 90.0s 直接超时 |

证据：

1. `~/.dsh/dsh-pet/logs.txt`
   - `2026-11-10T10:47:00.149Z [warn] voice speak: failed chars=6 voice=edge ms=90012 reason=The operation was aborted due to timeout`
   - `2026-11-10T10:47:57.814Z [info] voice speak: cache=miss chars=6 voice=edge ms=57661`
   - 日志里的 `voice=edge` 来自 [speak.ts](file:///%USERPROFILE%/.dsh/profiles/web/node_modules/dsh-pet/src/host/voice/speak.ts#L148)，`source = provider.id`，即**已经在走纯在线 Edge 兜底**。
2. `services/hub/data/manage-voice.log`：同机 CosyVoice 单句 `rtf 11.95 ~ 40.05`、`1/1 [01:43, 103.38s/it]`。
3. `nvidia-smi`：显存 **7671 / 8151 MiB 已打满**，占用者是三个进程：
   - `53448` `tools/cosyvoice_tts_server.py`
   - `42528` `astrbot-win/AstrBot-4.29.3/main.py`
   - `73552` `llama-server.exe`（Ollama `qwen3.5:9b`，relay 的旁白模型）
4. 空闲时直连 `8714/tts` 合成同一段新文本只要 **4.1s**（GPU 冲到 100%）。

**因果链**：8G 显存被 CosyVoice + AstrBot + 9B 旁白模型同时占满 → CosyVoice 拿不到连续显存，合成从 4s 劣化到 40~103s → 客户端 250s 超时/中枢 502 → 降级到在线 Edge → Edge 在本机要 57~90s 甚至超时 → 整句静音。与此同时宿主语音队列 `MAX_QUEUE=3` 且串行，一条慢请求把队列占死，后续推送消息全部 `queue full` / `dropped`，用户感知就是「对推送不发声」。

## 2. 目标

1. 推送消息**必定出声**，不再依赖外网 Edge TTS。
2. 推送正文**按句分段朗读**，不再被 60 字硬截断；分段各自可被缓存复用（前缀/重复段命中即 0s）。
3. 推送消息**不再生成 9B 旁白**，直接念原文，顺带把显存让给 CosyVoice。

## 3. 非目标

- 不改 CosyVoice 模型、不改 Seed-VC 训练。
- 不重训/删除 5 个克隆声线。
- 不改 AstrBot 插件侧协议字段。

## 4. 方案（逐文件）

### 4.1 `relay/pet-relay.js`

- 新增 `splitSpeech(raw, max = 60)`：按 `。！？；…!?;` 与 `，、,` 切句，单段超出 `max` 再硬切；返回非空段数组。`max` 必须 ≤ 客户端 `VOICE_SPEECH_MAX_CHARS`（60），否则 [injectOnce](file:///c:/Program%20Files/FlatFish/relay/pet-relay.js#L852) 的 `speechSlice` 又会截断。
- `deliverPet()`（[L1184](file:///c:/Program%20Files/FlatFish/relay/pet-relay.js#L1184)）：
  - 旁白条件收窄：`if (!message && CONFIG.narrate && !isChatPayload(payload))`，聊天消息永久不叫 9B。
  - 聊天消息改为分段投递：`const segs = splitSpeech(rawDetail)`；对每段依次 `await injectToPet({... message: seg })`，段间 `sleep(600)` 保证顺序，任一段失败只影响该段（不整条放弃）。
  - 非聊天消息（状态/事件）保持单条投递不变。
- `lastStatus` 记录 `segments` 数量与最后一段文本，便于 `/last` 排查。

### 4.2 `relay/config.json`

- `"narrate": false`（关掉 9B 旁白，减少 llama-server 调用）。

### 4.3 `pet/dsh-pet/patches/runtime/electron-helper/shared-core.js`

同步修改运行副本 `%USERPROFILE%\.dsh\profiles\web\node_modules\dsh-pet\runtime\electron-helper\shared-core.js`（两份内容一致）。

- `fetchSpeech()`（[L4444](file:///c:/Program%20Files/FlatFish/pet/dsh-pet/patches/runtime/electron-helper/shared-core.js#L4444)）：
  - 删除「克隆失败 → 在线 Edge」的兜底分支；
  - 克隆声线改为 **3 次重试**，间隔 `0 / 1200ms / 3000ms`，单次超时 `CLONE_SPEAK_TIMEOUT_MS` 由 `250e3` 降为 `120e3`；
  - 全部失败返回 `null`。
- `speakAuto()`（[L4726](file:///c:/Program%20Files/FlatFish/pet/dsh-pet/patches/runtime/electron-helper/shared-core.js#L4726)）与 `playManual()`：`null` 时直接走 `emitSilentCaption(text, seq)`（只字幕、不出声），不再产生 90s 阻塞。
- 若 `voiceName` 本来就是 Edge 白名单音色（用户显式选择），仍走宿主 `/voice/speak`，但客户端超时 `SPEAK_TIMEOUT_MS` 由 `95e3` 降为 `20e3`。

### 4.4 运行期显存回收（操作，非代码）

- 关掉旁白后执行 `ollama stop qwen3.5:9b`，让 llama-server 释放约 2~4G 显存。
- 验收 `nvidia-smi` 已用显存 ≤ 4.5G，CosyVoice 单句回到 5s 内。

## 5. 验收标准

1. `POST http://127.0.0.1:23334/state` 注入一条 150 字 QQ 消息：桌宠依次出现多条气泡，**每条都有声音**，段数与 `splitSpeech` 结果一致。
2. `logs.txt` 不再出现 `voice=edge`；hub 合成日志 `cache=miss ... ms=` 小于 15000。
3. 断网状态下（Edge 不可用）重复第 1 步，仍然出声。
4. `GET http://127.0.0.1:23334/last` 返回 `segments > 1` 且 `narrate` 未触发。
5. `nvidia-smi` 已用显存 ≤ 4.5G。

## 6. 风险与回滚

- **风险**：完全去掉 Edge 后，若 hub/CosyVoice 整体挂掉，只能出字幕不出声。缓解：保留「配置成 Edge 音色就走宿主」的能力，一键切回。
- **回滚**：`relay/config.json` 恢复 `narrate: true`；两份 `shared-core.js` 用同目录已有的 `.bak` 还原。

## 7. 审批

请确认第 4 节的四个改动点与第 5 节验收标准后再执行。
