"""会话解析：uid ↔ 显示名 ↔ 发送目标。显示名只用于展示，发送只认 uid。"""

from __future__ import annotations

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
    db = getattr(state.wx, "_db", None) if state.wx is not None else None
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
    db = getattr(state.wx, "_db", None) if state.wx is not None else None
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
    db = getattr(state.wx, "_db", None) if state.wx is not None else None
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


def uid_exists(uid: str) -> bool:
    db = getattr(state.wx, "_db", None) if state.wx is not None else None
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
    db = getattr(state.wx, "_db", None) if state.wx is not None else None
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
