# 识图按开关工作 · 任务清单

- [x] T1 `asr_vision.py`：`_vl_image` / `_vl_ollama` 去掉 23405 与本地 ollama 兜底，识图未开启直接跳过
- [x] T2 `avatar.py`：`_describe_avatar` 去掉本地 ollama 兜底，识图未开启直接跳过
- [x] T3 `collect.py`：表情命名统一走「中枢识图模型」，空配置且插件未显式配 `vision_model` 时不收图
- [x] T4 `dashboard.html` + `bots/config.json`：下拉占位文案改为「不识别（关闭）」，`narrate` 对齐为 false
- [x] T5 重启 astrbot，按 spec 第 5 节验收（日志无画面描述、`ollama ps` 无 9B、TTS rtf < 1）

## 验收记录（2026-11-10 21:24~21:29）

- `py_compile` 三个文件均 0 退出；已同步到 `astrbot-win/AstrBot-4.29.3/data/plugins/` 并再次编译通过。
- 重启 astrbot（PID 36968，6185 于 21:24:01 监听），插件无报错。
- 21:24 之后日志只剩 `收信翻译: [识图失败，请在 中枢→模型→识图模型 设置识图模型]`（inbound.py 既有占位），不再出现 `字幕系统画面描述` / `本地视觉` / `头像识别`。
- `ollama ps` 只剩 `nomic-embed-text`（323 MB），无 `qwen3.5:9b`；`nvidia-smi` 4135 / 8151 MiB。
- TTS 回归：长句 15.28s，`rtf 0.806`，wall 13.58s，缓存落盘 733 KB。
- 中枢识图 API 已可切换（`POST /bot/vision` 设 `deepseek/deepseek-flash` 后 vision.json 立即更新），随后已恢复为
  `{"provider_id": ""}`；因观察窗口内群里没有新图，云端识别路径未拿到实图日志，其余链路与改动前一致。


## 验证命令

```powershell
# 1. 语法
python -m py_compile "c:\Program Files\FlatFish\astrbot_plugins\output\astrbot_plugin_face_convert\asr_vision.py"
python -m py_compile "c:\Program Files\FlatFish\astrbot_plugins\function\astrbot_plugin_basic_tools\basic_core\avatar.py"
python -m py_compile "c:\Program Files\FlatFish\astrbot_plugins\output\astrbot_plugin_sticker_pack\collect.py"

# 2. 重启 astrbot
Invoke-WebRequest -UseBasicParsing -Method POST http://127.0.0.1:23408/manage `
  -ContentType "application/json" -Body '{"name":"astrbot","action":"restart"}'

# 3. 看是否还在识图 / 拉模型
Select-String -Path "C:\Program Files\FlatFish\astrbot-win\AstrBot-4.29.3\data\logs\astrbot.log" `
  -Pattern "画面描述|本地视觉|头像识别" | Select-Object -Last 10
ollama ps
```
