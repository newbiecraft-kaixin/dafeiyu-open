"""机器人管控：读写 provider_router 配置、扫描指令、代查 QQ 群列表。

控制台改的是 astrbot 插件自己的 config.json；插件按 mtime 热加载，
保存后下一条消息即生效，不需要重启 AstrBot。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .http_client import http_call

ROOT = Path(__file__).resolve().parents[3]
ASTRBOT = ROOT / "astrbot-win" / "AstrBot-4.29.3"
DATA = ASTRBOT / "data"
PLUGIN_DIR = DATA / "plugins" / "astrbot_plugin_provider_router"
CONFIG_PATH = PLUGIN_DIR / "config.json"
CMD_CONFIG = DATA / "cmd_config.json"
PLUGINS_DIR = DATA / "plugins"
ONEBOT_CONFIG = ROOT / "napcat-win" / "napcat" / "config" / "onebot11.json"
PERSONA_DB = DATA / "data_v4.db"
VOICE_ROOT = ROOT / "cosyvoice_ref" / "profiles"
ACCESS_PATH = DATA / "local_tools" / "access.json"
TOOL_NAMES_PATH = DATA / "local_tools" / "tool_names.json"
HUB_DATA = ROOT / "services" / "hub" / "data"
ADMINS_PATH = HUB_DATA / "admins.json"

_LOCK = threading.Lock()

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "block_users": [],
    "block_groups": [],
    "listen_groups": [],
    "user_provider_ids": {},
    "group_provider_ids": {},
    "group_persona_ids": {},
    "user_persona_ids": {},
    "group_provider_id": "",
    "private_provider_id": "",
    "stranger_private_provider_id": "",
    "wechat_provider_id": "",
    "deepseek_group_ids": [],
    "deepseek_provider_id": "",
}

_LISTS = ("block_users", "block_groups", "listen_groups")
_MAPS = ("user_provider_ids", "group_provider_ids")
_TEXTS = (
    "group_provider_id",
    "private_provider_id",
    "stranger_private_provider_id",
    "wechat_provider_id",
    "deepseek_provider_id",
)


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


def _write_admins(rows: List[str]) -> bool:
    """写入管理员列表，返回是否真的有变化（变化才需要重启 AstrBot）。"""
    for row in rows:
        if not row.isdigit() and not row.lower().startswith("wxid_"):
            raise ValueError("管理员必须是 QQ 号或微信 ID: " + row)
    data = _read_json(CMD_CONFIG)
    current = [str(item).strip() for item in data.get("admins_id") or [] if str(item).strip()]
    if current == rows:
        return False
    data["admins_id"] = rows
    _write_json(CMD_CONFIG, data)
    return True


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
    """新增/修改管理员：只保存身份标签，不再维护层级、不改动 tool_gate 的 owner_ids。"""
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
            restart = _write_admins(ids)
        stored = dict(_admin_store())
        stored[uid] = {"identity": identity, "name": name}
        HUB_DATA.mkdir(parents=True, exist_ok=True)
        _write_json(ADMINS_PATH, {"profiles": stored})
    if restart:
        _restart_astrbot()
    return {"admins": admins(), "admin_profiles": admin_profiles(), "restart": restart}


def remove_admin(uid_raw: Any) -> Dict[str, Any]:
    """移除管理员权限与身份资料（不改动 tool_gate 的 owner_ids）。"""
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


# ---------- 可用模型来源 / 角色 ----------
def provider_sources() -> List[Dict[str, str]]:
    data = _read_json(CMD_CONFIG)
    rows: List[Dict[str, str]] = []
    for item in data.get("provider_sources") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        rows.append({
            "id": str(item.get("id")),
            "type": str(item.get("type") or ""),
            "api_base": str(item.get("api_base") or ""),
            "has_key": bool(item.get("key")),
        })
    return rows


def _persona_title(persona_id: str, prompt: str) -> str:
    for line in (prompt or "").splitlines():
        text = line.strip().lstrip("#").strip()
        if line.strip().startswith("#") and text:
            return text
    return persona_id


def personas() -> List[Dict[str, str]]:
    """AstrBot 现有人格（data_v4.db 只读）。"""
    if not PERSONA_DB.exists():
        return []
    try:
        uri = "file:" + str(PERSONA_DB).replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=3)
        try:
            rows = con.execute(
                "SELECT persona_id, system_prompt FROM personas ORDER BY id"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [
        {
            "id": str(pid),
            "name": _persona_title(str(pid), str(prompt or "")),
            "prompt_len": len(str(prompt or "")),
        }
        for pid, prompt in rows
    ]


# ---------- 配置读写 ----------
def get_config() -> Dict[str, Any]:
    cfg = _read_json(CONFIG_PATH)
    editable = {key: cfg.get(key, DEFAULTS[key]) for key in DEFAULTS}
    editable["block_users"] = _as_list(editable["block_users"])
    editable["block_groups"] = _as_list(editable["block_groups"])
    editable["listen_groups"] = _as_list(editable["listen_groups"])
    editable["deepseek_group_ids"] = _as_list(editable["deepseek_group_ids"])
    editable["user_provider_ids"] = _as_map(editable["user_provider_ids"])
    editable["group_provider_ids"] = _as_map(editable["group_provider_ids"])
    editable["group_persona_ids"] = _as_map(editable["group_persona_ids"])
    editable["user_persona_ids"] = _as_map(editable["user_persona_ids"])
    return {
        "config": editable,
        "admins": admins(),
        "admin_profiles": admin_profiles(),
        "providers": providers(),
        "provider_sources": provider_sources(),
        "personas": personas(),
        "plugin_online": PLUGIN_DIR.exists(),
        "listen_mode": "only" if editable["listen_groups"] else "all",
    }


def save_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """只合并白名单键，避免控制台把插件其它配置写坏。"""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    known = {row["id"] for row in providers()}
    restart = False
    with _LOCK:
        cfg = _read_json(CONFIG_PATH)
        for key in _LISTS:
            if key in payload:
                cfg[key] = _as_list(payload[key])
        for key in _MAPS:
            if key in payload:
                mapping = _as_map(payload[key])
                unknown = [pid for pid in mapping.values() if known and pid not in known]
                if unknown:
                    raise ValueError("未知模型: " + "、".join(sorted(set(unknown))))
                cfg[key] = mapping
        valid = {row["id"] for row in personas()}
        for key in ("group_persona_ids", "user_persona_ids"):
            if key not in payload:
                continue
            mapping = _as_map(payload[key])
            unknown = [pid for pid in mapping.values() if valid and pid not in valid]
            if unknown:
                raise ValueError("未知角色: " + "、".join(sorted(set(unknown))))
            cfg[key] = mapping
        for key in _TEXTS:
            if key in payload:
                cfg[key] = str(payload[key] or "").strip()
        if "enabled" in payload:
            cfg["enabled"] = bool(payload["enabled"])
        if "admins" in payload:
            restart = _write_admins(_as_list(payload["admins"]))
        _write_json(CONFIG_PATH, cfg)
    if restart:
        _restart_astrbot()
    return get_config()


# ---------- 指令清单 ----------
_CMD_RE = re.compile(r'@filter\.command\(\s*"([^"]+)"\s*(?:,\s*alias\s*=\s*\{([^}]*)\})?')
_DOC_RE = re.compile(r'("""|\'\'\')(.*?)\1', re.S)
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", "venv"}


