# 识图（OCR / 看图）按开关工作 · 修复规格（Spec）

- 状态：待审批
- 日期：2026-11-10
- 用户原话：「我说的是直 原台词说接 不需要 ollama 转义；ocr 也是走 ollama 识图模型，全局统一的，没开启就不干活」

## 1. 结论（有证据的现状）

### 1.1 推送消息已不再经过 ollama

[relay/config.json](file:///c:/Program%20Files/FlatFish/relay/config.json#L10) 已是 `"narrate": false`，
[pet-relay.js](file:///c:/Program%20Files/FlatFish/relay/pet-relay.js) 的 `deliverPet()` 对聊天消息只做
`splitSpeech()` 分段后原样投递，不调 9B 写旁白。20:55 用户已听感验收通过，本次不改这条链路。

### 1.2 真正把 6.3G 模型拉起来的是「字幕系统画面描述」

AstrBot 日志（`astrbot-win/AstrBot-4.29.3/data/logs/astrbot.log`）持续出现：

```
21:10:05 [face_convert] 字幕系统画面描述: 图中是一位红发蓝眼的动漫少女…
21:14:08 [face_convert] 字幕系统画面描述: 这是一张网络表情包图片…
21:17:11 [face_convert] 字幕系统画面描述: 图中是一个黄色圆形卡通形象…
21:18:14 [face_convert] 字幕系统画面描述: 图中是一个黄色圆形卡通笑脸表情…
```

链路：QQ 里每来一张图/一个表情 →
[asr_vision.py `_vl_image()`](file:///c:/Program%20Files/FlatFish/astrbot_plugins/output/astrbot_plugin_face_convert/asr_vision.py#L287)
→ `_subtitle_call("vision", …)` → audio 服务 23405 `/vision` →
[subsvc/engines.py](file:///c:/Program%20Files/FlatFish/services/subtitle/subsvc/engines.py#L145) 用
[`VISION_MODEL`](file:///c:/Program%20Files/FlatFish/services/subtitle/subsvc/config.py#L16)
（默认 **`qwen3.5:9b`**）直连 `127.0.0.1:11434/api/chat`。

而中枢的「识图模型」配置 [vision.json](file:///c:/Program%20Files/FlatFish/astrbot-win/AstrBot-4.29.3/data/local_tools/vision.json)
当前是 `{"provider_id": ""}`，即**没有开启**；按现有代码，空值会落到本地 ollama 兜底，
于是每张表情包都拉起 6.3 GB 的 9B，把 CosyVoice 挤到 `rtf 11~40`、单句 108s。

同样的「空值即本地 ollama」兜底还存在于：

| 位置 | 功能 | 空值时的行为 |
|---|---|---|
| [asr_vision.py `_vl_image()`](file:///c:/Program%20Files/FlatFish/astrbot_plugins/output/astrbot_plugin_face_convert/asr_vision.py#L287) | 引用的图/动图描述 | 先试 23405 `/vision`，再直连 ollama |
| [asr_vision.py `_vl_ollama()`](file:///c:/Program%20Files/FlatFish/astrbot_plugins/output/astrbot_plugin_face_convert/asr_vision.py#L210) | 视频抽帧描述 | 直连 ollama `qwen3.5:9b` |
| [avatar.py `_describe_avatar()`](file:///c:/Program%20Files/FlatFish/astrbot_plugins/function/astrbot_plugin_basic_tools/basic_core/avatar.py#L70) | 查头像 | 直连 ollama `qwen3.5:9b` |
| [collect.py `_label_for_group()`](file:///c:/Program%20Files/FlatFish/astrbot_plugins/output/astrbot_plugin_sticker_pack/collect.py#L156) | 表情收藏起名 | 自动探测本地 ollama 视觉模型 |

## 2. 目标

1. **一个全局开关**：中枢「识图模型」`vision.json` 的 `provider_id` 就是唯一的识图总开关。
2. `provider_id` 为空 = **识图功能全部不工作**：上述四处一律直接跳过，**不调 23405、不连 11434、不加载任何本地模型**。
3. `provider_id` 非空 = 上述四处统一走这个 provider（不再各自挑模型）。
4. 推送消息保持原文直读，不新增任何 ollama 调用。

## 3. 非目标

- 不改 CosyVoice / 声线 / 桌宠发声链路（上一轮已完成并验收）。
- 不改聊天路由：`provider_router` 的 `stranger_private_provider_id`、`human_sim` 的
  `LOCAL_DECIDE_PROVIDER`、`sticker_pack/llm.py` 的选图兜底仍按原逻辑，只在「当前会话 provider 不可用」时才用本地 9B
  ——这类是对话场景，不在本次「识图」范围（作为已知残余来源记录在第 6 节）。
- 不新增配置项、不改中枢接口字段。

## 4. 方案（逐文件）

### 4.1 `astrbot_plugins/output/astrbot_plugin_face_convert/asr_vision.py`

- `_vl_image(path, timeout)`：
  - 抽帧后先取 `pid = _central_vision_provider()`；`pid` 为空 → **直接 `return None`**（不调 `_subtitle_call`、不连 ollama）。
  - 保留原有中枢分支：`pid` 非空 → `_vl_central(...)`；失败 `return None`。
  - 删除第 310 行起的 `_subtitle_call("vision", …)` 与第 314–355 行的直连 ollama 兜底。
- `_vl_ollama(frames)`：
  - 改为只走中枢：`pid` 为空 → `return None`；否则 `_vl_central(...)` 并返回其文本。
  - 删除第 225–253 行的 base64 + `/api/chat` 直连段；函数名保留（调用点在别处，避免连带改动）。
- 更新两处 docstring，写明「识图模型未开启即不识别」。

### 4.2 `astrbot_plugins/function/astrbot_plugin_basic_tools/basic_core/avatar.py`

- `_describe_avatar(url, body)`：取到图片字节后先 `pid = _central_vision_provider()`；
  - `pid` 为空 → `return None`；
  - `pid` 非空 → 走现有 `prov.text_chat(..., image_urls=["base64://"+b64])` 分支；
  - 删除第 95–117 行直连 `OLLAMA_BASE/api/chat` 的兜底。

### 4.3 `astrbot_plugins/output/astrbot_plugin_sticker_pack/collect.py`

- `_label_for_group(text, image_path, umo)` 开头加统一开关判断：
  `pid = _central_vision_provider()`（同 4.1 的读法：`<data>/local_tools/vision.json`，带 mtime 缓存）。
  - `pid` 为空且 `self._vision_model_pref()` 也为空 → `return None`（这张不收，日志一行 `识图未开启，跳过`）。
  - `pid` 非空 → 用 AstrBot provider 看图：`prov.text_chat(prompt=prompt, image_urls=["base64://"+b64])`，替换现在的 `_vision_chat(vpath, …)` 直连。
  - `pid` 为空但插件 `vision_model` 显式配了 → 保持现有 `_vision_chat` 直连（显式开启才用本地模型）。
- `_vision_ready()` 的自动探测只在 `vision_model` 显式配置时执行，避免「空配置还去探测/加载」。

### 4.4 `services/hub/hubsrv/dashboard.html`

- 识图模型下拉的占位项：`跟随本地默认` → **`不识别（关闭）`**（两处：模板渲染 L2401、提示文案 L1255–1256）。
- hint 改为：「表情包识图 / 头像识别 / 视频抽帧统一走这个模型；不选＝全部关闭，不加载本地模型。」
- 保存回调 L3601 的提示：`已恢复各功能本地识图` → `识图已关闭，不再识别图片`。

### 4.5 `bots/config.json`

- `"narrate": true` → `false`，与 [relay/config.json](file:///c:/Program%20Files/FlatFish/relay/config.json#L10) 对齐。
  该文件是旧副本（当前进程只读 `relay/config.json`），改它是为了防止以后被误用又走回 9B 旁白。

## 5. 验收标准

1. `python -m py_compile` 通过：`asr_vision.py`、`avatar.py`、`collect.py`。
2. 保持 `vision.json` 为 `{"provider_id": ""}`，重启 astrbot 后观察 10 分钟（群里持续有表情包）：
   - `astrbot.log` 不再出现 `字幕系统画面描述` / `单图画面描述` / `本地视觉描述` / `头像识别:`;
   - `ollama ps` 只剩 `nomic-embed-text`，无 `qwen3.5:9b` 常驻；
   - `nvidia-smi` 已用显存 ≤ 4.5 G。
3. 在中枢「识图模型」选 `deepseek/deepseek-flash` → 发一张图 → 日志出现 `中枢识图:`，随后 `ollama ps` 仍无 9B。
4. 改回 `不识别（关闭）` → 再发一张图，无任何识图日志、无 ollama 拉载。
5. TTS 回归：`POST /manage {"name":"voice","action":"restart"}` 后注入一条长消息，
   `manage-voice.log` 的 `rtf` 保持 < 1，桌宠分段出声。

## 6. 风险与回滚

- **风险**：关闭识图后，机器人对图片/表情/头像**不再有任何画面描述**（这正是「没开启就不干活」的预期）。
- **残余**：陌生人私聊路由与 `human_sim` 决策仍可能在会话 provider 不可用时拉起 9B；
  如需彻底禁止，另开一轮把这两处也改成云端/`dsh`，本次不做。
- **回滚**：控制台把识图模型选回任意图片模型即可恢复；代码回滚为还原上述四个文件。

## 7. 审批

请确认第 4 节的五个改动点与第 5 节的验收标准后再执行。
