"""把接口的 JSON 结果渲染成浏览器可读页面（API 客户端不受影响）。"""
from __future__ import annotations

import html as _html
import json
from typing import Any

from ..config import port_of
from .types import Html

_PAGE_CSS = """
  :root{--bg:#070a14;--panel:#101731;--line:rgba(122,152,222,.18);--text:#e9edff;
        --muted:#8b97bd;--dim:#636d95;--acc:#6b8fff;--good:#3ddc97;--bad:#ff617b}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;color:var(--text);font:14px/1.7 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;
       background:radial-gradient(900px 520px at 8% -12%,rgba(124,140,248,.20),transparent 62%),
                  radial-gradient(760px 480px at 96% 2%,rgba(34,211,238,.14),transparent 62%),
                  radial-gradient(700px 520px at 50% 110%,rgba(244,114,182,.10),transparent 60%),
                  linear-gradient(180deg,#0c1122,#070a14 46%);
       background-attachment:fixed}
  .wrap{max-width:1080px;margin:0 auto;padding:28px 20px 48px}
  header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between;
         padding:18px 20px;margin-bottom:18px;border:1px solid transparent;border-radius:16px;
         background:linear-gradient(rgba(16,23,49,.92),rgba(16,23,49,.92)) padding-box,
                    linear-gradient(135deg,rgba(124,140,248,.5),rgba(34,211,238,.28) 45%,rgba(244,114,182,.3)) border-box;
         box-shadow:0 10px 30px rgba(0,0,0,.40)}
  h1{margin:0;font-size:19px;
     background:linear-gradient(100deg,#eef1fb 8%,#9db4ff 55%,#7ff0d4 95%);
     -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .path{font-family:Consolas,monospace;color:var(--muted);font-size:13px}
  .pill{font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
  .pill.good{color:#b9f5dd;border-color:rgba(61,220,151,.4);background:rgba(61,220,151,.1);
             box-shadow:0 0 12px rgba(61,220,151,.10)}
  .pill.bad{color:#ffc2cd;border-color:rgba(255,97,123,.4);background:rgba(255,97,123,.1)}
  h3{margin:22px 0 8px;font-size:13px;letter-spacing:1px;color:#c8d3ff}
  table{width:100%;border-collapse:separate;border-spacing:0;margin-top:8px;font-size:13px;
        border:1px solid rgba(122,152,222,.22);border-radius:12px;overflow:hidden;
        background:rgba(16,23,49,.55);box-shadow:0 8px 24px rgba(0,0,0,.30)}
  th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
  tbody tr:last-child th,tbody tr:last-child td{border-bottom:0}
  tbody tr:hover td,tbody tr:hover th{background:linear-gradient(90deg,rgba(124,140,248,.10),rgba(34,211,238,.05))}
  thead th{color:var(--dim);font-weight:600;font-size:12px;background:rgba(122,152,222,.06)}
  table.kv th{width:210px;color:var(--muted);font-weight:500;font-family:Consolas,monospace}
  code,.mono{font-family:Consolas,monospace;color:#bfd0ff}
  td code{font-size:12px;word-break:break-all}
  .chip{display:inline-block;margin:3px 6px 3px 0;padding:3px 11px;border-radius:999px;
        border:1px solid rgba(124,140,248,.30);background:rgba(124,140,248,.10);
        font-family:Consolas,monospace;font-size:12.5px;color:#cfdcff}
  .note{color:var(--dim);font-size:12px;margin:8px 0 0}
  details{margin-top:26px;color:var(--muted)}
  details pre{background:#0a0e1c;border:1px solid var(--line);border-radius:10px;padding:14px;
             overflow:auto;max-height:420px;color:#cfdcff;font-size:12.5px}
  .banner{margin-top:18px;padding:10px 14px;border-radius:10px;font-size:13px;
          border:1px solid var(--line);background:rgba(122,152,222,.06)}
  .banner.good{color:#b9f5dd;border-color:rgba(61,220,151,.35);background:rgba(61,220,151,.07);
               box-shadow:0 0 18px rgba(61,220,151,.08)}
  .banner.bad{color:#ffc2cd;border-color:rgba(255,97,123,.35);background:rgba(255,97,123,.07)}
  footer{margin-top:30px;padding-top:14px;border-top:1px solid var(--line);
         color:var(--dim);font-size:12px;display:flex;gap:16px;flex-wrap:wrap;justify-content:space-between}
  a{color:#9fbcff;text-decoration:none}
  a:hover{text-decoration:underline}
  @media (prefers-reduced-motion: reduce){*,*::before,*::after{transition:none!important;animation:none!important}}
"""


def _esc(value: Any) -> str:
    return _html.escape(str(value), quote=True)


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


_FIELD_ZH = {
    "platform": "平台", "kind": "类型", "trace_id": "追踪号", "ts": "时间",
    "chat": "会话", "sender": "发送者", "mentions": "提及", "text": "内容",
    "media": "媒体", "id": "编号", "type": "类型", "name": "名称", "is_bot": "机器人",
    "seq": "序号", "events": "事件", "online": "在线", "error": "错误", "status": "状态",
    "port": "端口", "layer": "分层", "services": "服务", "tools": "工具", "ok": "结果",
    "count": "数量", "time": "时间", "message": "消息", "user": "用户", "group": "群",
    "voice": "声线", "active": "当前", "persona": "角色", "model": "模型",
    "envelope": "信封", "queued": "排队", "pushed": "已推送", "dropped": "已丢弃",
    "raw": "原始字段", "chat_uid": "会话标识", "is_all": "全员", "ambiguous": "指代不明",
    "sender_id": "发送者编号", "sender_name": "发送者名称", "at_list": "艾特列表",
}