def _plugin_meta(folder: Path) -> Dict[str, str]:
    meta: Dict[str, str] = {"name": folder.name, "desc": ""}
    path = folder / "metadata.yaml"
    if not path.exists():
        return meta
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return meta
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() in {"name", "desc"} and value.strip():
            meta[key.strip()] = value.strip()
    return meta


def _iter_sources(folder: Path):
    for path in folder.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def commands() -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    if not PLUGINS_DIR.exists():
        return {"commands": rows, "plugins": 0}
    plugins = 0
    for folder in sorted(p for p in PLUGINS_DIR.iterdir() if p.is_dir()):
        if folder.name == "data" or not any(_iter_sources(folder)):
            continue
        meta = _plugin_meta(folder)
        found = False
        for path in _iter_sources(folder):
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            for match in _CMD_RE.finditer(text):
                command = match.group(1).strip()
                aliases = re.findall(r'"([^"]+)"', match.group(2) or "")
                doc = _DOC_RE.search(text, match.end())
                desc = ""
                if doc:
                    for line in doc.group(2).strip().splitlines():
                        if line.strip():
                            desc = line.strip()
                            break
                rows.append({
                    "plugin": meta["name"],
                    "plugin_desc": meta["desc"],
                    "command": command,
                    "aliases": aliases,
                    "desc": desc,
                })
                found = True
        if found:
            plugins += 1
    return {"commands": rows, "plugins": plugins}


# ---------- 群列表（QQ 走 NapCat，微信走 io-wechat）----------
WECHAT_API = "http://127.0.0.1:23392"

_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\ufeff]")
# QQ 群名尾部可能带 <$ÿĀ> 这种协议残留标记，显示时去掉
_TAG_RE = re.compile(r"<\$[^<>]{0,12}>$")


def _clean_name(value: str, fallback: str) -> str:
    """群名里的控制字符/方向符和 <$…> 残留标记去掉，避免中枢显示成乱码。"""
    text = _CTRL_RE.sub("", str(value or "")).strip()
    text = _TAG_RE.sub("", text).strip()
    return text or fallback


def _onebot_token() -> str:
    try:
        data = _read_json(ONEBOT_CONFIG)
        for server in (data.get("network") or {}).get("httpServers") or []:
            if int(server.get("port") or 0) == 3000:
                return str(server.get("token") or "")
    except (OSError, ValueError, TypeError):
        pass
    return ""


def _qq_groups() -> tuple[bool, List[Dict[str, Any]], str]:
    token = _onebot_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    status, body = http_call(
        "POST", "http://127.0.0.1:3000/get_group_list", {}, timeout=3.0, headers=headers
    )
    if status != 200 or not isinstance(body, dict):
        return False, [], f"NapCat 无响应（HTTP {status}）"
    if body.get("status") != "ok":
        return False, [], str(body.get("message") or body.get("wording") or "调用失败")
    rows = [
        {
            "id": str(item.get("group_id")),
            "name": _clean_name(item.get("group_name"), str(item.get("group_id"))),
            "member_count": item.get("member_count"),
            "platform": "qq",
        }
        for item in body.get("data") or []
        if item.get("group_id")
    ]
    return True, rows, ""


def _wechat_groups() -> tuple[bool, List[Dict[str, Any]], str]:
    status, body = http_call("GET", f"{WECHAT_API}/groups", None, timeout=3.0)
    if status != 200 or not isinstance(body, dict):
        return False, [], f"微信接入无响应（HTTP {status}）"
    payload = body.get("data") if isinstance(body.get("data"), dict) else body
    if body.get("ok") is False:
        return False, [], str(body.get("error") or "微信未附着")
    rows = [
        {
            "id": str(item.get("id")),
            "name": _clean_name(item.get("name"), str(item.get("id"))),
            "platform": "wechat",
        }
        for item in payload.get("groups") or []
        if item.get("id")
    ]
    return True, rows, ""


