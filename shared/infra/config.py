"""全局端口与常量。

所有端口都可用环境变量覆盖，规则是服务名大写、横杠换下划线、加 _PORT：
    io-wechat  -> IO_WECHAT_PORT  (默认 23392)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

# 服务名 -> 默认端口
PORTS: Dict[str, int] = {
    # 输入输出层（平台相关，独立进程）；QQ 由 NapCat 直连 AstrBot，不再有 io-qq
    "io-wechat": 23392,
    # 现有微信 RPA 服务（io-wechat 的下游/前置，暂时并存）
    "svc-wechat": 23336,
    # 桌宠中继（桌宠窗口本身 23335）
    "pet": 23334,
    # 静态文档 / 监控页
    "docs": 23390,
    "brain": 23402,
    # 能力层：音频字幕系统（ASR / 画面描述）
    "subtitle": 23405,
    # 模型调度层：身份 → provider/model/key（本地或远程）
    "model": 23406,
    # 统一记忆层：SQLite
    "store": 23407,
    # 能力注册中心 / MCP 工具入口
    "hub": 23408,
    # 知识库（RAG）：资料入库与检索
    "rag": 23409,
    # 能力层：语音合成（可选，本地 CosyVoice2 或第三方 OpenAI 兼容）
    "voice": 8714,
    # 能力层：音频 / 视频处理
    "audio": 23366,
    "video": 23368,
    # 贴纸表情库
    "stickers": 8712,
    # 桌宠宿主（Electron helper）
    "dsh": 3080,
    # AstrBot 宿主
    "astrbot": 6185,
    # NapCat（OneBot WS）
    "napcat": 6199,
}

HOST = "127.0.0.1"

# HTTP 客户端默认超时（秒）
HTTP_TIMEOUT = 10.0
# 长轮询默认等待（毫秒）
LONGPOLL_WAIT_MS = 25000

# 信封与去重
MAX_TEXT_CHARS = 20000
DEDUPE_TTL_S = 180.0
DEDUPE_WINDOW_S = 120


def port_of(service: str, default: Optional[int] = None) -> int:
    """取某服务端口：环境变量优先，其次 PORTS，最后 default。"""
    env_key = service.upper().replace("-", "_") + "_PORT"
    raw = os.environ.get(env_key)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    if service in PORTS:
        return PORTS[service]
    if default is not None:
        return default
    raise KeyError(f"未知服务端口: {service}")


def base_url(service: str) -> str:
    return f"http://{HOST}:{port_of(service)}"


# ---- 外部依赖统一配置（沿用 OpenAI 的 base_url + api_key + model 三段式）----
# 优先级：环境变量 > config/endpoints.json > 本机默认端口
# 默认值就是本机微服务，开箱即用；要换成第三方或远端，改文件或环境变量都行：
#   config/endpoints.json:
#     {"voice": {"base_url": "https://ark.cn-beijing.volces.com/api/v3",
#                "api_key": "xxx", "model": "doubao-tts", "voice": "zh_female_..."},
#      "rag":   {"base_url": "http://10.0.0.8:23409", "api_key": "xxx"},
#      "model": {"base_url": "https://api.openai.com/v1", "api_key": "sk-...", "model": "gpt-4o-mini"}}
#   或 TTS_BASE_URL / RAG_BASE_URL / LLM_BASE_URL / STICKER_BASE_URL 环境变量。
CONFIG_FILE = Path(__file__).resolve().parents[2] / "config" / "endpoints.json"


def _overrides() -> Dict[str, dict]:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pick(service: str, field: str) -> str:
    env_name = f"{service.upper().replace('-', '_')}_{field.upper()}"
    raw = os.environ.get(env_name)
    if raw and raw.strip():
        return raw.strip()
    entry = _overrides().get(service) or {}
    value = entry.get(field)
    return str(value).strip() if value is not None else ""


def service_url(service: str) -> str:
    """服务地址：环境变量/配置文件优先（可指向远端或第三方），否则本机。"""
    return _pick(service, "base_url").rstrip("/") or base_url(service)


def service_key(service: str) -> str:
    """服务密钥：{SERVICE}_API_KEY 或配置文件 api_key，空表示不需要鉴权。"""
    return _pick(service, "api_key")


def service_model(service: str, default: str = "") -> str:
    """服务模型：{SERVICE}_MODEL 或配置文件 model，用于 OpenAI 兼容的 model 字段。"""
    return _pick(service, "model") or default
