"""机器人管控：读写 provider_router 配置、扫描指令、代查 QQ 群列表。

控制台改的是 astrbot 插件自己的 config.json；插件按 mtime 热加载，
保存后下一条消息即生效，不需要重启 AstrBot。

本文件是门面（facade）：按职责拆出的子模块（_admins/_models/_personas/_groups/
_voices/_humansim/_tools/_tts）在此统一转发，既有 `from hubsrv.botctl import X`
或 `botctl.X` 调用方零改动。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from ._admins import (
    ADMINS_PATH,
    ASTRBOT,
    CMD_CONFIG,
    CONFIG_PATH,
    DATA,
    HUB_DATA,
    PLUGIN_DIR,
    ROOT,
    _LOCK,
    _admin_store,
    _as_list,
    _as_map,
    _read_json,
    _restart_astrbot,
    _sync_owner_ids,
    _write_admins,
    _write_json,
    admin_profiles,
    admins,
    remove_admin,
    save_admin_profile,
)
from ._groups import (
    AVATAR_DIR,
    ONEBOT_CONFIG,
    WECHAT_API,
    _CTRL_RE,
    _TAG_RE,
    _avatar_cached,
    _clean_name,
    _onebot,
    _onebot_token,
    _qq_friends,
    _qq_groups,
    _wechat_friends,
    _wechat_groups,
    delete_friend,
    friends,
    groups,
    handle_avatar,
    leave_group,
)
from ._models import (
    _apply_source,
    _mask_key,
    _opt_num,
    create_model,
    delete_model,
    provider_sources,
    providers,
    save_vision,
    update_model,
    vision_setting,
)
from ._personas import (
    PERSONA_DB,
    SEED_DIR,
    _persona_title,
    _seed_json,
    create_persona,
    delete_persona,
    import_personas,
    persona_detail,
    persona_seeds,
    personas,
    update_persona,
    vendor_presets,
)
from ._tools import (
    ACCESS_PATH,
    TOOL_NAMES_PATH,
    _LEVEL_ID_RE,
    _TOOL_LEVELS,
    _TOOL_LEVEL_DESC,
    _TOOL_LEVEL_LABEL,
    _TOOL_ROLES,
    _custom_levels,
    _known_levels,
    _tool_levels_meta,
    group_tools,
    handle_tool_level,
    save_group_tools,
    save_tool_level,
    save_tool_settings,
    tool_access,
)
from ._tts import (
    TTS_CACHE_KEEP_DAYS,
    TTS_CACHE_MAX_FILES,
    _prune_tts_cache,
    handle_tts,
)
from ._pluginconf import (
    handle_plugins,
    handle_plugins_save,
    handle_qzone_scan,
    plugins_configs,
    save_plugin_config,
)
from ._voices import (
    VOICE_ROOT,
    _SAFE_NAME,
    _voice_dir,
    _voice_mode,
    _voice_prompt,
    add_voice,
    delete_voice,
    switch_voice,
    voice_audio,
    voices,
)
from ._humansim import (
    HUMAN_SIM_CONFIG,
    HUMAN_SIM_DIR,
    _HUMAN_SIM_BOOL,
    _HUMAN_SIM_NUM,
    _human_sim_defaults,
    _in_daytime,
    handle_human_sim,
    handle_human_sim_save,
    handle_mute_save,
    handle_mutes,
    human_sim,
    mutes,
    save_human_sim,
    set_mute,
)

PLUGINS_DIR = DATA / "plugins"

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "block_users": [],
    "block_groups": [],
    "listen_groups": [],
    "user_provider_ids": {},
    "group_provider_ids": {},
    "user_provider_backup_ids": {},
    "group_provider_backup_ids": {},
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
_MAPS = (
    "user_provider_ids",
    "group_provider_ids",
    "user_provider_backup_ids",
    "group_provider_backup_ids",
)
_TEXTS = (
    "group_provider_id",
    "private_provider_id",
    "stranger_private_provider_id",
    "wechat_provider_id",
    "deepseek_provider_id",
)


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
    editable["user_provider_backup_ids"] = _as_map(editable["user_provider_backup_ids"])
    editable["group_provider_backup_ids"] = _as_map(editable["group_provider_backup_ids"])
    editable["group_persona_ids"] = _as_map(editable["group_persona_ids"])
    editable["user_persona_ids"] = _as_map(editable["user_persona_ids"])
    return {
        "config": editable,
        "admins": admins(),
        "admin_profiles": admin_profiles(),
        "providers": providers(),
        "provider_sources": provider_sources(),
        "personas": personas(),
        "vendors": vendor_presets(),
        "persona_seeds": persona_seeds(),
        "vision": vision_setting(),
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


# ---------- 接口处理 ----------
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


def handle_admins_list(req: Any) -> Any:
    """只读：供本机其它进程（如 recharge_page 云端训练桥）动态注入主人身份。"""
    from infra import ok

    return ok({"admins": admins()})


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
        if action == "import_seeds":
            return ok(import_personas(payload.get("ids")))
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
                payload.get("token"),
                payload.get("timeout"),
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
            payload.get("token"),
            payload.get("timeout"),
        ))
    except ValueError as error:
        return fail(str(error), 400)


def handle_vision(_req: Any) -> Any:
    from infra import ok

    return ok(vision_setting())


def handle_vision_save(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(save_vision(payload.get("provider_id")))
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
