"""群与好友：QQ 走 NapCat OneBot，微信走 io-wechat；退群/删好友/头像缓存。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ._admins import CONFIG_PATH, HUB_DATA, ROOT, _LOCK, _as_list, _as_map, _read_json, _write_json
from .http_client import http_call

ONEBOT_CONFIG = ROOT / "napcat-win" / "napcat" / "config" / "onebot11.json"
AVATAR_DIR = HUB_DATA / "avatar_cache"
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


def _avatar_cached(platform: str, ident: str) -> Optional[Tuple[bytes, str]]:
    """读本机已落盘的头像文件。"""
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", ident)
    for ext, ctype in ((".jpg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp")):
        path = AVATAR_DIR / (platform + "-" + safe + ext)
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path.read_bytes(), ctype
        except OSError:
            continue
    return None


def handle_avatar(req: Any) -> Any:
    """GET /bot/avatar?platform=qq&id=123：把头像转成真实图片给控制台。

    QQ 头像走 qlogo（多节点重试），拿到后落盘缓存；微信头像在本机加密库里，
    进程内解不出来，只认已经缓存好的文件。取不到就回 404，前端会退回字符占位，
    不会出现破图。
    """
    from infra import Blob, fail

    platform = str(req.q("platform") or "qq").strip().lower() or "qq"
    ident = str(req.q("id") or "").strip()
    if not ident:
        return fail("id 不能为空", 400)
    cached = _avatar_cached(platform, ident)
    if cached:
        return 200, Blob(cached[0], cached[1])
    if platform == "qq" and ident.isdigit():
        size = str(req.q("size") or "100")
        if not size.isdigit():
            size = "100"
        for host in ("q1.qlogo.cn", "q4.qlogo.cn", "q2.qlogo.cn", "q3.qlogo.cn"):
            url = "https://" + host + "/g?b=qq&nk=" + ident + "&s=" + size
            status, data = http_call("GET", url, timeout=8.0)
            if status != 200 or not isinstance(data, (bytes, bytearray)):
                continue
            body = bytes(data)
            if body[:3] != b"\xff\xd8\xff" and body[:8] != b"\x89PNG\r\n\x1a\n":
                continue
            try:
                AVATAR_DIR.mkdir(parents=True, exist_ok=True)
                (AVATAR_DIR / (platform + "-" + ident + ".jpg")).write_bytes(body)
            except OSError:
                pass
            return 200, Blob(body, "image/jpeg")
    return fail("没有可用的头像", 404)
