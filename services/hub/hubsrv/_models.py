"""可用模型：provider 清单、提供商来源、模型增删改。"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from ._admins import CMD_CONFIG, CONFIG_PATH, _LOCK, _read_json, _restart_astrbot, _write_json


# ---------- 可用模型 ----------
def providers() -> List[Dict[str, str]]:
    """可用的对话模型：过滤掉 TTS / embedding。"""
    data = _read_json(CMD_CONFIG)
    rows: List[Dict[str, str]] = []
    for item in data.get("provider") or []:
        if not isinstance(item, dict) or not item.get("enable", True):
            continue
        kind = str(item.get("provider_type") or "").lower()
        modalities = item.get("modalities") or []
        if kind in {"text_to_speech", "speech_to_text"} or "text" not in modalities:
            continue
        pid = str(item.get("id") or "").strip()
        if pid:
            extra = item.get("custom_extra_body") or {}
            rows.append({
                "id": pid,
                "model": str(item.get("model") or pid),
                "source": str(item.get("provider_source_id") or ""),
                "temperature": extra.get("temperature"),
                "top_p": extra.get("top_p"),
                "max_context_tokens": item.get("max_context_tokens") or 0,
            })
    return rows


# ---------- 识图模型（全局视觉统一入口） ----------
DATA_DIR = os.path.dirname(CMD_CONFIG)
VISION_PATH = os.path.join(DATA_DIR, "local_tools", "vision.json")


def _image_providers() -> List[Dict[str, str]]:
    """能看图的模型：声明了 image 模态的 provider。"""
    data = _read_json(CMD_CONFIG)
    rows: List[Dict[str, str]] = []
    for item in data.get("provider") or []:
        if not isinstance(item, dict) or not item.get("enable", True):
            continue
        if "image" not in (item.get("modalities") or []):
            continue
        pid = str(item.get("id") or "").strip()
        if pid:
            rows.append({
                "id": pid,
                "model": str(item.get("model") or pid),
                "source": str(item.get("provider_source_id") or ""),
            })
    return rows


def vision_setting() -> Dict[str, Any]:
    """识图模型当前选择 + 可选项；选中的模型被删了就当没配置。"""
    data = _read_json(VISION_PATH)
    pid = str(data.get("provider_id") or "").strip()
    options = _image_providers()
    if pid and pid not in {row["id"] for row in options}:
        pid = ""
    return {"provider_id": pid, "options": options}


def save_vision(provider_id: Any) -> Dict[str, Any]:
    pid = str(provider_id or "").strip()
    if pid and pid not in {row["id"] for row in _image_providers()}:
        raise ValueError("这个模型不支持图片输入：" + pid)
    os.makedirs(os.path.dirname(VISION_PATH), exist_ok=True)
    with _LOCK:
        _write_json(VISION_PATH, {"provider_id": pid})
    return vision_setting()


# ---------- 可用模型来源 / 角色 ----------
def _mask_key(value: Any) -> str:
    """把密钥压成可展示的掩码，避免前端只能看到「有没有」，改完看不出换没换。"""
    if isinstance(value, (list, tuple)):
        raw = next((item for item in value if str(item or "").strip()), "")
    else:
        raw = value if value is not None else ""
    text = str(raw).strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "…" + text[-4:]


def provider_sources() -> List[Dict[str, str]]:
    data = _read_json(CMD_CONFIG)
    rows: List[Dict[str, str]] = []
    for item in data.get("provider_sources") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        masked = _mask_key(item.get("key"))
        # dsh_bridge 用 token 鉴权，key 只是占位（["dsh"]），必须单独暴露给前端
        token = _mask_key(item.get("token"))
        rows.append({
            "id": str(item.get("id")),
            "type": str(item.get("type") or ""),
            "api_base": str(item.get("api_base") or ""),
            "has_key": bool(masked),
            "key_masked": masked,
            "has_token": bool(token),
            "token_masked": token,
            "timeout": item.get("timeout"),
        })
    return rows


# ---------- 模型增删 ----------
def _opt_num(raw: Any, label: str) -> tuple[bool, Any]:
    """None = 没传这个字段；空串 = 清空；其它按数字校验。"""
    if raw is None:
        return False, None
    text = str(raw).strip()
    if not text:
        return True, None
    try:
        return True, float(text)
    except ValueError as error:
        raise ValueError(f"{label}必须是数字") from error


def _apply_source(src: Dict[str, Any], api_base: str, api_key: str, token: str,
                  has_timeout: bool, timeout_value: Any) -> None:
    """写提供商级参数：dsh_bridge 的鉴权是 token，key 只是占位（["dsh"]）。"""
    if api_base:
        src["api_base"] = api_base
    if api_key:
        src["key"] = [api_key] if isinstance(src.get("key"), list) else api_key
    if token and str(src.get("type") or "") == "dsh_bridge":
        src["token"] = token
    if has_timeout and timeout_value is not None:
        src["timeout"] = int(timeout_value) if float(timeout_value).is_integer() else timeout_value


def create_model(
    source_id: str,
    model: str,
    provider_id: str = "",
    source_new: str = "",
    api_base: str = "",
    api_key: str = "",
    temperature: Any = None,
    top_p: Any = None,
    max_context_tokens: Any = None,
    token: str = "",
    timeout: Any = None,
) -> Dict[str, Any]:
    source_id, model = str(source_id or "").strip(), str(model or "").strip()
    source_new = str(source_new or "").strip()
    api_base, api_key = str(api_base or "").strip(), str(api_key or "").strip()
    token = str(token or "").strip()
    has_timeout, timeout_value = _opt_num(timeout, "超时")
    if not model:
        raise ValueError("模型名不能为空")
    has_temp, temp = _opt_num(temperature, "温度")
    has_top, top_p_value = _opt_num(top_p, "Top P")
    try:
        max_ctx = int(str(max_context_tokens or "0").strip() or 0)
    except ValueError as error:
        raise ValueError("最大上下文必须是整数") from error
    with _LOCK:
        data = _read_json(CMD_CONFIG)
        sources = {
            str(s.get("id")): s
            for s in data.get("provider_sources") or []
            if isinstance(s, dict) and s.get("id")
        }
        if source_id == "__new__" or source_id not in sources:
            if not source_new:
                raise ValueError("新建提供商要填提供商 ID")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", source_new):
                raise ValueError("提供商 ID 只能用字母、数字、- 和 _")
            if source_new in sources:
                raise ValueError("提供商 ID 已存在: " + source_new)
            if not api_base:
                raise ValueError("新建提供商要填 API 地址")
            source_id = source_new
            data.setdefault("provider_sources", []).append({
                "provider": source_new.replace("-", "_"),
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
                "key": [api_key] if api_key else [],
                "api_base": api_base,
                "timeout": 120,
                "proxy": "",
                "custom_headers": {},
                "id": source_new,
                "enable": True,
            })
        else:
            src = sources[source_id]
            _apply_source(src, api_base, api_key, token, has_timeout, timeout_value)
        pid = str(provider_id or "").strip() or f"{source_id}/{model}"
        if any(str(p.get("id")) == pid for p in data.get("provider") or [] if isinstance(p, dict)):
            raise ValueError("模型 ID 已存在: " + pid)
        template = next(
            (
                p
                for p in data.get("provider") or []
                if isinstance(p, dict) and str(p.get("provider_source_id")) == source_id
            ),
            None,
        )
        entry = dict(template) if template else {
            "provider_type": "chat_completion",
            "modalities": ["text"],
            "enable": True,
        }
        entry.update({
            "id": pid,
            "model": model,
            "provider_source_id": source_id,
            "enable": True,
        })
        if "text" not in (entry.get("modalities") or []):
            entry["modalities"] = list(entry.get("modalities") or []) + ["text"]
        if has_temp or has_top:
            extra = dict(entry.get("custom_extra_body") or {})
            if has_temp and temp is not None:
                extra["temperature"] = temp
            if has_top and top_p_value is not None:
                extra["top_p"] = top_p_value
            entry["custom_extra_body"] = extra
        if max_ctx:
            entry["max_context_tokens"] = max_ctx
        data.setdefault("provider", []).append(entry)
        _write_json(CMD_CONFIG, data)
    _restart_astrbot()
    return {"ok": True, "id": pid, "restarted": True, "provider_sources": provider_sources()}


def delete_model(provider_id: str) -> Dict[str, Any]:
    pid = str(provider_id or "").strip()
    if not pid:
        raise ValueError("模型 ID 不能为空")
    cfg = _read_json(CONFIG_PATH)
    if pid in json.dumps(cfg, ensure_ascii=False):
        raise ValueError("该模型仍被群/用户绑定，先解绑再删")
    with _LOCK:
        data = _read_json(CMD_CONFIG)
        rows = data.get("provider") or []
        keep = [p for p in rows if not (isinstance(p, dict) and str(p.get("id")) == pid)]
        if len(keep) == len(rows):
            raise ValueError("模型不存在")
        if not [p for p in keep if isinstance(p, dict) and p.get("enable", True)]:
            raise ValueError("至少保留一个可用模型")
        data["provider"] = keep
        _write_json(CMD_CONFIG, data)
    _restart_astrbot()
    return {"ok": True, "id": pid, "restarted": True}


def update_model(
    provider_id: str,
    model: str = "",
    source_id: str = "",
    api_base: str = "",
    api_key: str = "",
    temperature: Any = None,
    top_p: Any = None,
    max_context_tokens: Any = None,
    token: str = "",
    timeout: Any = None,
) -> Dict[str, Any]:
    pid = str(provider_id or "").strip()
    model = str(model or "").strip()
    source_id, api_base = str(source_id or "").strip(), str(api_base or "").strip()
    api_key = str(api_key or "").strip()
    token = str(token or "").strip()
    has_timeout, timeout_value = _opt_num(timeout, "超时")
    if not pid:
        raise ValueError("模型 ID 不能为空")
    if not model:
        raise ValueError("模型名不能为空")
    has_temp, temp = _opt_num(temperature, "温度")
    has_top, top_p_value = _opt_num(top_p, "Top P")
    has_ctx = max_context_tokens is not None
    max_ctx = 0
    if has_ctx:
        try:
            max_ctx = int(str(max_context_tokens or "0").strip() or 0)
        except ValueError as error:
            raise ValueError("最大上下文必须是整数") from error
    with _LOCK:
        data = _read_json(CMD_CONFIG)
        hit = next(
            (
                p
                for p in data.get("provider") or []
                if isinstance(p, dict) and str(p.get("id")) == pid
            ),
            None,
        )
        if not hit:
            raise ValueError("模型不存在")
        sources = {
            str(s.get("id")): s
            for s in data.get("provider_sources") or []
            if isinstance(s, dict) and s.get("id")
        }
        if source_id and source_id != str(hit.get("provider_source_id") or ""):
            if source_id not in sources:
                raise ValueError("提供商不存在: " + source_id)
            hit["provider_source_id"] = source_id
        src = sources.get(str(hit.get("provider_source_id") or ""))
        if src:
            _apply_source(src, api_base, api_key, token, has_timeout, timeout_value)
        hit["model"] = model
        if has_temp or has_top:
            extra = dict(hit.get("custom_extra_body") or {})
            if has_temp:
                if temp is None:
                    extra.pop("temperature", None)
                else:
                    extra["temperature"] = temp
            if has_top:
                if top_p_value is None:
                    extra.pop("top_p", None)
                else:
                    extra["top_p"] = top_p_value
            hit["custom_extra_body"] = extra
        if has_ctx and max_ctx:
            hit["max_context_tokens"] = max_ctx
        _write_json(CMD_CONFIG, data)
    _restart_astrbot()
    return {"ok": True, "id": pid, "model": model, "restarted": True, "provider_sources": provider_sources()}
