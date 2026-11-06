"""桌宠参数管控：只改 main-config.jsonc 的白名单字段，保留原注释。

宿主 /config 是热加载，渲染端每 30s 重拉一次；保存后最迟 30s 生效。
嵌套字段用点号表示（physics.gravity / font.size / eventsRefreshSec.whisper），
pets[] 相关字段只改第一只（main）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

CONFIG_PATH = Path.home() / ".dsh" / "dsh-pet" / "main-config.jsonc"
PROFILES = Path.home() / "dafeiyu" / "cosyvoice_ref" / "profiles"

# 顶层标量
TOP_FIELDS = {
    "voiceEnabled": "bool",
    "voiceName": "str",
    "voiceVolume": "num",
    "voiceWhisperEnabled": "bool",
    "voiceTodoEnabled": "bool",
    "actionSubtitleEnabled": "bool",
    "visionToolsEnabled": "bool",
    "visionWatchEnabled": "bool",
    "visionWatchSec": "num",
    "ollamaModel": "str",
    "ollamaBase": "str",
    "visionWechatBase": "str",
    "chatMemoryRounds": "num",
    "chatImageEnabled": "bool",
    "chatImageLimit": "num",
    "chatAnimationEnabled": "bool",
    "whisperImageEnabled": "bool",
    "notificationsEnabled": "bool",
    "bubbleBackdropEnabled": "bool",
    "bubbleBackdropCount": "num",
    "bubbleBackdropMotion": "enum",
    "bubbleBackdropPalette": "enum",
    "bubbleRiseSfxEnabled": "bool",
    "confineToScreen": "bool",
    "trayEnabled": "bool",
}
# 顶层对象里的子字段
NESTED_FIELDS = {
    "physics.gravity": "num",
    "physics.restitution": "num",
    "physics.groundFriction": "num",
    "physics.throwPower": "num",
    "physics.ceilingBounce": "bool",
    "physics.petCollision": "bool",
    "physics.hapticEnabled": "bool",
    "physics.tiltGravity": "bool",
    "physics.shakeThrow": "bool",
    "font.family": "enum",
    "font.size": "num",
    "font.followPet": "bool",
    "eventsRefreshSec.balance": "num",
    "eventsRefreshSec.whisper": "num",
    "eventsRefreshSec.sleepTalk": "num",
    "animations.speed": "num",
    "animations.switchSec": "num",
    "animations.sleepEnabled": "bool",
}
# 模型 API：空字符串 = 跟随当前会话模型
MODEL_FIELDS = {
    "whisperModel.provider": "str0",
    "whisperModel.model": "str0",
    "chatModel.provider": "str0",
    "chatModel.model": "str0",
}
# pets[] 第一只（main）的字段
PET_FIELDS = {
    "name": "str",
    "size": "num",
    "display": "enum",
    "balanceEnabled": "bool",
    "whisperEnabled": "bool",
    "workStatusEnabled": "bool",
    "fixedEnabled": "bool",
}
PET_NESTED = {
    "position.corner": "enum",
    "position.marginX": "num",
    "position.marginY": "num",
}

LIMITS = {
    "voiceVolume": (0.0, 1.0),
    "visionWatchSec": (60, 86400),
    "chatMemoryRounds": (0, 100),
    "chatImageLimit": (0, 200),
    "physics.gravity": (0, 100000),
    "physics.restitution": (0.0, 1.0),
    "physics.groundFriction": (0, 100),
    "physics.throwPower": (0.1, 10),
    "font.size": (4, 200),
    "eventsRefreshSec.balance": (30, 86400),
    "eventsRefreshSec.whisper": (10, 86400),
    "eventsRefreshSec.sleepTalk": (10, 86400),
    "animations.speed": (0.25, 3.0),
    "animations.switchSec": (2, 120),
    "bubbleBackdropCount": (0, 24),
    "size": (50, 2000),
    "position.marginX": (0, 5000),
    "position.marginY": (0, 5000),
}
ENUMS = {
    "font.family": {"candy", "yahei", "youyuan", "kaiti", "simsun", "comic", "system"},
    "bubbleBackdropMotion": {"calm", "normal", "lively"},
    "bubbleBackdropPalette": {"candy", "ocean", "sunset", "rainbow", "mono"},
    "display": {"web", "desktop", "both", "none"},
    "position.corner": {"top-right", "top-left", "bottom-right", "bottom-left"},
}

# ---------- 桌宠中转服务（bots/pet-relay.js，端口 23334） ----------
RELAY_CONFIG = Path.home() / "dafeiyu" / "bots" / "config.json"
RELAY_URL = "http://127.0.0.1:23334"
RELAY_FIELDS = {
    "pet": "str",
    "petPath": "str",
    "petKind": "enum",
    "ollama": "str",
    "model": "str",
    "brain": "str0",
    "narrate": "bool",
    "timeoutMs": "num",
    "brainTimeoutMs": "num",
    "periodicEnabled": "bool",
    "periodicMinutes": "num",
    "petChatIntervalMs": "num",
    "petMinGapMs": "num",
}
RELAY_LIMITS = {
    "timeoutMs": (1000, 120000),
    "brainTimeoutMs": (1000, 120000),
    "periodicMinutes": (1, 1440),
    "petChatIntervalMs": (0, 600000),
    "petMinGapMs": (0, 600000),
}
RELAY_ENUMS = {"petKind": {"auto", "dsh-pet", "dafeiyu3d"}}
RELAY_DEFAULTS = {
    "port": 23334,
    "pet": "http://127.0.0.1:23335",
    "petPath": "/state",
    "petKind": "auto",
    "ollama": "http://127.0.0.1:11434",
    "model": "qwen3.5:9b",
    "brain": "http://127.0.0.1:23402",
    "brainTimeoutMs": 10000,
    "narrate": True,
    "timeoutMs": 15000,
    "periodicEnabled": True,
    "periodicMinutes": 30,
    "petChatIntervalMs": 15000,
    "petMinGapMs": 10000,
}


def _strip_jsonc(raw: str) -> str:
    """去 // 与 /* */ 注释、去尾逗号（字符串内的斜杠原样保留）。"""
    out = []
    i = 0
    in_str = False
    while i < len(raw):
        ch = raw[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < len(raw):
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(raw) and raw[i + 1] == "/":
            while i < len(raw) and raw[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < len(raw) and raw[i + 1] == "*":
            i += 2
            while i + 1 < len(raw) and not (raw[i] == "*" and raw[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


def _active_voice() -> str:
    try:
        name = (PROFILES / ".active").read_text("utf-8").strip()
    except OSError:
        name = ""
    return name or "姬子"


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def read_config() -> Dict[str, Any]:
    raw = CONFIG_PATH.read_text("utf-8")
    data = json.loads(_strip_jsonc(raw))
    main = next((p for p in data.get("pets") or [] if isinstance(p, dict)), {})
    out: Dict[str, Any] = {key: data.get(key) for key in TOP_FIELDS}
    out.update({key: main.get(key) for key in PET_FIELDS})
    out.update({key: _dig(data, key) for key in NESTED_FIELDS})
    out.update({key: _dig(data, key) for key in MODEL_FIELDS})
    out.update({key: _dig(main, key) for key in PET_NESTED})
    out["activeVoice"] = _active_voice()
    return out


def _coerce(key: str, kind: str, value: Any, limits: Optional[Dict[str, Tuple[float, float]]] = None,
            enums: Optional[Dict[str, set]] = None) -> str:
    """把网页传来的值转成写回 JSONC 的字面量；类型不对就报错（不猜）。"""
    if kind == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return "true" if value.strip().lower() == "true" else "false"
        raise ValueError(f"{key} 需要布尔值")
    if kind == "str0":
        # 允许空字符串：模型 API 留空 = 跟随当前会话模型
        text = value if isinstance(value, str) else str(value)
        return json.dumps(text.strip(), ensure_ascii=False)
    if kind == "str":
        text = value if isinstance(value, str) else str(value)
        text = text.strip()
        if not text:
            raise ValueError(f"{key} 不能为空")
        return json.dumps(text, ensure_ascii=False)
    if kind == "enum":
        text = (value if isinstance(value, str) else str(value)).strip()
        allowed = (enums if enums is not None else ENUMS).get(key) or set()
        if text not in allowed:
            raise ValueError(f"{key} 只能是 {' / '.join(sorted(allowed))}")
        return json.dumps(text)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{key} 需要数字") from None
    low, high = (limits if limits is not None else LIMITS).get(key, (-float("inf"), float("inf")))
    if not low <= float(value) <= high:
        raise ValueError(f"{key} 超出范围 {low}~{high}")
    return str(int(value)) if float(value).is_integer() else str(float(value))


def _match_brace(raw: str, open_idx: int) -> int:
    """从 { 开始做括号配对（跳过字符串与注释），返回对应 } 的下标。"""
    depth = 0
    i = open_idx
    in_str = False
    while i < len(raw):
        ch = raw[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "/" and i + 1 < len(raw) and raw[i + 1] == "/":
            while i < len(raw) and raw[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < len(raw) and raw[i + 1] == "*":
            i += 2
            while i + 1 < len(raw) and not (raw[i] == "*" and raw[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("配置结构不完整：括号没有闭合")


def _object_span(raw: str, key: str, start: int = 0) -> Tuple[int, int]:
    match = re.search(r'"' + re.escape(key) + r'"\s*:\s*\{', raw[start:])
    if not match:
        raise ValueError(f"配置里没有 {key} 字段")
    open_idx = start + match.end() - 1
    return open_idx, _match_brace(raw, open_idx)


def _pets_first_span(raw: str) -> Tuple[int, int]:
    match = re.search(r'"pets"\s*:\s*\[', raw)
    if not match:
        raise ValueError("配置里没有 pets 字段")
    open_idx = raw.find("{", match.end())
    if open_idx < 0:
        raise ValueError("pets 里没有宠物条目")
    return open_idx, _match_brace(raw, open_idx)


_VALUE_PATTERN = r'(true|false|-?\d+(?:\.\d+)?|"(?:[^"\\]|\\.)*")'


def _replace_in_span(raw: str, span: Tuple[int, int], key: str, literal: str) -> str:
    start, end = span
    segment = raw[start : end + 1]
    pattern = re.compile(r'("' + re.escape(key) + r'"\s*:\s*)' + _VALUE_PATTERN)
    updated, count = pattern.subn(lambda m: m.group(1) + literal, segment, count=1)
    if count == 0:
        raise ValueError(f"配置里没有 {key} 字段")
    return raw[:start] + updated + raw[end + 1 :]


def _replace_top(raw: str, key: str, literal: str) -> str:
    pattern = re.compile(r'("' + re.escape(key) + r'"\s*:\s*)' + _VALUE_PATTERN)
    updated, count = pattern.subn(lambda m: m.group(1) + literal, raw, count=1)
    if count == 0:
        raise ValueError(f"配置里没有 {key} 字段")
    return updated


def save_config(body: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, str] = {}
    for key, value in body.items():
        kind = (TOP_FIELDS.get(key) or NESTED_FIELDS.get(key) or MODEL_FIELDS.get(key)
                or PET_FIELDS.get(key) or PET_NESTED.get(key))
        if not kind:
            raise ValueError(f"不支持修改 {key}")
        if value is None or (value == "" and kind != "str0"):
            continue
        normalized[key] = _coerce(key, kind, value)
    if not normalized:
        raise ValueError("没有要保存的参数")

    raw = CONFIG_PATH.read_text("utf-8")
    for key, literal in normalized.items():
        if key in TOP_FIELDS:
            raw = _replace_top(raw, key, literal)
        elif key in PET_FIELDS:
            raw = _replace_in_span(raw, _pets_first_span(raw), key, literal)
        elif key in NESTED_FIELDS or key in MODEL_FIELDS:
            section, sub = key.split(".", 1)
            raw = _replace_in_span(raw, _object_span(raw, section), sub, literal)
        else:  # pets[0].position.*
            _, sub = key.split(".", 1)
            pet_span = _pets_first_span(raw)
            position_span = _object_span(raw, "position", pet_span[0])
            raw = _replace_in_span(raw, position_span, sub, literal)
    CONFIG_PATH.write_text(raw, "utf-8")
    return read_config()


def handle_get(_req: Any = None) -> Any:
    from infra import fail, ok

    try:
        return ok(read_config())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return fail(f"读取桌宠配置失败：{error}", 500)


def handle_save(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(save_config(payload))
    except ValueError as error:
        return fail(str(error), 400)


# ---------- 中转服务配置 ----------
def read_relay() -> Dict[str, Any]:
    raw = RELAY_CONFIG.read_text("utf-8-sig")
    data = json.loads(raw) if raw.strip() else {}
    out: Dict[str, Any] = {key: data.get(key, RELAY_DEFAULTS.get(key)) for key in RELAY_FIELDS}
    out["port"] = int(data.get("port") or RELAY_DEFAULTS["port"])
    return out


def relay_status() -> Dict[str, Any]:
    from .http_client import http_call

    status, data = http_call("GET", RELAY_URL + "/health", timeout=2.0)
    online = status == 200 and isinstance(data, dict) and data.get("ok") is True
    return {"online": online, "health": data if isinstance(data, dict) else None}


def save_relay(body: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key, value in body.items():
        kind = RELAY_FIELDS.get(key)
        if not kind:
            raise ValueError(f"不支持修改 {key}")
        if value is None or (value == "" and kind != "str0"):
            continue
        normalized[key] = json.loads(_coerce(key, kind, value, RELAY_LIMITS, RELAY_ENUMS))
    if not normalized:
        raise ValueError("没有要保存的参数")

    raw = RELAY_CONFIG.read_text("utf-8-sig")
    data = json.loads(raw) if raw.strip() else {}
    data.update(normalized)
    data.setdefault("port", RELAY_DEFAULTS["port"])
    RELAY_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=4), "utf-8")

    from . import manage

    restart = manage.run("pet", "restart")
    return {"config": read_relay(), "restart": restart}


def handle_relay_get(_req: Any = None) -> Any:
    from infra import fail, ok

    try:
        return ok({"config": read_relay(), "status": relay_status()})
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return fail(f"读取桌宠中转配置失败：{error}", 500)


def handle_relay_save(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(save_relay(payload))
    except ValueError as error:
        return fail(str(error), 400)
    except OSError as error:
        return fail(f"保存桌宠中转配置失败：{error}", 500)


# ---------- 桌宠控制 API ----------
def _pet_base() -> str:
    """从中转配置推出 dsh-pet 的接口前缀（如 http://127.0.0.1:3080/dsh-pet-7340）。"""
    cfg = read_relay()
    base = str(cfg.get("pet") or "").rstrip("/")
    path = str(cfg.get("petPath") or "/state").split("?", 1)[0]
    for suffix in ("/broadcast", "/state"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return base + path


def _upstream_error(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body.get("reason") or body)
    return str(body)


def control(body: Dict[str, Any]) -> Dict[str, Any]:
    from .http_client import http_call

    action = str(body.get("action") or "").strip()
    base = _pet_base()
    text = str(body.get("text") or "").strip()

    if action in {"say", "chat"} and not text:
        raise ValueError("text 不能为空")
    if action == "anim" and not str(body.get("name") or "").strip():
        raise ValueError("name 不能为空")
    if action == "history":
        keep = body.get("keep", 10)
        try:
            keep = int(keep)
        except (TypeError, ValueError):
            raise ValueError("keep 需要整数") from None
        if not 0 <= keep <= 200:
            raise ValueError("keep 超出范围 0~200")
        status, found = http_call("GET", base + "/chat?pet=main&rounds=200", timeout=8.0)
        if status != 200 or not isinstance(found, dict) or not found.get("ok"):
            raise RuntimeError(_upstream_error(found))
        stamps = sorted({
            int(item["ts"]) for item in found.get("messages") or []
            if isinstance(item, dict) and isinstance(item.get("ts"), int)
        })
        doomed = stamps[:-keep] if keep > 0 else stamps
        if not doomed:
            return {"action": action, "removed": 0, "kept": keep}
        query = "".join(f"&ts={stamp}" for stamp in doomed)
        status, removed = http_call("DELETE", base + "/chat?pet=main" + query, timeout=8.0)
        if status != 200 or not isinstance(removed, dict) or not removed.get("ok"):
            raise RuntimeError(_upstream_error(removed))
        return {"action": action, "removed": removed.get("removed", 0), "kept": keep}
    if action == "history-clear":
        status, removed = http_call("DELETE", base + "/chat?pet=main", timeout=8.0)
        if status != 200 or not isinstance(removed, dict) or not removed.get("ok"):
            raise RuntimeError(_upstream_error(removed))
        return {"action": action, "removed": removed.get("removed", 0)}

    targets = {
        "say": ("/broadcast?pet=main&surface=desktop", {
            "text": text,
            **({"duration": body["duration"]} if body.get("duration") not in (None, "") else {}),
            **({"anim": str(body["anim"])} if body.get("anim") not in (None, "") else {}),
        }),
        "chat": ("/chat?pet=main&surface=desktop", {"text": text}),
        "anim": ("/anim?pet=main&surface=desktop", {"name": str(body.get("name")).strip()}),
        "clear": ("/bubble/clear?pet=main&surface=desktop", {}),
        "balance": ("/balance", {}),
    }
    if action not in targets:
        raise ValueError(f"未知操作 {action or '（空）'}")
    path, payload = targets[action]
    status, result = http_call("POST", base + path, payload, timeout=10.0)
    if status != 200 or not isinstance(result, dict) or result.get("ok") is False:
        raise RuntimeError(_upstream_error(result))
    return {"action": action, "result": result}


def handle_control(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(control(payload))
    except ValueError as error:
        return fail(str(error), 400)
    except (RuntimeError, OSError) as error:
        return fail(f"桌宠接口调用失败：{error}", 502)
