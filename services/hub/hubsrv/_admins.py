"""管理员管理与通用基础：共享路径常量、JSON 读写小工具、全局锁、AstrBot 重启。

botctl 拆分后的基础模块：其余子模块（模型/角色/群组/声线/水群/工具/TTS）
从这里导入共享常量与工具函数，避免循环导入。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from infra import astrbot_dir, find_versioned_dir

ROOT = Path(__file__).resolve().parents[3]
ASTRBOT = astrbot_dir(ROOT) or find_versioned_dir(ROOT / "astrbot-win") or (ROOT / "astrbot-win" / "AstrBot")
DATA = ASTRBOT / "data"
PLUGIN_DIR = DATA / "plugins" / "astrbot_plugin_provider_router"
CONFIG_PATH = PLUGIN_DIR / "config.json"
CMD_CONFIG = DATA / "cmd_config.json"
HUB_DATA = ROOT / "services" / "hub" / "data"
ADMINS_PATH = HUB_DATA / "admins.json"

_LOCK = threading.Lock()


# ---------- 基础读写 ----------
def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"读取 {path.name} 失败: {error}") from error
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _as_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,，、\s]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _as_map(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in raw.items():
        key, value = str(key).strip(), str(value or "").strip()
        if key and value:
            out[key] = value
    return out


def admins() -> List[str]:
    """AstrBot 管理员（cmd_config.json 的 admins_id），拥有重启/配置等管理指令权限。"""
    data = _read_json(CMD_CONFIG)
    return [str(item).strip() for item in data.get("admins_id") or [] if str(item).strip()]


# tool_gate / local_tools 也会动态合并 admins_id；这里同步写入 owner_ids 是为了
# 兼容关掉 dynamic_admins 的旧配置（关掉后静态名单就是唯一来源）。
OWNER_ID_CONFIGS = (
    DATA / "plugins" / "astrbot_plugin_tool_gate" / "config.json",
    DATA / "plugins" / "astrbot_plugin_local_tools" / "config.json",
)


def _sync_owner_ids(rows: List[str]) -> bool:
    """把管理员列表同步到插件的 owner_ids，返回是否有变化。"""
    synced = False
    for path in OWNER_ID_CONFIGS:
        data: Dict[str, Any] = {}
        if path.is_file():
            try:
                data = _read_json(path)
            except ValueError:
                continue
        else:
            # 插件还没生成 config.json 时也要落盘，否则管理员名单永远同步不过去
            path.parent.mkdir(parents=True, exist_ok=True)
        current = [str(item).strip() for item in data.get("owner_ids") or [] if str(item).strip()]
        if current == rows:
            continue
        data["owner_ids"] = list(rows)
        _write_json(path, data)
        synced = True
    return synced


def _write_admins(rows: List[str]) -> bool:
    """写入管理员列表，返回是否需要重启 AstrBot（管理员或插件主人有变化）。"""
    for row in rows:
        if not row.isdigit() and not row.lower().startswith("wxid_"):
            raise ValueError("管理员必须是 QQ 号或微信 ID: " + row)
    data = _read_json(CMD_CONFIG)
    current = [str(item).strip() for item in data.get("admins_id") or [] if str(item).strip()]
    changed = current != rows
    if changed:
        data["admins_id"] = rows
        _write_json(CMD_CONFIG, data)
    synced = _sync_owner_ids(rows)
    return changed or synced


def _admin_store() -> Dict[str, Any]:
    data = _read_json(ADMINS_PATH)
    rows = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    return rows


def admin_profiles() -> Dict[str, Dict[str, str]]:
    """管理员资料：身份标签（管理员不再区分层级）。"""
    stored = _admin_store()
    ids = sorted(set(admins()) | {str(k) for k in stored})
    rows: Dict[str, Dict[str, str]] = {}
    for uid in ids:
        item = stored.get(uid) if isinstance(stored.get(uid), dict) else {}
        rows[uid] = {
            "identity": str(item.get("identity") or "").strip(),
            "name": str(item.get("name") or "").strip(),
        }
    return rows


def save_admin_profile(payload: Dict[str, Any]) -> Dict[str, Any]:
    """新增/修改管理员：保存身份标签，并把管理员同步到插件的 owner_ids。"""
    uid = str(payload.get("id") or "").strip()
    if not uid:
        raise ValueError("QQ 号 / 微信 ID 不能为空")
    identity = str(payload.get("identity") or "").strip()[:24]
    name = str(payload.get("name") or "").strip()[:40]
    restart = False
    with _LOCK:
        ids = admins()
        if uid not in ids:
            ids.append(uid)
        # 老管理员也要过一遍：历史上只写了 admins_id 没同步 owner_ids 的机子靠这里补齐
        restart = _write_admins(ids)
        stored = dict(_admin_store())
        stored[uid] = {"identity": identity, "name": name}
        HUB_DATA.mkdir(parents=True, exist_ok=True)
        _write_json(ADMINS_PATH, {"profiles": stored})
    if restart:
        _restart_astrbot()
    return {"admins": admins(), "admin_profiles": admin_profiles(), "restart": restart}


def remove_admin(uid_raw: Any) -> Dict[str, Any]:
    """移除管理员权限与身份资料，同时从插件的 owner_ids 里去掉。"""
    uid = str(uid_raw or "").strip()
    if not uid:
        raise ValueError("QQ 号 / 微信 ID 不能为空")
    restart = False
    with _LOCK:
        restart = _write_admins([item for item in admins() if item != uid])
        stored = dict(_admin_store())
        if uid in stored:
            stored.pop(uid, None)
            HUB_DATA.mkdir(parents=True, exist_ok=True)
            _write_json(ADMINS_PATH, {"profiles": stored})
    if restart:
        _restart_astrbot()
    return {"admins": admins(), "admin_profiles": admin_profiles(), "restart": restart}


_RESTART_RUNNING = False
_RESTART_PENDING = False


def _restart_astrbot() -> None:
    """后台重启 AstrBot：接口立刻返回，前端按钮不会一直转圈。

    重启中又改了配置时，等这轮结束后再补一次，避免新配置被旧进程吃掉。
    """
    global _RESTART_RUNNING, _RESTART_PENDING

    _RESTART_PENDING = True
    if _RESTART_RUNNING:
        return
    _RESTART_RUNNING = True

    def _worker() -> None:
        global _RESTART_RUNNING, _RESTART_PENDING
        from .manage import run as manage_run

        try:
            while _RESTART_PENDING:
                _RESTART_PENDING = False
                try:
                    manage_run("astrbot", "restart")
                except Exception:  # noqa: BLE001 - 重启失败不影响配置已落盘
                    pass
                time.sleep(1.0)
        finally:
            _RESTART_RUNNING = False

    threading.Thread(target=_worker, daemon=True, name="hub-restart-astrbot").start()