def groups() -> Dict[str, Any]:
    """QQ 群 + 微信群合并，前端按 platform 区分。"""
    qq_ok, qq_rows, qq_error = _qq_groups()
    wx_ok, wx_rows, wx_error = _wechat_groups()
    return {
        "online": qq_ok or wx_ok,
        "groups": qq_rows + wx_rows,
        "qq_online": qq_ok,
        "wechat_online": wx_ok,
        "error": "；".join(e for e in (qq_error, wx_error) if e),
    }


def _onebot(action: str, payload: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
    token = _onebot_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    status, body = http_call(
        "POST", f"http://127.0.0.1:3000/{action}", payload, timeout=timeout, headers=headers
    )
    if status != 200 or not isinstance(body, dict):
        return {"ok": False, "error": f"NapCat 无响应（HTTP {status}）"}
    if body.get("status") != "ok":
        return {"ok": False, "error": str(body.get("message") or body.get("wording") or "调用失败")}
    return {"ok": True, "data": body.get("data")}


def _qq_friends() -> tuple[bool, List[Dict[str, Any]], str]:
    result = _onebot("get_friend_list", {}, timeout=3.0)
    if not result["ok"]:
        return False, [], str(result.get("error") or "调用失败")
    rows = [
        {
            "id": str(item.get("user_id")),
            "name": _clean_name(item.get("remark") or item.get("nickname"), str(item.get("user_id"))),
            "platform": "qq",
        }
        for item in result.get("data") or []
        if item.get("user_id")
    ]
    return True, rows, ""


def _wechat_friends() -> tuple[bool, List[Dict[str, Any]], str]:
    status, body = http_call("GET", f"{WECHAT_API}/friends", None, timeout=3.0)
    if status != 200 or not isinstance(body, dict):
        return False, [], f"微信接入无响应（HTTP {status}）"
    if body.get("ok") is False:
        return False, [], str(body.get("error") or "微信未附着")
    payload = body.get("data") if isinstance(body.get("data"), dict) else body
    rows = [
        {
            "id": str(item.get("id")),
            "name": _clean_name(item.get("name"), str(item.get("id"))),
            "platform": "wechat",
        }
        for item in payload.get("friends") or []
        if item.get("id")
    ]
    return True, rows, ""


def friends() -> Dict[str, Any]:
    """QQ 好友 + 微信好友合并，前端按 platform 区分。"""
    qq_ok, qq_rows, qq_error = _qq_friends()
    wx_ok, wx_rows, wx_error = _wechat_friends()
    return {
        "online": qq_ok or wx_ok,
        "friends": qq_rows + wx_rows,
        "qq_online": qq_ok,
        "wechat_online": wx_ok,
        "error": "；".join(e for e in (qq_error, wx_error) if e),
    }


def leave_group(group_id: str) -> Dict[str, Any]:
    try:
        gid = int(str(group_id).strip())
    except ValueError as error:
        raise ValueError("群号非法") from error
    result = _onebot("set_group_leave", {"group_id": gid})
    if not result["ok"]:
        raise ValueError(result.get("error") or "退群失败")
    gid_text = str(gid)
    with _LOCK:
        cfg = _read_json(CONFIG_PATH)
        for key in ("listen_groups", "block_groups"):
            cfg[key] = [g for g in _as_list(cfg.get(key)) if g != gid_text]
        for key in ("group_provider_ids", "group_persona_ids"):
            mapping = _as_map(cfg.get(key))
            mapping.pop(gid_text, None)
            cfg[key] = mapping
        _write_json(CONFIG_PATH, cfg)
    return {"ok": True, "group_id": gid_text}


def delete_friend(user_id: str) -> Dict[str, Any]:
    try:
        uid = int(str(user_id).strip())
    except ValueError as error:
        raise ValueError("QQ 号非法") from error
    result = _onebot("delete_friend", {"user_id": uid})
    if not result["ok"]:
        raise ValueError(result.get("error") or "删好友失败")
    uid_text = str(uid)
    with _LOCK:
        cfg = _read_json(CONFIG_PATH)
        cfg["block_users"] = [u for u in _as_list(cfg.get("block_users")) if u != uid_text]
        mapping = _as_map(cfg.get("user_provider_ids"))
        mapping.pop(uid_text, None)
        cfg["user_provider_ids"] = mapping
        _write_json(CONFIG_PATH, cfg)
    return {"ok": True, "user_id": uid_text}


# ---------- 角色增删 ----------
def create_persona(name: str, prompt: str, persona_id: str = "") -> Dict[str, Any]:
    name, prompt = str(name or "").strip(), str(prompt or "").strip()
    if not name:
        raise ValueError("角色名称不能为空")
    if not prompt:
        raise ValueError("角色提示词不能为空")
    pid = re.sub(r"[^a-zA-Z0-9_-]", "", str(persona_id or "")).strip()
    pid = pid or f"persona{int(time.time())}"
    text = prompt if prompt.lstrip().startswith("#") else f"# {name}\n\n{prompt}"
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(str(PERSONA_DB), timeout=8)
    try:
        if con.execute("SELECT 1 FROM personas WHERE persona_id=?", (pid,)).fetchone():
            raise ValueError(f"角色 ID 已存在: {pid}")
        con.execute(
            "INSERT INTO personas (created_at, updated_at, persona_id, system_prompt, "
            "begin_dialogs, tools, skills, sort_order) VALUES (?, ?, ?, ?, '[]', '[]', '[]', 0)",
            (now, now, pid, text),
        )
        con.commit()
    finally:
        con.close()
    _restart_astrbot()
    return {"ok": True, "id": pid, "name": name, "restarted": True}


def delete_persona(persona_id: str) -> Dict[str, Any]:
    pid = str(persona_id or "").strip()
    if pid in ("", "default"):
        raise ValueError("默认角色不能删除")
    cfg = _read_json(CONFIG_PATH)
    used = [g for g, v in _as_map(cfg.get("group_persona_ids")).items() if v == pid]
    if used:
        raise ValueError("角色仍被这些群使用: " + "、".join(used))
    used_users = [u for u, v in _as_map(cfg.get("user_persona_ids")).items() if v == pid]
    if used_users:
        raise ValueError("角色仍被这些好友使用: " + "、".join(used_users))
    if str(cfg.get("wechat_persona_id") or "") == pid:
        raise ValueError("角色仍被微信侧使用")
    con = sqlite3.connect(str(PERSONA_DB), timeout=8)
    try:
        cur = con.execute("DELETE FROM personas WHERE persona_id=?", (pid,))
        con.commit()
        if cur.rowcount == 0:
            raise ValueError("角色不存在")
    finally:
        con.close()
    _restart_astrbot()
    return {"ok": True, "id": pid, "restarted": True}


def persona_detail(persona_id: str) -> Dict[str, Any]:
    pid = str(persona_id or "").strip()
    if not pid:
        raise ValueError("角色 ID 不能为空")
    if not PERSONA_DB.exists():
        raise ValueError("人格库不存在")
    con = sqlite3.connect(str(PERSONA_DB), timeout=8)
    try:
        row = con.execute(
            "SELECT system_prompt FROM personas WHERE persona_id=?", (pid,)
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise ValueError("角色不存在")
    text = str(row[0] or "")
    return {"id": pid, "name": _persona_title(pid, text), "prompt": text}


def update_persona(persona_id: str, name: str, prompt: str) -> Dict[str, Any]:
    pid = str(persona_id or "").strip()
    name, prompt = str(name or "").strip(), str(prompt or "").strip()
    if not pid:
        raise ValueError("角色 ID 不能为空")
    if not name:
        raise ValueError("角色名称不能为空")
    if not prompt:
        raise ValueError("角色提示词不能为空")
    lines = prompt.splitlines()
    if lines and lines[0].strip().startswith("#"):
        lines[0] = f"# {name}"
        text = "\n".join(lines)
    else:
        text = f"# {name}\n\n{prompt}"
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(str(PERSONA_DB), timeout=8)
    try:
        cur = con.execute(
            "UPDATE personas SET system_prompt=?, updated_at=? WHERE persona_id=?",
            (text, now, pid),
        )
        con.commit()
        if cur.rowcount == 0:
            raise ValueError("角色不存在")
    finally:
        con.close()
    _restart_astrbot()
    return {"ok": True, "id": pid, "name": name, "restarted": True}


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
) -> Dict[str, Any]:
    source_id, model = str(source_id or "").strip(), str(model or "").strip()
    source_new = str(source_new or "").strip()
    api_base, api_key = str(api_base or "").strip(), str(api_key or "").strip()
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
            if api_base:
                src["api_base"] = api_base
            if api_key:
                src["key"] = [api_key] if isinstance(src.get("key"), list) else api_key
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
    return {"ok": True, "id": pid, "restarted": True}


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
) -> Dict[str, Any]:
    pid = str(provider_id or "").strip()
    model = str(model or "").strip()
    source_id, api_base = str(source_id or "").strip(), str(api_base or "").strip()
    api_key = str(api_key or "").strip()
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
            if api_base:
                src["api_base"] = api_base
            if api_key:
                src["key"] = [api_key] if isinstance(src.get("key"), list) else api_key
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
    return {"ok": True, "id": pid, "model": model, "restarted": True}


