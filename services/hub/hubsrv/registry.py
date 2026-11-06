"""服务注册表：扫描 services/*/service.json、端口健康探测、能力聚合。

registry 依赖 catalog（工具目录）、probe（端口探活）与 http_client；tools / api 依赖本模块。
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from .catalog import TOOLS
from .config import LAYERS, LAYER_BY_NAME, PINNED, PORT, SERVICES_DIR
from .http_client import http_call as _http
from .probe import port_alive as _port_alive

# 清单缓存：目录里任何一个 service.json 的 mtime/大小变化都会让签名失效。
_MANIFEST_LOCK = threading.Lock()
_MANIFEST_CACHE: Dict[str, Any] = {"signature": None, "rows": []}

# /health 结果短缓存：前端高频刷新时不必每次都打 HTTP。
_PROBE_LOCK = threading.Lock()
_PROBE_CACHE: Dict[tuple, tuple] = {}
PROBE_TTL = 2.5
PROBE_WORKERS = 8


def _manifest_signature() -> tuple:
    entries = []
    for path in SERVICES_DIR.glob("*/service.json"):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(entries))


def _load_manifests() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in sorted(SERVICES_DIR.glob("*/service.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        manifest.setdefault("name", path.parent.name)
        manifest.setdefault("optional", manifest.get("status") == "optional")
        rows.append(manifest)
    return rows


def _read_manifests() -> List[Dict[str, Any]]:
    """带 mtime 缓存的清单读取；目录内任一 service.json 变化即重新加载。"""
    signature = _manifest_signature()
    with _MANIFEST_LOCK:
        if _MANIFEST_CACHE["signature"] == signature:
            return _MANIFEST_CACHE["rows"]
    rows = _load_manifests()
    with _MANIFEST_LOCK:
        _MANIFEST_CACHE["signature"] = signature
        _MANIFEST_CACHE["rows"] = rows
    with _PROBE_LOCK:
        # 端口可能随清单调整，清掉旧探活结果，避免短缓存命中错误端口。
        _PROBE_CACHE.clear()
    return rows


def _probe(manifest: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
    """在线判定 + /health 带回的附加信息（表情库在这里报张数）。

    清单里写 "probe": "port" 的服务（NapCat / AstrBot / 桌宠宿主等）只做端口探活，
    没有标准 /health 的进程也能正确显示在线。
    """
    port = manifest.get("port")
    if not isinstance(port, int):
        return False, {}
    name = manifest.get("name")
    if port == PORT or name == "hub":
        return True, {}
    key = (name, port)
    now = time.monotonic()
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
        if cached and now - cached[0] < PROBE_TTL:
            return cached[1], cached[2]
    if str(manifest.get("probe") or "").lower() == "port":
        online, health = _port_alive(port), {}
    else:
        status, body = _http("GET", f"http://127.0.0.1:{port}/health", timeout=0.9)
        online = status == 200 and isinstance(body, dict) and bool(body.get("ok", True))
        health = body if online else {}
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = (now, online, health)
    return online, health


def _online(manifest: Dict[str, Any]) -> bool:
    return _probe(manifest)[0]


def _capabilities() -> Dict[str, Any]:
    manifests = _read_manifests()
    visible = [
        manifest for manifest in manifests
        # hub 自己不出现在自己的清单里，不然点进去看到的还是自己。
        if manifest.get("name") != "hub" and manifest.get("port") != PORT
        # 模型服务的 provider / key 已在中枢「机器人 · 管控 → 模型层」里直接管理，
        # 单独一张「数据 /providers ↻」卡片没有意义，不再展示。
        and manifest.get("name") != "model"
        # 已并入其它服务的同类能力（merged_into）不再单独出卡片：
        # 例如字幕并入 audio、CosyVoice 归到音频域；进程本身照常被 manage 启停。
        and not manifest.get("merged_into")
    ]
    if visible:
        with ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(visible))) as pool:
            probes = list(pool.map(_probe, visible))
    else:
        probes = []

    services = []
    for manifest, (online, health) in zip(visible, probes):
        port = manifest.get("port")
        ui = str(manifest.get("ui") or "").strip()
        if isinstance(port, int):
            # "/ui" 这类相对路径必须补成绝对地址，否则在别处打开会挂到错误的端口。
            if ui.startswith("/"):
                ui = f"http://127.0.0.1:{port}{ui}"
            elif not ui:
                ui = f"http://127.0.0.1:{port}/health"
        services.append({
            "name": manifest.get("name"),
            "kind": manifest.get("kind"),
            "layer": manifest.get("layer") if manifest.get("layer") in LAYERS else _layer_of(manifest),
            "port": port,
            "optional": bool(manifest.get("optional")),
            "status": manifest.get("status", "active"),
            "responsibility": manifest.get("responsibility", ""),
            "endpoints": manifest.get("endpoints", []),
            "ui": ui,
            "links": _links(manifest),
            "online": online,
            "pin": manifest.get("name") in PINNED or bool(manifest.get("pin")),
            # 有 entry 的服务都能由控制台启停；hub 自己已在上面被跳过
            "manageable": bool(manifest.get("entry")),
            # 服务自己在 /health 报的数量（表情库用这个把张数反馈给中枢）
            "stats": health.get("stats") or {},
        })

    # 架构里的这层不是进程，但控制台必须把它摆出来，否则分层看起来缺一块。
    # 基础库只是共享 Python 包，不是可管理服务，界面不再给它留卡片。
    virtual = [
        {
            "name": "plugins",
            "kind": "插件",
            "layer": "能力层",
            "port": None,
            "optional": False,
            "responsibility": "插件系统：指令 / 表情 / MCP，按 scope 注册，只产出动作请求，不直接碰平台 API",
            "ui": "",
            "links": {},
            "online": True,
            "virtual": True,
        },
    ]
    rows = services + virtual
    layers = [
        {
            "name": layer,
            "services": [row for row in rows if row["layer"] == layer],
        }
        for layer in LAYERS
        if any(row["layer"] == layer for row in rows)
    ]
    ready = {tool["name"]: _tool_ready(tool, manifests) for tool in TOOLS}
    return {
        "owner": "hub",
        "ui": "/ui",
        "layers": layers,
        "services": rows,
        "tools": [tool for tool in TOOLS if ready[tool["name"]]],
        "tool_catalog": [
            {
                **tool,
                "ready": ready[tool["name"]],
                "depends": tool.get("requires") or " / ".join(tool.get("optional_backends") or []) or "无",
            }
            for tool in TOOLS
        ],
        "optional_missing": [row["name"] for row in rows if row["optional"] and not row["online"]],
    }


def _layer_of(manifest: Dict[str, Any]) -> str:
    name = manifest.get("name")
    kind = manifest.get("kind")
    if name in LAYER_BY_NAME:
        return LAYER_BY_NAME[name]
    if kind in {"registry", "pipeline", "dispatcher"}:
        return "中枢层"
    if kind == "platform-io":
        return "输入输出层"
    if kind == "edge":
        return "接入客户端"
    if kind in {"capability", "storage"}:
        return "能力层"
    return "外部适配"


def _links(manifest: Dict[str, Any]) -> Dict[str, str]:
    """把清单里的 GET 端点变成可点击的绝对地址。"""
    port = manifest.get("port")
    links: Dict[str, str] = {}
    if isinstance(port, int):
        for endpoint in manifest.get("endpoints", []):
            if endpoint.upper().startswith("GET "):
                path = endpoint[4:].strip()
                links[path] = f"http://127.0.0.1:{port}{path}"
    extra = manifest.get("links")
    if isinstance(extra, dict):
        links.update({str(key): str(value) for key, value in extra.items()})
    return links


def _service(manifests: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for manifest in manifests:
        if manifest.get("name") == name:
            return manifest
    return None


def _backend_online(name: str, manifests: Optional[List[Dict[str, Any]]] = None) -> bool:
    """可选后端是否在线：优先按清单里的端口探活，清单缺失时回落到约定端口。"""
    manifests = manifests if manifests is not None else _read_manifests()
    manifest = _service(manifests, name)
    if manifest is not None and isinstance(manifest.get("port"), int):
        return _port_alive(manifest["port"])
    if name == "napcat":
        return _port_alive(3000)
    if name in {"tts", "voice"}:
        return _port_alive(8714)
    return False


def _tool_ready(tool: Dict[str, Any], manifests: Optional[List[Dict[str, Any]]] = None) -> bool:
    backends = tool.get("optional_backends")
    if backends:
        return any(_backend_online(name, manifests) for name in backends)
    need = tool.get("requires")
    if not need:
        return True
    return _backend_online(need, manifests)
