"""model（模型调度层）：身份 → provider / model / key，本地与远程统一入口。

职责边界：
  - 只看「哪个角色用哪个 provider、哪个模型、哪把 key」以及怎么调用；
  - 不关心人设、会话、记忆（那属于 brain / store）；
  - 支持 OpenAI 兼容端点（DeepSeek、Ollama、任意自建）与 DSH headless harness。

配置在 providers.json；密钥只写环境变量名，明文从进程环境或 .env 读取，接口不回显。
"""
from __future__ import annotations

from .complete import complete
from .constants import CONFIG_PATH
from .dotenv import read_dotenv, resolve_key
from .errors import ModelError
from .route import Route
from .router import Router

__all__ = [
    "CONFIG_PATH",
    "ModelError",
    "Route",
    "Router",
    "complete",
    "read_dotenv",
    "resolve_key",
]
