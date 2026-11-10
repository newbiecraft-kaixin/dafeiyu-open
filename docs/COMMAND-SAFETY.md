# 指令与转发的安全边界（排查记录）

- 日期：2026-11-10
- 结论：直接发的 `/指令` 不会误答、不会入上下文；**合并转发里夹带的 `/指令` 不会被执行**，
  但会被 human_sim 展开后交给模型，存在「被当成聊天内容回应」和「随 prompt 进私聊记忆」的残余风险。

## 1. 直接发 `/指令`

| 层 | 行为 | 位置 |
|---|---|---|
| 桌宠中转 | `/`、`／` 开头的内容按指令处理，不气泡、不发声（`forceSubtitle` 也不放行） | `relay/pet-relay.js` 的 `isTrivialText` / 投递前命令判定 |
| human_sim | `_is_slash_message` 直接看消息链 Plain 原文，不看被插件改写后的 `message_str`；命中即 `stop_event`，不进模型 | `astrbot_plugins/function/astrbot_plugin_human_sim/human_sim_core/_record.py` |
| AstrBot | 命令处理器先消费事件，未命中命令的 `/xxx` 也不会进 LLM | core 命令分发 |

结果：不触发额外模型调用，不写入 `_history`，也不进长期记忆。

## 2. 合并转发里夹带 `/指令`

- **不会执行**：适配器只把 `Forward` 当组件塞进消息链，**不写入 `message_str`**
  （`astrbot-win/AstrBot-4.29.3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py`）。
  命令解析看到的是空串，`/reset`、`/清空` 这类不会命中。
- **会被模型看到**：human_sim 的 `_inject_forward_records`
  （`astrbot_plugins/function/astrbot_plugin_human_sim/human_sim_core/probe.py`）把展开后的正文
  原样拼进 `req.prompt`，没有过滤 `/` 行。提示词标明了「用户转发了一份聊天记录」，
  正常不会照做，但模型可能对里面的指令文本作出内容层面的回应。
- **入库风险**：livingmemory 私聊路径存储的就是 `req.prompt`
  （`astrbot_plugins/function/astrbot_plugin_livingmemory/core/event_handler_modules/memory_recall.py`）。
  两个插件都挂 `on_llm_request`，执行顺序不固定——若 human_sim 先注入，转发正文会随
  `req.prompt` 进入会话历史/记忆库；未注入时 `req.prompt` 为空，插件直接跳过。
  群聊被动捕获只记 `[转发消息]` 占位，不会展开正文。

## 3. 建议的加固（尚未实施）

在 `_inject_forward_records` 展开后加一道过滤：

```python
lines = [ln for ln in body.splitlines()
         if not ln.strip().startswith(("/", "／", "#", "！", "!"))]
if not lines:
    return  # 纯指令转发，不注入
body = "\n".join(lines)
```

这样转发里的指令既不会喂给模型，也不会随 prompt 落进记忆。