def _restart_astrbot() -> None:
    from .manage import run as manage_run

    try:
        manage_run("astrbot", "restart")
    except Exception:  # noqa: BLE001 - 重启失败不影响配置已落盘
        pass


# ---------- 声线管理 ----------
_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff-]")


def _voice_dir(name: str) -> Path:
    name = str(name or "").strip()
    if not name or _SAFE_NAME.sub("", name) != name or name.startswith("."):
        raise ValueError("声线名称非法")
    return VOICE_ROOT / name


def _voice_prompt(folder: Path) -> str:
    """参考音对应的转写文本。

    prompt.txt 里如果是「（使用 instruct 模式，无需参考文本）」这类备注，
    就不是转写，不能喂给 inference_zero_shot（参考音和文本对不上，音色会走样）。
    """
    path = folder / "prompt.txt"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8-sig").strip()
    if (not text or text.startswith(("（", "("))
            or "无需参考文本" in text or "instruct" in text.lower()):
        return ""
    return text


def _voice_mode(folder: Path) -> Tuple[str, str]:
    """按 CosyVoice2 实际推理路径判定模式（cosyvoice_tts_server.synthesize）。

    有参考音 + 对应转写 -> inference_zero_shot（零样本克隆）
    只有参考音           -> inference_cross_lingual（跨语种克隆，不用转写）
    连参考音都没有       -> 合成不了

    ft_model_*.pth 只是训练产物，TTS 服务当前只加载 CosyVoice2 基座，
    不会走 inference_sft —— 所以不能把「有权重文件」当成「微调模型在用」。
    """
    has_ref = (folder / "ref.wav").exists()
    if not has_ref:
        return "broken", "缺参考音"
    if _voice_prompt(folder):
        return "zero_shot", "零样本克隆"
    return "cross_lingual", "跨语种克隆"


