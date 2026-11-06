"""model（模型调度）：身份 → provider / model / key 的独立一层。

端口：23406（infra.port_of("model")，可用 MODEL_PORT 覆盖）

接口
  POST /complete  {role, profile?, model?, provider?, system?, session_id?, scene?, messages|text}
  POST /route     {role, provider?, model?} 只看路由，不调模型
  GET  /providers 可用 provider 与角色绑定（不含密钥）
  GET  /status    调度概况与最近错误

本地 / 远程 / DSH harness 都在这一层决定并执行；brain 只负责身份、人设、会话、记忆。
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from app.entry import main  # noqa: E402

if __name__ == "__main__":
    main()
