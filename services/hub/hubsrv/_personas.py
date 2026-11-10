"""角色（人格）：data_v4.db 读写、厂商/角色预置种子、角色增删改查。"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

from ._admins import CONFIG_PATH, DATA, _as_list, _as_map, _read_json, _restart_astrbot

PERSONA_DB = DATA / "data_v4.db"
SEED_DIR = Path(__file__).resolve().parents[1] / "seeds"


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


# ---------- 预置：厂商 / 角色 ----------
def _read_seed(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _seed_json(name: str) -> Dict[str, Any]:
    data = _read_seed(SEED_DIR / name)
    if name != "personas.json":
        return data
    # 私密人设只放本地的 personas.local.json（已 gitignore），按 id 覆盖/追加，绝不入库
    local = _read_seed(SEED_DIR / "personas.local.json")
    extra = local.get("personas")
    if not isinstance(extra, list) or not extra:
        return data
    merged = {
        str(row.get("id")): row
        for row in data.get("personas") or []
        if isinstance(row, dict) and row.get("id")
    }
    for row in extra:
        if isinstance(row, dict) and row.get("id"):
            merged[str(row["id"])] = row
    return {**data, "personas": list(merged.values())}


def vendor_presets() -> List[Dict[str, Any]]:
    """OpenAI 兼容厂商预置：选完只填 API Key 就能建模型。"""
    data = _seed_json("vendors.json")
    rows = data.get("vendors") if isinstance(data, dict) else []
    return [row for row in rows or [] if isinstance(row, dict) and row.get("id")]


def persona_seeds() -> List[Dict[str, Any]]:
    data = _seed_json("personas.json")
    rows = data.get("personas") if isinstance(data, dict) else []
    return [
        {
            "id": str(item.get("id") or ""),
            "name": _persona_title(str(item.get("id") or ""), str(item.get("prompt") or "")),
            "prompt_len": len(str(item.get("prompt") or "")),
        }
        for item in rows or []
        if isinstance(item, dict) and item.get("id")
    ]


def import_personas(ids: Any = None) -> Dict[str, Any]:
    """把预置角色补进人格库，已存在的跳过；有新增才重启 AstrBot。"""
    data = _seed_json("personas.json")
    rows = data.get("personas") if isinstance(data, dict) else []
    picked = {str(item).strip() for item in _as_list(ids)} if ids else None
    if not PERSONA_DB.exists():
        raise ValueError("人格库还不存在，先让 AstrBot 启动一次")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    imported: List[str] = []
    skipped: List[str] = []
    con = sqlite3.connect(str(PERSONA_DB), timeout=8)
    try:
        have = {str(row[0]) for row in con.execute("SELECT persona_id FROM personas").fetchall()}
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "").strip()
            prompt = str(item.get("prompt") or "")
            if not pid or not prompt or (picked is not None and pid not in picked):
                continue
            if pid in have:
                skipped.append(pid)
                continue
            con.execute(
                "INSERT INTO personas (created_at, updated_at, persona_id, system_prompt, "
                "begin_dialogs, tools, skills, sort_order) VALUES (?, ?, ?, ?, '[]', '[]', '[]', 0)",
                (now, now, pid, prompt),
            )
            imported.append(pid)
        con.commit()
    finally:
        con.close()
    if imported:
        _restart_astrbot()
    return {"ok": True, "imported": imported, "skipped": skipped, "restarted": bool(imported)}


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
