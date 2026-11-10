# astrbot_plugin_dsh_bridge

把 DSH（DeepSeek Harness）的 `dsh_bridge` provider 注册在**插件层**，AstrBot core 保持上游原样，
升级时不再需要重新打补丁。

## 为什么不改 core

以前的做法是往 `astrbot/core/provider/manager.py` 的 `dynamic_import_provider()` 里插一个
`case "dsh_bridge"`，并把 `dsh_bridge_source.py` 放进 `astrbot/core/provider/sources/`。
AstrBot 升级会覆盖 `astrbot/core`，这两处改动随之丢失。

实际上 provider 注册依赖的是 `astrbot.core.provider.register.provider_cls_map`：任何模块只要
执行到 `@register_provider_adapter("dsh_bridge", ...)` 就会自动注册。而
`astrbot/core/core_lifecycle.py` 里插件加载（L260 `plugin_manager.reload()`）早于
provider 实例化（L264 `provider_manager.initialize()`），插件在导入时注册完全来得及。

因此本插件在 `main.py` **模块导入阶段**完成两件事：

1. 用 `importlib` 加载同目录的 `dsh_bridge_source.py`，触发装饰器注册；
2. 给 `ProviderManager.dynamic_import_provider` 包一层：
   ```python
   if type in provider_cls_map:
       return
   return original(self, type)
   ```

这样 `dsh_bridge` 不会再抛 `ImportError`，其它 provider 仍走原始 `match` 分支，行为不变。
包装带标记位，重复 reload 不会叠加；插件 `terminate()` 时恢复原函数并撤销注册。

## 依赖的 AstrBot 版本

- 实机验证：AstrBot **4.29.3**（`C:\Program Files\FlatFish\astrbot-win\AstrBot-4.29.3`）。
- 依赖的上游接口：`astrbot.core.provider.register.provider_cls_map`、
  `astrbot.core.provider.manager.ProviderManager.dynamic_import_provider`、
  `astrbot.api.provider.Provider`。升级后若这些接口改名，需要同步调整。

## Provider 配置示例

`data/cmd_config.json` 的 `provider_sources`：

```json
{
  "id": "dsh",
  "provider": "dsh",
  "type": "dsh_bridge",
  "provider_type": "chat_completion",
  "api_base": "http://127.0.0.1:18081",
  "key": ["dsh"],
  "token": "<DSH_BRIDGE_TOKEN>",
  "timeout": 390,
  "custom_headers": {},
  "enable": true
}
```

`provider`：

```json
{
  "id": "dsh/dsh",
  "enable": true,
  "provider_source_id": "dsh",
  "model": "dsh",
  "modalities": ["text", "tool_use"],
  "custom_extra_body": {},
  "max_context_tokens": 0
}
```

`token` 留空时 Provider 回退读取 `~/.dsh/qq-bridge-token.txt`。真实 token 不要写进仓库。

## 安装

把整个目录复制到 `<AstrBotHome>\data\plugins\astrbot_plugin_dsh_bridge\`，重启 AstrBot。
启动日志应出现 `Loading model dsh_bridge(dsh) ...` 与 `[dsh_bridge] provider adapter 已由插件注册`。