def voices() -> Dict[str, Any]:
    active = ""
    marker = VOICE_ROOT / ".active"
    if marker.exists():
        active = marker.read_text(encoding="utf-8-sig").strip()
    rows: List[Dict[str, Any]] = []
    if VOICE_ROOT.exists():
        for folder in sorted((p for p in VOICE_ROOT.iterdir() if p.is_dir())):
            meta_path = folder / "meta.txt"
            meta = ""
            if meta_path.exists():
                meta = meta_path.read_text(encoding="utf-8-sig").strip()
            mode, mode_label = _voice_mode(folder)
            rows.append({
                "name": folder.name,
                "meta": meta,
                "mode": mode,
                "mode_label": mode_label,
                "has_ref": (folder / "ref.wav").exists(),
                # 训练权重存在 ≠ 服务在用；前端据此显示「未加载」，不再冒充微调模型
                "ft_weight": any(folder.glob("ft_model_*.pth")),
                "active": folder.name == active,
            })
    return {"active": active, "voices": rows}


def switch_voice(name: str) -> Dict[str, Any]:
    folder = _voice_dir(name)
    if not (folder / "ref.wav").exists():
        raise ValueError("该声线缺少 ref.wav")
    VOICE_ROOT.mkdir(parents=True, exist_ok=True)
    (VOICE_ROOT / ".active").write_text(folder.name + "\n", encoding="utf-8")
    return {"ok": True, "active": folder.name}


def delete_voice(name: str) -> Dict[str, Any]:
    folder = _voice_dir(name)
    if not folder.exists():
        raise ValueError("声线不存在")
    shutil.rmtree(folder)
    remaining = sorted(p.name for p in VOICE_ROOT.iterdir() if p.is_dir())
    active = ""
    marker = VOICE_ROOT / ".active"
    if marker.exists():
        active = marker.read_text(encoding="utf-8-sig").strip()
    if name == active or not active:
        active = next((n for n in remaining if n == "小忆"), remaining[0] if remaining else "")
        marker.write_text((active + "\n") if active else "", encoding="utf-8")
    return {"ok": True, "active": active}


def add_voice(name: str, ref_path: str, prompt: str = "", meta: str = "自定义") -> Dict[str, Any]:
    folder = _voice_dir(name)
    if folder.exists():
        raise ValueError("声线已存在: " + folder.name)
    src = Path(str(ref_path or "").strip())
    if not src.is_file():
        raise ValueError("参考音频不存在: " + str(src))
    if src.suffix.lower() != ".wav":
        raise ValueError("参考音频必须是 .wav")
    folder.mkdir(parents=True)
    shutil.copy2(src, folder / "ref.wav")
    (folder / "prompt.txt").write_text(str(prompt or "").strip() + "\n", encoding="utf-8")
    (folder / "meta.txt").write_text(str(meta or "自定义").strip() + "\n", encoding="utf-8")
    return {"ok": True, "name": folder.name}


def voice_audio(name: str) -> Any:
    from infra import Blob

    folder = _voice_dir(name)
    ref = folder / "ref.wav"
    if not ref.exists():
        raise ValueError("没有可试听的 ref.wav")
    return Blob(ref.read_bytes(), "audio/wav")


# ---------- 人味水群（human_sim 插件：白天档 / 夜宵档） ----------
HUMAN_SIM_DIR = DATA / "plugins" / "astrbot_plugin_human_sim"
HUMAN_SIM_CONFIG = HUMAN_SIM_DIR / "config.json"

# key -> (显示名, 最小值, 最大值, 类型)
_HUMAN_SIM_NUM: Dict[str, Tuple[str, float, float, Any]] = {
    "start_hour": ("白天开始（点）", 0, 23, int),
    "end_hour": ("白天结束（点）", 0, 23, int),
    "night_frequency_scale": ("夜间频率系数", 0, 1, float),
    "ai_decide_probability": ("主动决策抽样", 0, 1, float),
    "ai_decide_cooldown_seconds": ("主动决策冷却（秒）", 0, 86400, int),
    "decision_check_interval_seconds": ("决策检查间隔（秒）", 1, 3600, int),
    "ai_decide_per_minute": ("主动决策每分钟", 0, 60, int),
    "followup_seconds": ("接话窗口（秒）", 0, 3600, int),
    "followup_max_messages": ("窗口内最多条数", 0, 20, int),
    "followup_min_gap_seconds": ("跟进最小间隔（秒）", 0, 600, int),
    "interject_probability": ("随机插话概率", 0, 1, float),
    "interject_cooldown_minutes": ("插话冷却（分钟）", 0, 1440, int),
    "group_idle_minutes": ("冷场判定（分钟）", 0, 1440, int),
    "group_idle_probability": ("冷场开话头概率", 0, 1, float),
    "solo_face_probability": ("单发表情概率", 0, 1, float),
    "solo_sticker_probability": ("表情包概率", 0, 1, float),
    "repeat_probability": ("复读概率", 0, 1, float),
    "repeat_cooldown_seconds": ("复读冷却（秒）", 0, 86400, int),
    "global_per_minute": ("全局每分钟上限", 0, 60, int),
    "global_gap_min": ("全局最小间隔（秒）", 0, 600, int),
    "global_gap_max": ("全局最大间隔（秒）", 0, 600, int),
    "pm_active_probability": ("私聊主动关心概率", 0, 1, float),
    "pm_active_cooldown_hours": ("私聊关心冷却（小时）", 0, 168, int),
}
_HUMAN_SIM_BOOL = {
    "ai_decide": "AI 自主决策",
    "repeat_enabled": "复读开关",
    "plus1_enabled": "+1 机械跟队（关 = 全交给 AI 判断）",
}


