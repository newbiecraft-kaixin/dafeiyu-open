"""QQ 空间页面的纯解析工具（不依赖插件实例，方便单测）。"""

from __future__ import annotations

import json
import re


def _extract_qzonetoken(html: str) -> str:
    """从空间首页 HTML 里抠 qzonetoken。

    现在页面上多是 `window.g_qzonetoken = (function(){ try{return "xxx";} catch(e){}})()`，
    也有老版本直接给明文/单引号/JSON 字段，几种形态都试一遍。
    """
    patterns = (
        r"window\.g_qzonetoken\s*=\s*\(function\s*\(\)\s*\{\s*try\s*\{\s*return\s*[\"']?([A-Za-z0-9]{16,})",
        r"window\.g_qzonetoken\s*=\s*[\"']([A-Za-z0-9]{16,})[\"']",
        r"[\"']qzonetoken[\"']\s*[:=]\s*[\"']([A-Za-z0-9]{16,})[\"']",
        r"qzonetoken=([A-Za-z0-9]{16,})",
    )
    for pat in patterns:
        m = re.search(pat, html or "")
        if m:
            return m.group(1)
    return ""


def _parse_jsonp(text: str):
    """Qzone 接口可能返回 JSON 或 JSONP，统一解析成 dict。"""
    t = (text or "").strip()
    if t.startswith("{") or t.startswith("["):
        return json.loads(t)
    m = re.match(r"^[^(]*\((.*)\)\s*;?\s*$", t, re.S)
    if not m:
        raise ValueError(f"不是 JSON/JSONP: {t[:80]}")
    return json.loads(m.group(1))


def _parse_frontpage(html: str) -> dict:
    """从 h5 空间首页里抠出 FrontPage.data 那段 JSON（说说 + 评论都在里面）。"""
    i = html.find("FrontPage = {")
    if i < 0:
        return {}
    m = re.search(r"\bdata\s*:\s*\{", html[i:])
    if not m:
        return {}
    start = i + m.end() - 1
    depth = 0
    instr = esc = False
    for k in range(start, len(html)):
        ch = html[k]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(html[start:k + 1])
                except ValueError:
                    return {}
                return data if isinstance(data, dict) else {}
    return {}


def _extract_json_array(html: str, key: str) -> list:
    """从 HTML 里抠出 "key": [ ... ] 的数组原文并解析（h5 空间页把说说内嵌在页面里）。"""
    i = html.find('"' + key + '"')
    if i < 0:
        return []
    j = html.find("[", i)
    if j < 0:
        return []
    depth = 0
    instr = esc = False
    for k in range(j, len(html)):
        ch = html[k]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(html[j:k + 1])
                except ValueError:
                    return []
                return data if isinstance(data, list) else []
    return []


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return (
        s.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .strip()
    )


def _flatten_comments(items: list, depth: int = 0, parent_uin: str = "") -> list:
    """把顶层评论和楼中楼回复拉平成一条列表，并记下「谁回谁」。

    h5 的 comment.comments 只给顶层评论，用户回复「机器人那条回复」时落在
    comment.replys 里；不拉平就永远收不到这类消息。楼中楼没有 commentid，
    走 _comment_key 的 md5 兜底即可。楼中楼自带 target.uin 标明回复对象，
    这里和父评论作者一起挂到 _target_uin / _parent_uin，轮询时据此跳过
    「别人回别人」的对话，只回真正回我的评论。
    """
    out: list = []
    for c in items or []:
        if not isinstance(c, dict):
            continue
        item = c
        if depth > 0:
            item = dict(c)
            tgt = c.get("target") or {}
            item["_parent_uin"] = parent_uin
            item["_target_uin"] = (
                str(tgt.get("uin") or "") if isinstance(tgt, dict) else ""
            )
        out.append(item)
        if depth < 3:
            author = str((c.get("user") or {}).get("uin") or "") or parent_uin
            out.extend(
                _flatten_comments(
                    c.get("replys") or c.get("replylist") or [],
                    depth + 1,
                    author,
                )
            )
    return out