_VALUE_ZH = {
    "private": "私聊", "group": "群聊", "message": "消息", "wechat": "微信",
    "qq": "QQ", "notice": "通知", "ok": "正常", "true": "是", "false": "否",
}


def _label(key: Any) -> str:
    text = str(key)
    return _FIELD_ZH.get(text, text)


def _brief(value: Any) -> str:
    """把嵌套结构写成人能读的一段话，而不是塞一段 JSON。"""
    if _scalar(value):
        if value is None:
            return ""
        if isinstance(value, bool):
            return "是" if value else "否"
        return _VALUE_ZH.get(str(value), str(value)) if isinstance(value, str) else str(value)
    if isinstance(value, dict):
        parts = [
            f"{_label(key)}={_brief(item)}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        ]
        return "，".join(parts)
    if isinstance(value, list):
        return "、".join(_brief(item) for item in value)
    return str(value)


def _cell(value: Any) -> str:
    text = _brief(value)
    text = text if len(text) <= 200 else text[:200] + "…"
    return f"<td>{_esc(text)}</td>"


def _kv_table(obj: dict) -> str:
    rows = "".join(f"<tr><th>{_esc(_label(key))}</th>{_cell(value)}</tr>" for key, value in obj.items())
    return f'<table class="kv"><tbody>{rows}</tbody></table>'


def _list_block(items: list) -> str:
    if not items:
        return '<p class="note">（空）</p>'
    if all(_scalar(item) for item in items):
        chips = "".join(f'<span class="chip">{_esc(item)}</span>' for item in items[:200])
        more = f'<p class="note">仅显示前 200 条，共 {len(items)} 条</p>' if len(items) > 200 else ""
        return chips + more
    lines = []
    for item in items[:200]:
        text = _brief(item)
        lines.append(f"<li>{_esc(str(text)[:300])}</li>")
    more = f'<p class="note">仅显示前 200 条，共 {len(items)} 条</p>' if len(items) > 200 else ""
    return "<ul>" + "".join(lines) + "</ul>" + more


def _rows_table(rows: list) -> str:
    columns: list = []
    for row in rows[:200]:
        if isinstance(row, dict):
            for key in row:
                if key not in columns:
                    columns.append(key)
    head = "".join(f"<th>{_esc(_label(col))}</th>" for col in columns)
    body = "".join(
        "<tr>" + "".join(_cell(row.get(col)) for col in columns) + "</tr>"
        for row in rows[:200]
        if isinstance(row, dict)
    )
    more = f'<p class="note">仅显示前 200 条，共 {len(rows)} 条</p>' if len(rows) > 200 else ""
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}"


def _data_block(data: Any) -> str:
    if isinstance(data, dict):
        if data and all(_scalar(value) for value in data.values()):
            return _kv_table(data)
        parts = []
        flat = {key: value for key, value in data.items() if _scalar(value)}
        if flat:
            parts.append(_kv_table(flat))
        for key, value in data.items():
            if isinstance(value, list):
                block = _rows_table(value) if value and all(isinstance(x, dict) for x in value) else _list_block(value)
                parts.append(f"<h3>{_esc(_label(key))}</h3>{block}")
            elif isinstance(value, dict):
                parts.append(f"<h3>{_esc(_label(key))}</h3>{_data_block(value)}")
        return "".join(parts)
    if isinstance(data, list):
        if data and all(isinstance(item, dict) for item in data):
            return _rows_table(data)
        return _list_block(data)
    return f'<p class="mono">{_esc(data)}</p>'


def data_page(title: str, path: str, status: int, data: Any) -> Html:
    """把接口的 JSON 结果渲染成给浏览器看的页面（API 客户端不受影响）。"""
    flag = None
    inner = data
    if isinstance(data, dict) and "ok" in data and ("data" in data or "error" in data):
        flag = bool(data.get("ok"))
        inner = data.get("data") if flag else data.get("error")
    banner = ""
    if flag is not None:
        text = "调用成功" if flag else f"调用失败：{inner}"
        banner = f'<div class="banner {"good" if flag else "bad"}">{_esc(text)}</div>'
        inner = data.get("data") if flag else None
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    body = _data_block(inner) if inner is not None else ""
    tone = "good" if status < 400 else "bad"
    hub = f"http://127.0.0.1:{port_of('hub', 23408)}/ui"
    return Html(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · {_esc(path)}</title><style>{_PAGE_CSS}</style></head>
<body><div class="wrap">
<header><h1>{_esc(title)}</h1><span class="path">{_esc(path)}</span>
<span class="pill {tone}">HTTP {status}</span></header>
{banner}{body}
<details><summary>原始数据（技术细节）</summary><pre>{_esc(raw)}</pre></details>
<footer><span>地址本身返回的是原始数据，浏览器里会整理成上面这样。</span>
<a href="{hub}">← 返回中枢</a></footer>
</div></body></html>"""
    )