def _human_sim_defaults() -> Dict[str, Any]:
    """插件默认值（读不到插件源码时的兜底），只取本卡片暴露的键。"""
    return {
        "start_hour": 9,
        "end_hour": 23,
        "night_frequency_scale": 0.25,
        "ai_decide": True,
        "ai_decide_probability": 0.3,
        "ai_decide_cooldown_seconds": 90,
        "decision_check_interval_seconds": 8,
        "ai_decide_per_minute": 2,
        "followup_seconds": 180,
        "followup_max_messages": 8,
        "followup_min_gap_seconds": 10,
        "interject_probability": 0.02,
        "interject_cooldown_minutes": 45,
        "group_idle_minutes": 120,
        "group_idle_probability": 0.15,
        "solo_face_probability": 0.06,
        "solo_sticker_probability": 0.05,
        "repeat_enabled": True,
        "repeat_probability": 0.25,
        "repeat_cooldown_seconds": 600,
        "plus1_enabled": False,
        "global_per_minute": 3,
        "global_gap_min": 20,
        "global_gap_max": 60,
        "pm_active_probability": 0.2,
        "pm_active_cooldown_hours": 8,
    }


def _in_daytime(hour: int, start: int, end: int) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def human_sim() -> Dict[str, Any]:
    """读取 human_sim 的水群参数 + 当前落在白天档还是夜宵档。"""
    defaults = _human_sim_defaults()
    cfg = _read_json(HUMAN_SIM_CONFIG) if HUMAN_SIM_CONFIG.exists() else {}
    values: Dict[str, Any] = {}
    for key in _HUMAN_SIM_NUM:
        raw = cfg.get(key, defaults[key])
        try:
            values[key] = _HUMAN_SIM_NUM[key][3](raw)
        except (TypeError, ValueError):
            values[key] = defaults[key]
    for key in _HUMAN_SIM_BOOL:
        values[key] = bool(cfg.get(key, defaults[key]))
    start, end = int(values["start_hour"]), int(values["end_hour"])
    hour = time.localtime().tm_hour
    night_scale = float(values["night_frequency_scale"])
    return {
        "config": values,
        "period": {
            "start_hour": start,
            "end_hour": end,
            "now_hour": hour,
            "is_day": _in_daytime(hour, start, end),
            "night_scale": night_scale,
            "night_silent": night_scale <= 0,
        },
        "plugin_online": HUMAN_SIM_DIR.exists(),
        "restart_required": True,
    }


def save_human_sim(payload: Dict[str, Any]) -> Dict[str, Any]:
    """只合并卡片暴露的键；其它配置原样保留，保存后必须重启 AstrBot 才生效。"""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    with _LOCK:
        cfg = _read_json(HUMAN_SIM_CONFIG)
        for key, (label, low, high, cast) in _HUMAN_SIM_NUM.items():
            if key not in payload:
                continue
            try:
                value = cast(payload[key])
            except (TypeError, ValueError) as error:
                raise ValueError(f"{label}必须是数字") from error
            if value < low or value > high:
                raise ValueError(f"{label}必须在 {low} ~ {high} 之间")
            cfg[key] = value
        for key, label in _HUMAN_SIM_BOOL.items():
            if key in payload:
                cfg[key] = bool(payload[key])
        start = int(cfg.get("start_hour", 9))
        end = int(cfg.get("end_hour", 23))
        if start == end:
            raise ValueError("白天开始和结束不能相同（会变成全天有效）")
        gap_min = int(cfg.get("global_gap_min", 15))
        gap_max = int(cfg.get("global_gap_max", 45))
        if gap_min > gap_max:
            raise ValueError("全局最小间隔不能大于最大间隔")
        HUMAN_SIM_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        _write_json(HUMAN_SIM_CONFIG, cfg)
    _restart_astrbot()
    result = human_sim()
    result["restarted"] = True
    return result


def handle_human_sim(_req: Any) -> Any:
    from infra import ok

    return ok(human_sim())


