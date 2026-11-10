"""会话解析：uid ↔ 显示名 ↔ 发送目标。显示名只用于展示，发送只认 uid。"""

from __future__ import annotations

import threading
import time

import wechatauto

from . import state
from .config import UID_RE


def chat(who: str):
    """复用附着时的 GUI/DB，避免每次发送都重新扫描窗口（每次几秒）。"""
    return wechatauto.Chat(
        who=who,
        gui=getattr(state.wx, "_gui", None),
        db=getattr(state.wx, "_db", None),
    )


def display_name(uid: str) -> str:
    """uid → 微信界面上的显示名（好友/群），用来判断目标会话是不是已经开着。"""
    db = _resolve_db()
    if db is None:
        return ""
    try:
        if uid.endswith("@chatroom"):
            return str(db.group_id_to_name(uid) or "")
        return str(db.get_nickname(uid) or "")
    except Exception:  # noqa: BLE001
        return ""


def group_rows(limit: int = 800) -> list[dict]:
    """群列表：联系人库里的全部 *@chatroom + 会话里新出现的群。

    只看会话表会漏掉很久没说话的群；中枢的群管控要能配到所有群。
    """
    db = _resolve_db()
    if db is None:
        return []
    rows = []
    seen = set()

    conn = None
    try:
        conn = db._contact_conn()
    except Exception:  # noqa: BLE001
        conn = None
    if conn is not None:
        try:
            items = conn.execute(
                "SELECT username, nick_name FROM contact "
                "WHERE username LIKE '%@chatroom' LIMIT ?", (limit,),
            ).fetchall()
            for item in items:
                uid = str(item["username"] or "")
                name = str(item["nick_name"] or "").strip()
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                rows.append({"id": uid, "name": name or uid})
        except Exception:  # noqa: BLE001
            pass
        finally:
            conn.close()

    try:
        sessions = db.get_sessions(limit=limit)
    except Exception:  # noqa: BLE001
        sessions = []
    for item in sessions:
        uid = str(item.get("username") or "")
        if not uid.endswith("@chatroom") or uid in seen:
            continue
        seen.add(uid)
        try:
            name = str(db.group_id_to_name(uid) or "")
        except Exception:  # noqa: BLE001
            name = ""
        rows.append({"id": uid, "name": name or uid})
    return rows


_SKIP_FRIEND_UID = {
    "filehelper", "fmessage", "medianote", "floatbottle", "weixin",
    "brandsessionholder", "qqmail", "tmessage", "mphelper", "notifymessage",
    "opencustomerservicemsg", "weixinreminder", "newsapp",
}
# 企微机器人 / 客服号 / 群聊，看起来是联系人但不是正常好友
_SKIP_FRIEND_SUFFIX = ("@weclaw", "@openim", "@kefu", "@chatroom")


def friend_rows(limit: int = 300) -> list[dict]:
    """通讯录里的个人好友（排除群、群成员、公众号、系统号），给中枢好友列表用。

    实测 contact.db（微信 4.1.15）：
      local_type=3 / flag=4  -> 只是「群里见过的人」的成员缓存，不是好友；
      local_type=1 / flag&1=1 -> 通讯录里的联系人（好友、关注的公众号、系统号）。
    所以必须同时看 local_type 和 flag 最低位，不能只按 username 过滤。
    """
    db = _resolve_db()
    if db is None:
        return []
    conn = None
    try:
        conn = db._contact_conn()
    except Exception:  # noqa: BLE001
        conn = None
    if conn is None:
        return []
    self_uid = str(getattr(db, "wxid", "") or "")
    rows = []
    seen = set()
    try:
        items = conn.execute(
            "SELECT username, nick_name, remark FROM contact "
            "WHERE local_type NOT IN (0, 2, 3) AND (flag&1)=1 AND delete_flag=0 "
            "AND username NOT LIKE 'gh_%' AND username NOT LIKE '%@chatroom' LIMIT ?",
            (limit,),
        ).fetchall()
        for item in items:
            uid = str(item["username"] or "")
            name = str(item["remark"] or item["nick_name"] or "").strip()
            if (not uid or not name or uid in seen or uid in _SKIP_FRIEND_UID
                    or uid == self_uid or uid.endswith(_SKIP_FRIEND_SUFFIX)):
                continue
            seen.add(uid)
            rows.append({"id": uid, "name": name})
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()
    return rows


# ---------- 头像（head_image.db，与 contact.db 同一主密钥派生） ----------
# 解密后的图片按 uid 缓存一小时：好友列表刷新时几十张图不该反复解库。
_AVATAR_CACHE: dict = {}
_AVATAR_LOCK = threading.Lock()
_AVATAR_TTL = 3600


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _head_image_rel(db) -> str | None:
    for rel, path, _ in db._db_files:
        if path.replace("\\", "/").endswith("head_image/head_image.db"):
            return rel
    return None


