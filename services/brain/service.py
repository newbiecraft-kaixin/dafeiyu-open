"""brain（主脑）：身份 → 角色 → 人设/会话/记忆 → model 层执行。

端口：23402（infra.port_of("brain")，可用 BRAIN_PORT 覆盖）

接口
  GET  /roles         角色、身份绑定、平台默认角色（不含密钥）
  POST /route         {platform, user_id, name, chat_id} 只做身份解析
  POST /chat          {actor{platform,user_id,name}, chat{id,type,name}, text}
  POST /event         {统一信封} 只返回建议回复，不直接发平台
  GET  /memory?scope=&q=&limit=   统一记忆：长期事实 + 最近消息
  POST /memory        {scope,key,value,importance} 写一条长期事实
  DELETE /memory?scope=&key=      删除长期事实
  GET  /capabilities  能力与 harness 状态
  GET  /status        DSH / 会话 / profile / 记忆概况

角色配置在 roles.json：每个角色一套 DSH profile、凭据（账号或 API key 环境变量）与人设。
模型调度（provider/model/key）在 services/model；记忆在 services/store。
出站发送不在这里做：QQ 由 AstrBot → NapCat 直接发，微信由 AstrBot 插件走 io-wechat。
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parents[1] / "store"))

from app.entry import main  # noqa: E402

if __name__ == "__main__":
    main()