def handle_human_sim_save(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    body = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    try:
        return ok(save_human_sim(body))
    except ValueError as error:
        return fail(str(error), 400)


# ---------- 会话工具档位（tool_gate 的 access.json，群/好友会话生效） ----------
_TOOL_LEVELS = ("full", "standard", "lite", "media", "admin", "info", "guest", "none", "custom")
_TOOL_LEVEL_LABEL = {
    "full": "全量",
    "standard": "标准",
    "lite": "精简",
    "media": "表情语音",
    "admin": "群管通知",
    "info": "只读查询",
    "guest": "陌生人",
    "none": "禁用",
    "custom": "自定义",
}
_TOOL_LEVEL_DESC = {
    "full": "全量工具，适合 DeepSeek 等云端强模型",
    "standard": "日常聊天：人设白名单，没设白名单时收敛到常用工具",
    "lite": "小模型精简：表情/语音/时间/联网/天气/头像/记忆",
    "media": "只要活跃：表情包/超级表情/语音/唱歌/贴表情/+1",
    "admin": "群管通知：提醒/入退群通知/群成员/跨群发送/私聊",
    "info": "只读查询：时间/天气/联网/头像/长期记忆/空间动态",
    "guest": "陌生人：只聊天，仅时间/天气/头像",
    "none": "禁用所有工具",
    "custom": "自定义白名单，按勾选下发工具",
}
_TOOL_ROLES = ("group", "friend", "stranger")
_LEVEL_ID_RE = re.compile(r"[a-z0-9_-]{2,24}")


def _custom_levels(access: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """页面新建的档位：access.json 的 levels = {ID: {label, desc, tools}}。"""
    raw = access.get("levels")
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, item in raw.items():
        level = str(key or "").strip()
        if not level or level in _TOOL_LEVELS or not isinstance(item, dict):
            continue
        out[level] = {
            "label": str(item.get("label") or level).strip()[:24] or level,
            "desc": str(item.get("desc") or "").strip()[:160],
            "tools": _as_list(item.get("tools")),
        }
    return out


def _known_levels(access: Dict[str, Any]) -> set:
    return set(_TOOL_LEVELS) | set(_custom_levels(access))


def _tool_levels_meta(access: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = [
        {"id": key, "label": _TOOL_LEVEL_LABEL[key], "desc": _TOOL_LEVEL_DESC[key]}
        for key in _TOOL_LEVELS
    ]
    for key, item in _custom_levels(access).items():
        rows.append(
            {
                "id": key,
                "label": item["label"],
                "desc": item["desc"] or f"自定义档位 · {len(item['tools'])} 个工具",
                "custom": True,
                "tools": item["tools"],
            }
        )
    return rows


def tool_access() -> Dict[str, Any]:
    """读取全局档位、角色档位、各会话配置和可用工具名。"""
    access = _read_json(ACCESS_PATH)
    groups = access.get("groups") if isinstance(access.get("groups"), dict) else {}
    roles = access.get("roles") if isinstance(access.get("roles"), dict) else {}
    try:
        names = [
            str(item).strip()
            for item in (_read_json(TOOL_NAMES_PATH).get("tools") or [])
            if str(item).strip()
        ]
    except ValueError:
        names = []
    return {
        "groups": groups,
        "tools": names,
        "default_level": str(access.get("default_level") or "standard"),
        "roles": {
            role: str(roles.get(role) or ("guest" if role == "stranger" else "standard"))
            for role in _TOOL_ROLES
        },
        "levels": _tool_levels_meta(access),
    }


def group_tools() -> Dict[str, Any]:
    """兼容旧接口：返回与 tool_access 相同的数据。"""
    return tool_access()


def save_tool_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """写入全局默认档位与角色档位（群聊 / 好友私聊 / 陌生人私聊）。"""
    default_level = payload.get("default_level")
    roles = payload.get("roles") if isinstance(payload.get("roles"), dict) else {}
    with _LOCK:
        access = _read_json(ACCESS_PATH)
        known = _known_levels(access)
        if default_level is not None:
            level = str(default_level).strip()
            if level not in known:
                raise ValueError("默认档位非法: " + level)
            access["default_level"] = level
        current = access.get("roles") if isinstance(access.get("roles"), dict) else {}
        for role in _TOOL_ROLES:
            if role not in roles:
                continue
            level = str(roles.get(role) or "").strip()
            if level not in known:
                raise ValueError("角色档位非法: " + role + "=" + level)
            current[role] = level
        access["roles"] = current
        access.setdefault("default_level", "standard")
        _write_json(ACCESS_PATH, access)
    return tool_access()


def save_group_tools(payload: Dict[str, Any]) -> Dict[str, Any]:
    """按会话写入工具档位；level 为空 = 跟随全局默认。

    group_id 兼容群号和好友 QQ，优先取 group_id，其次 session_id。
    """
    gid = str(payload.get("group_id") or payload.get("session_id") or "").strip()
    if not gid:
        raise ValueError("会话 ID 不能为空")
    level = str(payload.get("level") or "").strip()
    tools = _as_list(payload.get("tools"))
    with _LOCK:
        access = _read_json(ACCESS_PATH)
        if level and level not in _known_levels(access):
            raise ValueError("工具档位非法: " + level)
        groups = access.get("groups") if isinstance(access.get("groups"), dict) else {}
        entry = groups.get(gid)
        if not isinstance(entry, dict):
            entry = {}
        name = str(payload.get("name") or "").strip()
        if name:
            entry["name"] = name
        if level:
            entry["level"] = level
        else:
            entry.pop("level", None)
        if level == "custom":
            entry["tools"] = tools
        else:
            entry.pop("tools", None)
        groups[gid] = entry
        access["groups"] = groups
        access.setdefault("default_level", "standard")
        _write_json(ACCESS_PATH, access)
    return tool_access()


def save_tool_level(payload: Dict[str, Any]) -> Dict[str, Any]:
    """新建 / 修改 / 删除页面自定义档位，写在 access.json 的 levels 里（热生效）。"""
    action = str(payload.get("action") or "save").strip().lower()
    level = str(payload.get("id") or "").strip().lower()
    if not _LEVEL_ID_RE.fullmatch(level):
        raise ValueError("档位 ID 只能用 2-24 位小写字母、数字、- 或 _")
    if level in _TOOL_LEVELS:
        raise ValueError("内置档位不能改名或删除：" + level)
    with _LOCK:
        access = _read_json(ACCESS_PATH)
        current = _custom_levels(access)
        if action == "delete":
            if level not in current:
                raise ValueError("档位不存在: " + level)
            used = [
                str(key)
                for key, item in (access.get("groups") or {}).items()
                if isinstance(item, dict) and str(item.get("level") or "") == level
            ]
            roles = access.get("roles") if isinstance(access.get("roles"), dict) else {}
            used += [role for role, value in roles.items() if str(value or "") == level]
            if str(access.get("default_level") or "") == level:
                used.append("默认档位")
            if used:
                raise ValueError("档位仍在使用，先改成别的档位：" + "、".join(used[:6]))
            current.pop(level, None)
        else:
            label = str(payload.get("label") or "").strip()[:24] or level
            desc = str(payload.get("desc") or "").strip()[:160]
            tools = _as_list(payload.get("tools"))
            known = set(
                str(item).strip()
                for item in (_read_json(TOOL_NAMES_PATH).get("tools") or [])
                if str(item).strip()
            )
            if known:
                tools = [tool for tool in tools if tool in known]
            current[level] = {"label": label, "desc": desc, "tools": tools}
        if current:
            access["levels"] = current
        else:
            access.pop("levels", None)
        access.setdefault("default_level", "standard")
        _write_json(ACCESS_PATH, access)
    return tool_access()


# ---------- 接口处理 ----------
def handle_tool_level(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(save_tool_level(payload))
    except ValueError as error:
        return fail(str(error), 400)


def handle_get(_req: Any) -> Any:
    from infra import ok

    return ok(get_config())


def handle_save(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    body = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    try:
        return ok(save_config(body))
    except ValueError as error:
        return fail(str(error), 400)


def handle_commands(_req: Any) -> Any:
    from infra import ok

    return ok(commands())


def handle_groups(_req: Any) -> Any:
    from infra import ok

    return ok(groups())


def handle_group_tools(_req: Any) -> Any:
    from infra import ok

    return ok(group_tools())


def handle_group_tools_save(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(save_group_tools(payload))
    except ValueError as error:
        return fail(str(error), 400)


def handle_tool_access(_req: Any) -> Any:
    from infra import ok

    return ok(tool_access())


def handle_tool_access_save(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(save_tool_settings(payload))
    except ValueError as error:
        return fail(str(error), 400)


def handle_admin_profile(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(save_admin_profile(payload))
    except ValueError as error:
        return fail(str(error), 400)


def handle_admin_remove(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(remove_admin(payload.get("id")))
    except ValueError as error:
        return fail(str(error), 400)


def handle_friends(_req: Any) -> Any:
    from infra import ok

    return ok(friends())


def handle_leave(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(leave_group(payload.get("group_id")))
    except ValueError as error:
        return fail(str(error), 400)


def handle_friend_delete(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(delete_friend(payload.get("user_id")))
    except ValueError as error:
        return fail(str(error), 400)


def handle_persona(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    action = str(payload.get("action") or "")
    try:
        if action == "delete":
            return ok(delete_persona(payload.get("id")))
        if action == "update":
            return ok(update_persona(payload.get("id"), payload.get("name"), payload.get("prompt")))
        if action == "get":
            return ok(persona_detail(payload.get("id")))
        return ok(create_persona(payload.get("name"), payload.get("prompt"), payload.get("id")))
    except ValueError as error:
        return fail(str(error), 400)


def handle_model(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    action = str(payload.get("action") or "")
    try:
        if action == "delete":
            return ok(delete_model(payload.get("id")))
        if action == "update":
            return ok(update_model(
                payload.get("id"),
                payload.get("model"),
                payload.get("source"),
                payload.get("api_base"),
                payload.get("api_key"),
                payload.get("temperature"),
                payload.get("top_p"),
                payload.get("max_context_tokens"),
            ))
        return ok(create_model(
            payload.get("source"),
            payload.get("model"),
            payload.get("id"),
            payload.get("source_new"),
            payload.get("api_base"),
            payload.get("api_key"),
            payload.get("temperature"),
            payload.get("top_p"),
            payload.get("max_context_tokens"),
        ))
    except ValueError as error:
        return fail(str(error), 400)


def handle_voices(_req: Any) -> Any:
    from infra import ok

    return ok(voices())


def handle_voice(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    action = str(payload.get("action") or "")
    try:
        if action == "switch":
            return ok(switch_voice(payload.get("name")))
        if action == "delete":
            return ok(delete_voice(payload.get("name")))
        if action == "add":
            return ok(add_voice(
                payload.get("name"), payload.get("ref_path"),
                payload.get("prompt"), payload.get("meta"),
            ))
        return fail("未知动作: " + action, 400)
    except (ValueError, OSError) as error:
        return fail(str(error), 400)


def handle_voice_audio(req: Any) -> Any:
    from infra import fail

    try:
        return 200, voice_audio(req.q("name"))
    except ValueError as error:
        return fail(str(error), 404)


def handle_tts(req: Any) -> Any:
    """给桌宠用的合成接口：用指定克隆声线（cosyvoice profiles）念一句话，直接回 wav。

    桌宠自带的 /voice/speak 只认 4 个 Edge 音色，切到克隆声线后会把名字当 Edge 音色传，
    于是 400 静音。这里由中枢代转 CosyVoice，桌宠按名字区分走哪条路。
    """
    from infra import Blob, fail

    payload = req.payload if isinstance(req.payload, dict) else {}
    text = str(payload.get("text") or "").strip()
    voice = str(payload.get("voice") or "").strip()
    if not text:
        return fail("text 不能为空", 400)
    try:
        folder = _voice_dir(voice)
    except ValueError as error:
        return fail(str(error), 400)
    if not (folder / "ref.wav").exists():
        return fail("该声线缺少 ref.wav", 400)
    body = {
        "text": text,
        "prompt_wav": str(folder / "ref.wav"),
        "prompt_text": _voice_prompt(folder),
        "fmt": "wav",
        "speed": 1.0,
    }
    status, data = http_call("POST", "http://127.0.0.1:8714/tts", body, timeout=90.0)
    if status != 200 or not isinstance(data, (bytes, bytearray)):
        message = data.get("error") if isinstance(data, dict) else "CosyVoice 未响应"
        return fail("合成失败：" + str(message or "未知错误"), 502)
    return 200, Blob(bytes(data), "audio/wav")