def _resolve_db():
    """解密库句柄：优先附着时缓存的 _db，取不到就走 gui 的懒打开。

    wechatauto 只在部分附着路径上挂 _db；发送链路一直用 gui._get_db()，
    头像/昵称查询必须走同一个来源，否则永远查不到。
    """
    wx = state.wx
    if wx is None:
        return None
    db = getattr(wx, "_db", None)
    if db is not None:
        return db
    g = getattr(wx, "_gui", None)
    if g is not None and hasattr(g, "_get_db"):
        try:
            return g._get_db()
        except Exception:  # noqa: BLE001
            return None
    return None


def avatar(uid: str) -> tuple:
    """uid → (图片字节, Content-Type)；没有头像返回 (b"", "")。

    head_image.db 和 contact.db 一样是 SQLCipher，走 wechatauto 已提取的密钥
    解密（_open 自带按库锁与解密缓存），群头像也有少量存在同表里。
    """
    uid = (uid or "").strip()
    if not uid:
        return b"", ""
    now = time.time()
    with _AVATAR_LOCK:
        hit = _AVATAR_CACHE.get(uid)
        if hit and now - hit[0] < _AVATAR_TTL:
            return hit[1], hit[2]
    db = _resolve_db()
    if db is None:
        from .util import log as _log
        wx = state.wx
        g = getattr(wx, "_gui", None) if wx is not None else None
        err = ""
        if g is not None and hasattr(g, "_get_db"):
            try:
                g._get_db()
            except Exception as e:  # noqa: BLE001
                err = f" get_db异常 {type(e).__name__}: {e}"
        _log(
            f"[diag] avatar: 无可用库 wx={'None' if wx is None else 'OK'} "
            f"_db={'None' if getattr(wx, '_db', None) is None else 'OK'} "
            f"_gui={'None' if g is None else 'OK'}{err}"
        )
        return b"", ""
    rel = _head_image_rel(db)
    if rel is None:
        from .util import log as _log
        _files = [r for r, _, _ in getattr(db, "_db_files", [])][:8]
        _log(f"[diag] avatar: 未找到 head_image 路径, db_files={_files}")
        return b"", ""
    try:
        conn = db._open(rel)
        try:
            row = conn.execute(
                "SELECT image_buffer FROM head_image WHERE username=? LIMIT 1", (uid,)
            ).fetchone()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - 解密失败/微信未登录时退回首字占位
        from .util import log as _log
        _log(f"[diag] avatar 查询失败 {type(e).__name__}: {e}")
        return b"", ""
    data = bytes(row[0]) if row and row[0] else b""
    ctype = _image_type(data) if data else ""
    if data:
        with _AVATAR_LOCK:
            if len(_AVATAR_CACHE) >= 512:
                _AVATAR_CACHE.clear()
            _AVATAR_CACHE[uid] = (now, data, ctype)
    return data, ctype


def display_name(uid: str) -> str:
    """uid → 备注/昵称；联系人库里查不到返回空串。

    引擎解析不出群成员显示名时由调用方兜底，避免裸 wxid/openim id
    漏进桌宠气泡和管道记忆。
    """
    uid = (uid or "").strip()
    if not uid:
        return ""
    db = _resolve_db()
    if db is None:
        return ""
    try:
        for h in db.search_contact(uid):
            if str(h.get("username") or "") == uid:
                return str(h.get("remark") or h.get("nick_name") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def uid_exists(uid: str) -> bool:
    db = _resolve_db()
    if db is None:
        return False
    try:
        for h in db.search_contact(uid):
            if str(h.get("username") or "") == uid:
                return True
        for s in db.get_sessions(limit=500):
            if str(s.get("username") or "") == uid:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def resolve_target(to: str) -> str:
    """把发送目标收敛成微信会话 username。

    只接受 wxid / *@chatroom / filehelper，或联系人库里**唯一精确匹配**的
    昵称/备注。解析不出来一律拒绝——绝不退回「当前打开的会话/输入框」。
    """
    to = (to or "").strip().lstrip("@").strip()
    if not to:
        raise ValueError("目标会话为空，拒绝发送")
    if UID_RE.match(to):
        if to == "filehelper" or uid_exists(to):
            return to
        raise ValueError(f"会话 {to} 不在联系人/会话列表中，拒绝发送")
    db = _resolve_db()
    if db is None:
        raise ValueError(f"无法解析会话「{to}」：请传 wxid 或群 id")
    try:
        hits = db.search_contact(to)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"解析会话「{to}」失败：{exc}") from exc
    uids = {
        str(h.get("username") or "")
        for h in hits
        if to in (h.get("nick_name"), h.get("remark"))
    }
    uids.discard("")
    if len(uids) > 1:
        raise ValueError(f"会话「{to}」匹配到 {len(uids)} 个账号，拒绝猜测")
    if not uids:
        raise ValueError(f"会话「{to}」不存在或不是精确昵称")
    return uids.pop()
