"""状态与能力描述接口。"""
from __future__ import annotations

from typing import Any

from infra import ok

from .state import config, harness, store


def handle_status(req) -> Any:  # noqa: ANN001
    data = harness.status()
    data["memory"] = store.stats()
    return ok(data)


def handle_capabilities(req) -> Any:  # noqa: ANN001
    return ok(
        {
            "service": "brain",
            "harness": "deepseek-headless",
            "transport": "brain -> model -> DSH / OpenAI 兼容端点",
            "credential_modes": ["account", "api_key"],
            "session_persistence": True,
            "memory": "sqlite (services/store)",
            "model_layer": "services/model",
            "tools": "disabled",
            "roles": [r.name for r in config.roles.values()],
            "platform_defaults": dict(config.platform_defaults),
            "endpoints": {
                "GET /roles": "角色与身份绑定",
                "POST /route": "身份解析",
                "POST /chat": "按角色对话",
                "POST /event": "统一信封入口，只回建议回复",
                "GET /memory": "读取长期事实与最近消息",
                "POST /memory": "写入长期事实",
                "DELETE /memory": "删除长期事实",
                "GET /status": "harness / 记忆概况",
            },
        }
    )
